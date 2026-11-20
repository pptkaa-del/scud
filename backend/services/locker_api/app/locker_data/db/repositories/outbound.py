"""Репозиторий персистентной очереди исходящих событий в history_api.

Используется продьюсерами (см. `app/services/history_producers/`) для атомарной
постановки события вместе с бизнес-операцией, а воркером
`OutboundDispatcher` — для выборки/доставки/ретеншена.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any
import uuid

from shared.database import AsyncPostgres

# Длительность lease-аренды claim'а (секунды). При выборке строки её
# next_attempt_at сдвигается на это значение в будущее — на это время строка
# скрыта от повторного захвата (своего и чужой реплики). Размер выбран как
# граница latency восстановления после краша: если реплика упала mid-delivery,
# in-flight строки снова станут видимы максимум через _CLAIM_LEASE_SECONDS.
# Это НЕ «время одной доставки» и не «время батча» — для штатно провалившихся
# строк mark_failed сам перезаписывает next_attempt_at backoff'ом, поэтому
# больший lease не нужен. Не зашиваем литерал в SQL — module-level константа.
_CLAIM_LEASE_SECONDS = 120


def _extract_locker_id(payload: dict[str, Any]) -> str | None:
    """Достать locker_id из payload для FK-привязки события к локеру.

    Берёт `payload['locker_id']` (telemetry/security/offline/command_history) или
    `payload['form_fields']['locker_id']` (media — там locker_id живёт во вложенных
    form_fields). Возвращает каноничную UUID-строку только если значение реально
    парсится как UUID; иначе None — так FK-вставка не падает на нестандартном
    payload (например, тестовом 'l'/'abc'), а событие просто остаётся без привязки
    вместо того, чтобы свалить весь enqueue на горячем пути.
    """
    candidate = payload.get("locker_id")
    if candidate is None:
        form_fields = payload.get("form_fields")
        if isinstance(form_fields, dict):
            candidate = form_fields.get("locker_id")
    if candidate is None:
        return None
    try:
        return str(uuid.UUID(str(candidate)))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(slots=True)
class OutboundEvent:
    id: int
    channel: str
    endpoint: str
    payload: dict[str, Any]
    created_at: datetime
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    delivered_at: datetime | None
    # Привязка к локеру (FK ON DELETE CASCADE) — проставляется при enqueue.
    locker_id: str | None = None
    # Маркер dead-letter: dispatcher паркует сюда после исчерпания попыток.
    dead_lettered_at: datetime | None = None


class OutboundRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def enqueue(
        self,
        channel: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        delay_seconds: float = 0.0,
        locker_id: str | None = None,
    ) -> int:
        """Поставить запись в очередь. Возвращает id записи.

        delay_seconds > 0 откладывает первую попытку доставки — полезно для
        медиа-событий, которым нужно дождаться появления security_event в БД.

        locker_id связывает событие с локером (FK ON DELETE CASCADE): при
        ФИЗИЧЕСКОМ удалении локера его недоставленные события уходят из очереди
        автоматически. Если явно не передан — извлекается из payload
        (`_extract_locker_id`: top-level или form_fields, с UUID-валидацией),
        поэтому продьюсерам менять call-site не нужно. None → событие без
        привязки (cascade-очистка к нему не применяется); FK не нарушается, т.к.
        продьюсеры кладут реальный существующий locker.id.
        """
        if locker_id is None:
            locker_id = _extract_locker_id(payload)
        if delay_seconds > 0:
            row = await self._db.fetch_one_strict(
                """
                INSERT INTO outbound_history_events
                    (channel, endpoint, payload, locker_id, next_attempt_at)
                VALUES (%s, %s, %s::jsonb, %s,
                        now() + (%s || ' seconds')::interval)
                RETURNING id
                """,
                (channel, endpoint, json.dumps(payload), locker_id,
                 str(delay_seconds)),
                read_only=False,
            )
        else:
            row = await self._db.fetch_one_strict(
                """
                INSERT INTO outbound_history_events
                    (channel, endpoint, payload, locker_id)
                VALUES (%s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (channel, endpoint, json.dumps(payload), locker_id),
                read_only=False,
            )
        return int(row["id"])

    async def fetch_pending(
        self,
        limit: int = 100,
        *,
        claim_lease_seconds: float = _CLAIM_LEASE_SECONDS,
        max_bytes: int | None = None,
    ) -> list[OutboundEvent]:
        """Атомарно заявить недоставленные записи, готовые к попытке отправки.

        Вместо голого SELECT выполняет `UPDATE ... RETURNING` поверх вложенного
        `SELECT ... FOR UPDATE SKIP LOCKED`. Это даёт безопасность при нескольких
        репликах: `SKIP LOCKED` гарантирует, что конкурентный
        `fetch_pending` другой реплики НЕ возьмёт уже заблокированные строки —
        он их просто пропустит и заберёт следующие. Никакого двойного захвата
        одного и того же события в рамках одного окна выборки.

        Голого `FOR UPDATE SKIP LOCKED` мало: row-lock снимается на commit
        транзакции, а commit происходит в момент возврата `fetch_all` — ДО
        медленной HTTP-доставки. Поэтому заявка делается персистентной через
        lease: `next_attempt_at` сдвигается на `claim_lease_seconds` в будущее.
        Этот маркер переживает commit, и повторная выборка (своя или чужой
        реплики) не вернёт заявленную строку, пока lease не истечёт.

        Важно про границы lease — он действует ПОСТРОЧНО, с момента claim'а
        каждой строки, и НЕ покрывает весь батч целиком:

          * lease скрывает каждую заявленную строку на `claim_lease_seconds`
            именно с момента её claim'а (а не с момента старта доставки батча);
          * он рассчитан с запасом на ОДНУ доставку, а не на последовательную
            доставку всего батча. При `limit=100` и доставке каждой строки до
            ~15 с (RetryHTTPClient: таймаут 5 с × 3 попытки) у последних строк
            батча lease (120 с) может истечь раньше, чем до них дойдёт очередь;
          * если так случилось — строка станет видимой и может быть заявлена и
            доставлена повторно. Это БЕЗОПАСНО: семантика at-least-once плюс
            идемпотентность downstream (history_api дедуплицирует по event_uid),
            поэтому повтор — не потеря данных, а в худшем случае лишняя отправка.

        Реальное назначение lease — не «продержать» строку весь HTTP-вызов, а:
          (а) не дать конкурентным репликам схватить одну строку дважды;
          (б) ограничить latency восстановления in-flight строк после краша
              реплики (упавшая mid-delivery строка снова видима максимум через
              `claim_lease_seconds`).

        Жизненный цикл заявленной строки:
          * успех    → `mark_delivered` ставит `delivered_at` (строка выбывает);
          * провал   → `mark_failed` перезаписывает `next_attempt_at` корректным
                       backoff'ом (lease становится неактуален);
          * краш реплики mid-delivery / истёкший lease в длинном батче → строка
                       снова видима и переотправляется (at-least-once + downstream
                       идемпотентность гарантируют отсутствие потери данных).

        `attempt_count` на claim НЕ инкрементируется намеренно: lease — это
        маркер видимости (кто-то взял строку в работу), а не неуспешная попытка.
        Инкремент попытки делает только `mark_failed` при реальном провале
        доставки.

        Порядок выдачи (FIFO): внешний `ORDER BY id` поверх CTE `claimed`
        обязателен. Вложенный `ORDER BY id` упорядочивает строки только для
        `LIMIT`, но `RETURNING` сам по себе НЕ сохраняет этот порядок (PostgreSQL
        не гарантирует порядок выдачи `RETURNING`). Без внешней сортировки FIFO
        становится недетерминированным — строки возвращаются в произвольном
        порядке. CTE выполняет атомарный claim+lease, внешний `SELECT ... ORDER
        BY id` восстанавливает детерминированную FIFO-выдачу.

        Бюджет по байтам (`max_bytes`): помимо построчного `limit`, батч
        ограничивается суммарным размером payload'ов. Кандидаты (до `limit`
        строк) лочатся `FOR UPDATE SKIP LOCKED`, но lease (и возврат payload в
        Python) получает только префикс, чей нарастающий `pg_column_size(payload)`
        укладывается в `max_bytes`; первая строка берётся всегда (иначе одиночное
        гигантское событие навсегда заклинило бы очередь). Это не даёт батчу из
        крупных media-событий (base64-фото) загрузить сотни МБ одним махом —
        ровно тот сценарий, что ронял dispatcher в OOM. `max_bytes=None`
        отключает бюджет. Незализенные (но залоченные) кандидаты освобождаются
        на commit и доступны следующему циклу.

        Dead-letter: запаркованные строки (`dead_lettered_at IS NOT NULL`) НЕ
        выбираются — dispatcher перестал их ретраить, они ждут ручного разбора.
        """
        rows = await self._db.fetch_all(
            """
            WITH candidates AS (
                SELECT id, pg_column_size(payload) AS sz
                FROM outbound_history_events
                WHERE delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND next_attempt_at <= now()
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            ),
            budgeted AS (
                SELECT id
                FROM (
                    SELECT id,
                           sum(sz) OVER (ORDER BY id) AS running,
                           row_number() OVER (ORDER BY id) AS rn
                    FROM candidates
                ) r
                WHERE running <= COALESCE(%s::bigint, running) OR rn = 1
            ),
            claimed AS (
                UPDATE outbound_history_events
                SET next_attempt_at = now() + (%s || ' seconds')::interval
                WHERE id IN (SELECT id FROM budgeted)
                RETURNING id, channel, endpoint, payload, created_at,
                          attempt_count, next_attempt_at, last_error,
                          delivered_at, locker_id, dead_lettered_at
            )
            SELECT id, channel, endpoint, payload, created_at,
                   attempt_count, next_attempt_at, last_error,
                   delivered_at, locker_id, dead_lettered_at
            FROM claimed
            ORDER BY id
            """,
            (limit, max_bytes, claim_lease_seconds),
            read_only=False,
        )
        return [
            OutboundEvent(
                id=r["id"],
                channel=r["channel"],
                endpoint=r["endpoint"],
                payload=r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]),
                created_at=r["created_at"],
                attempt_count=r["attempt_count"],
                next_attempt_at=r["next_attempt_at"],
                last_error=r["last_error"],
                delivered_at=r["delivered_at"],
                locker_id=str(r["locker_id"]) if r["locker_id"] is not None else None,
                dead_lettered_at=r["dead_lettered_at"],
            )
            for r in rows
        ]

    async def mark_delivered(self, event_id: int) -> None:
        await self._db.execute(
            "UPDATE outbound_history_events SET delivered_at = now() WHERE id = %s",
            (event_id,),
        )

    async def mark_failed(
        self,
        event_id: int,
        error: str,
        next_attempt_delay_seconds: float,
    ) -> None:
        """Пометить попытку доставки неуспешной и запланировать следующую."""
        await self._db.execute(
            """
            UPDATE outbound_history_events
            SET attempt_count = attempt_count + 1,
                last_error = %s,
                next_attempt_at = now() + (%s || ' seconds')::interval
            WHERE id = %s
            """,
            (error[:1000], next_attempt_delay_seconds, event_id),
        )

    async def mark_dead_lettered(self, event_id: int, error: str) -> None:
        """Запарковать событие в dead-letter после исчерпания попыток.

        Инкрементирует `attempt_count` (это была реальная — последняя —
        неуспешная попытка) и ставит `dead_lettered_at`. Запаркованная строка
        больше не выбирается `fetch_pending` (хвост очереди не растёт
        бесконечно) и видна на дашборде Outbox как сигнал «доставка сломалась».
        Не удаляем строку: payload и last_error сохраняются для ручного разбора
        и возможной ручной переотправки.
        """
        await self._db.execute(
            """
            UPDATE outbound_history_events
            SET attempt_count = attempt_count + 1,
                last_error = %s,
                dead_lettered_at = now()
            WHERE id = %s
            """,
            (error[:1000], event_id),
        )

    async def cleanup_delivered(self, older_than: timedelta) -> int:
        """Удалить успешно доставленные записи старше `older_than`. Возвращает количество удалённых."""
        seconds = int(older_than.total_seconds())
        # SELECT COUNT(*) FROM cte всегда даёт ровно одну строку — strict.
        row = await self._db.fetch_one_strict(
            """
            WITH deleted AS (
                DELETE FROM outbound_history_events
                WHERE delivered_at IS NOT NULL
                  AND delivered_at < now() - (%s || ' seconds')::interval
                RETURNING 1
            )
            SELECT COUNT(*) AS deleted FROM deleted
            """,
            (seconds,),
        )
        return int(row["deleted"])
