from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import json
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, is_unique_violation

from ..models.command import CommandStatus, CommandType, LockerCommand

_DACITE_CONFIG = Config(cast=[CommandStatus, CommandType])

_ACTIVE_STATUSES: list[str] = [
    CommandStatus.PENDING.value, CommandStatus.SENT.value, CommandStatus.DELIVERED.value,
]

# Сколько раз пере-аллоцировать relay_cmd_id при unique-violation на
# ux_locker_commands_relay_cmd_id (конкурентный inline + queue-processor).
_RELAY_CMD_ID_MAX_RETRIES = 5


class CommandRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def get_by_id(self, command_id: str) -> LockerCommand | None:
        row = await self._db.fetch_one(
            """
            SELECT *
            FROM locker_commands
            WHERE id = %s
            """,
            (command_id,),
        )
        if not row:
            return None

        return from_dict(LockerCommand, row, config=_DACITE_CONFIG)

    async def create(
            self,
            locker_id: str,
            command_type: CommandType,
            created_by: int | None,
            parameters: dict[str, Any] | None = None,
            max_retries: int = 3,
    ) -> LockerCommand:
        """Создать новую команду"""
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO locker_commands (locker_id,
                                         type,
                                         created_by,
                                         parameters,
                                         max_retries)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            RETURNING *
            """,
            (locker_id, command_type, created_by, json.dumps(parameters or {}), max_retries),
        )
        return from_dict(LockerCommand, row, config=_DACITE_CONFIG)

    async def get_pending_commands(
        self,
        *,
        ttl_s: int,
        locker_id: str | None = None,
        limit: int = 500,
    ) -> list[LockerCommand]:
        """Получить команды в статусе pending, ещё не истёкшие по TTL.

        Без ``locker_id`` (обход всей очереди в `command_queue` фоновом таске)
        LIMIT обязателен: при 10K шкафов × 3 команды в очереди иначе
        получаем ~15 МБ строк в память. Дефолтный батч — 500.
        """
        if locker_id:
            rows = await self._db.fetch_all(
                """
                SELECT *
                FROM locker_commands
                WHERE status = 'pending'
                  AND locker_id = %s
                  AND retry_count < max_retries
                  AND created_at > now() - make_interval(secs => %s)
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (locker_id, ttl_s, limit),
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT *
                FROM locker_commands
                WHERE status = 'pending'
                  AND retry_count < max_retries
                  AND created_at > now() - make_interval(secs => %s)
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (ttl_s, limit),
            )

        return [from_dict(LockerCommand, row, config=_DACITE_CONFIG) for row in rows]

    async def update_status_to_sent(
        self, command_id: str, *, ttl_s: int,
    ) -> LockerCommand | None:
        """Заявить (claim) команду как 'sent' через conditional CAS.

        Переход pending→sent выполняется ТОЛЬКО если строка ещё `pending`
        (`WHERE ... AND status='pending'`). Это делает отправку at-most-once
        на тик: конкурентные inline-доставка и queue-processor оба зовут
        update_status_to_sent, но лишь один CAS выигрывает — второй получает 0
        строк (RETURNING пустой → None). Вызывающий код обязан CLAIM-before-send:
        send_command только если claim вернул строку (см. COR-02/18).

        Заодно закрывает регрессию executed→sent: если устройство уже
        ACK'нуло команду (status='executed'), запоздавший inline-апдейт не
        найдёт pending-строку и НЕ откатит её обратно в sent.

        retry_count здесь НЕ инкрементится: успешная отправка не должна жечь
        retry-бюджет. Инкремент происходит только на genuine re-attempt — в
        ack-timeout reset (см. start_ack_timeout_loop).

        Истёкшая по TTL команда (created_at старше ttl_s) не заявляется никогда.

        Возвращает заявленную (sent) команду или None, если строки в pending не
        было (claim проиграл / команда уже не pending / истекла).
        """
        row = await self._db.fetch_one(
            """
            UPDATE locker_commands
            SET status = 'sent',
                sent_at = now()
            WHERE id = %s
              AND status = 'pending'
              AND created_at > now() - make_interval(secs => %s)
            RETURNING *
            """,
            (command_id, ttl_s),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(LockerCommand, row, config=_DACITE_CONFIG)

    async def update_status_to_executed(
            self,
            command_id: str,
            locker_id: str,
            result: dict[str, Any] | None = None,
    ) -> LockerCommand | None:
        """Обновить статус команды на 'executed' с результатом.

        locker_id обязателен и проверяется в WHERE: команда обновляется только
        если она принадлежит указанному локеру. Защищает от cross-locker
        spoofing: скомпрометированное устройство A не может пометить чужую
        команду устройства B как executed, прислав её UUID в command_result.

        Терминальная строка (executed/failed) не меняется никогда (CAS по
        активным статусам); None — не найдена, чужой локер или уже терминальна.
        """
        row = await self._db.fetch_one(
            """
            UPDATE locker_commands
            SET status = 'executed',
                executed_at = now(),
                result = %s::jsonb
            WHERE id = %s AND locker_id = %s
              AND status = ANY(%s::command_status[])
            RETURNING *
            """,
            (json.dumps(result or {}), command_id, locker_id, _ACTIVE_STATUSES),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(LockerCommand, row, config=_DACITE_CONFIG)

    async def update_status_to_failed(
            self,
            command_id: str,
            locker_id: str,
            error_message: str,
            result: dict[str, Any] | None = None,
    ) -> LockerCommand | None:
        """Обновить статус команды на 'failed' с сообщением об ошибке.

        ``result`` — опциональный JSON-payload для колонки ``result`` (карточка
        команды в API). Fix #3: relay-путь кладёт ``{success:false, message}`` в
        пару к человекочитаемому ``error_message``. None → ``{}`` (back-compat:
        прочие call-site статуса result не пишут).

        Cross-locker scope: см. docstring update_status_to_executed.

        Терминальная строка (executed/failed) не меняется никогда (CAS по
        активным статусам); None — не найдена, чужой локер или уже терминальна.
        """
        row = await self._db.fetch_one(
            """
            UPDATE locker_commands
            SET status = 'failed',
                failed_at = now(),
                error_message = %s,
                result = %s::jsonb
            WHERE id = %s AND locker_id = %s
              AND status = ANY(%s::command_status[])
            RETURNING *
            """,
            (error_message, json.dumps(result or {}), command_id, locker_id,
             _ACTIVE_STATUSES),
            read_only=False,
        )
        if not row:
            return None

        return from_dict(LockerCommand, row, config=_DACITE_CONFIG)

    async def mark_device_rejected(
        self, command_id: str, locker_id: str, reason: str,
        rejected_reasons: Sequence[str],
    ) -> bool:
        """Отказ платы, пришедший после того, как команда уже завершилась ошибкой.

        Причина (TTL, исчерпаны повторы) заменяется на отказ платы, чтобы
        is_open_authorized не считал отвергнутую команду действующей.
        """
        row = await self._db.fetch_one(
            """
            UPDATE locker_commands
            SET error_message = %(reason)s
            WHERE id = %(command_id)s AND locker_id = %(locker_id)s
              AND status = %(failed)s::command_status
              AND COALESCE(error_message, '') <> ALL(%(rejected)s)
            RETURNING id
            """,
            {
                "reason": reason,
                "command_id": command_id,
                "locker_id": locker_id,
                "failed": CommandStatus.FAILED.value,
                "rejected": list(rejected_reasons),
            },
            read_only=False,
        )
        return row is not None

    async def expire_stale_commands(
        self,
        *,
        ttl_s: int,
        error_message: str,
        outbox_channel: str,
        outbox_endpoint: str,
        locker_id: str | None = None,
        limit: int = 500,
    ) -> list[LockerCommand]:
        """Перевести активные команды старше ttl_s в терминальный failed.

        Один атомарный statement: переход и outbox-строка command_history
        коммитятся вместе. idempotent=True безопасен — повтор после
        неоднозначного обрыва не найдёт уже терминальных строк и вернёт [].
        Ключи payload зеркалят CommandHistoryProducer._payload.
        """
        rows = await self._db.fetch_all(
            """
            WITH expired AS (
                UPDATE locker_commands
                SET status = %(failed)s::command_status,
                    failed_at = now(),
                    error_message = %(reason)s,
                    result = jsonb_build_object('success', false, 'message', %(reason)s::text)
                WHERE id IN (
                    SELECT id FROM locker_commands
                    WHERE status = ANY(%(active)s::command_status[])
                      AND created_at < now() - make_interval(secs => %(ttl)s)
                      AND (%(locker_id)s::uuid IS NULL OR locker_id = %(locker_id)s::uuid)
                    ORDER BY created_at
                    LIMIT %(limit)s
                    FOR UPDATE SKIP LOCKED)
                  AND status = ANY(%(active)s::command_status[])
                RETURNING *
            ), outbox AS (
                INSERT INTO outbound_history_events (channel, endpoint, payload, locker_id)
                SELECT %(channel)s, %(endpoint)s,
                       jsonb_build_object(
                           'command_id', e.id::text,
                           'locker_id', e.locker_id::text,
                           'command_type', e.type::text,
                           'status', e.status::text,
                           'parameters', COALESCE(e.parameters, '{}'::jsonb),
                           'error_message', e.error_message)
                       || CASE WHEN e.created_by IS NULL THEN '{}'::jsonb
                               ELSE jsonb_build_object('created_by', e.created_by) END,
                       e.locker_id
                FROM expired e
            )
            SELECT * FROM expired ORDER BY created_at
            """,
            {
                "failed": CommandStatus.FAILED.value,
                "reason": error_message,
                "active": _ACTIVE_STATUSES,
                "ttl": ttl_s,
                "locker_id": locker_id,
                "limit": limit,
                "channel": outbox_channel,
                "endpoint": outbox_endpoint,
            },
            read_only=False,
            idempotent=True,
        )
        return [from_dict(LockerCommand, row, config=_DACITE_CONFIG) for row in rows]

    async def is_open_authorized(
        self, locker_id: str, at: datetime, rejected_reasons: Sequence[str],
    ) -> bool:
        """Была ли действующей командой замка на момент ``at`` «Открыть» (протокол §7.1).

        Действующая — последняя опубликованная в com/{id} OPEN/CLOSE: плата держит
        её retained и выполняет при каждом переподключении, поэтому rep для этого
        не нужен (он может потеряться или прийти позже stat). Не считаются
        команды, которые плата явно отвергла rep'ом. Нет такой команды — замок
        закрыт.
        """
        row = await self._db.fetch_one(
            """
            SELECT type = %(open)s::command_type AS authorized
            FROM locker_commands
            WHERE locker_id = %(locker_id)s
              AND type = ANY(%(lock_types)s::command_type[])
              AND sent_at <= %(at)s
              AND NOT (status = %(failed)s::command_status
                       AND COALESCE(error_message, '') = ANY(%(rejected)s))
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            {
                "open": CommandType.OPEN.value,
                "locker_id": locker_id,
                "lock_types": [CommandType.OPEN.value, CommandType.CLOSE.value],
                "at": at,
                "failed": CommandStatus.FAILED.value,
                "rejected": list(rejected_reasons),
            },
        )
        return bool(row and row["authorized"])

    async def release_claim(self, command_id: str) -> None:
        """Вернуть заявленную, но не отправленную команду в pending (CAS из sent)."""
        await self._db.execute(
            """
            UPDATE locker_commands
            SET status = 'pending',
                sent_at = NULL
            WHERE id = %s
              AND status = 'sent'
            """,
            (command_id,),
            idempotent=True,
        )

    async def reset_timed_out_sent_commands(
        self, timeout_min: int,
    ) -> list[dict[str, str]]:
        """Сбросить просроченные 'sent' команды с ОСТАВШИМСЯ бюджетом в 'pending'.

        Часть ack-timeout sweep (COR-15): команда, провисевшая в 'sent' дольше
        timeout_min без ACK, и у которой ПОСЛЕ инкремента retry_count остаётся
        попытка (retry_count + 1 < max_retries), возвращается в очередь
        (status='pending', retry_count+1). retry_count инкрементится именно
        здесь — genuine re-attempt, а не на каждом claim'е (см. COR-02/18).

        Claim-based: CAS-guard `status='sent'` защищает executed/failed-строки
        от случайного отката. Исчерпавшие бюджет НЕ трогаются — их забирает
        fail_exhausted_sent_commands (терминальный failed).

        Возвращает список dict с полями id, type для каждой переведённой строки.
        """
        return await self._db.fetch_all(
            """
            UPDATE locker_commands
            SET status = 'pending',
                retry_count = retry_count + 1
            WHERE status = 'sent'
              AND sent_at < NOW() - make_interval(mins => %s)
              AND retry_count + 1 < max_retries
            RETURNING id, type
            """,
            (timeout_min,),
            read_only=False,
        )

    async def fail_exhausted_sent_commands(
            self, timeout_min: int, error_message: str,
    ) -> list[LockerCommand]:
        """Перевести просроченные 'sent' команды с ИСЧЕРПАННЫМ бюджетом в 'failed'.

        Вторая половина ack-timeout sweep (COR-15): если команда провисела в
        'sent' дольше timeout_min без ACK и инкремент retry_count исчерпал бы
        бюджет (retry_count + 1 >= max_retries), переотправлять её больше
        некуда — переводим в ТЕРМИНАЛЬНЫЙ 'failed'. Иначе get_pending_commands
        (фильтр retry_count < max_retries) никогда её не подберёт, и она зависнет
        в 'pending' навсегда (зомби-команда: вечная запись в очереди в UI).

        Claim-based: CAS-guard `status='sent'` гарантирует, что только этот
        sweep переведёт строку в failed (конкурентный device-ACK успел бы
        перевести её в executed → строка уже не sent → не клобберится).

        Возвращает обновлённые (failed) строки — вызывающий ack-timeout loop
        публикует по ним publish_failed в command_history.
        """
        rows = await self._db.fetch_all(
            """
            UPDATE locker_commands
            SET status = 'failed',
                failed_at = now(),
                retry_count = retry_count + 1,
                error_message = %s
            WHERE status = 'sent'
              AND sent_at < NOW() - make_interval(mins => %s)
              AND retry_count + 1 >= max_retries
            RETURNING *
            """,
            (error_message, timeout_min),
            read_only=False,
        )
        return [from_dict(LockerCommand, row, config=_DACITE_CONFIG) for row in rows]

    async def count_pending(self) -> int:
        """Количество команд с status='pending' и незакончившимся retry-бюджетом.

        Используется периодическим тасом refresh_pending_commands_gauge для
        заполнения scud_command_pending_total. Тот же фильтр, что у
        get_pending_commands (status='pending' AND retry_count < max_retries).
        """
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM locker_commands "
            "WHERE status = %s AND retry_count < max_retries",
            (CommandStatus.PENDING.value,),
        )
        return int(row["n"]) if row else 0

    async def get_commands_by_locker(
            self,
            locker_id: str,
            limit: int = 100,
            offset: int = 0,
    ) -> list[LockerCommand]:
        """Получить историю команд для конкретного шкафа"""
        rows = await self._db.fetch_all(
            """
            SELECT *
            FROM locker_commands
            WHERE locker_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (locker_id, limit, offset),
        )
        return [from_dict(LockerCommand, row, config=_DACITE_CONFIG) for row in rows]

    async def count_by_locker(self, locker_id: str) -> int:
        """Общее количество команд шкафа (для пагинации)."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS count FROM locker_commands WHERE locker_id = %s",
            (locker_id,),
        )
        return int(row["count"]) if row else 0

    async def allocate_relay_cmd_id(self, command_id: str, locker_id: str) -> int:
        """Назначить per-locker монотонный uint32 relay_cmd_id и сохранить его.

        relay_cmd_id — uint32, который отправляется плате в com/{id} и эхом
        возвращается в rep (locker_commands.id — UUID, int(uuid) недопустим).
        MAX+1 с переносом монотонен в пределах локера; партиальный уникальный
        индекс ux_locker_commands_relay_cmd_id защищает от коллизий. Назначение
        НЕ сериализовано: inline-доставка и queue-processor могут аллоцировать
        конкурентно (см. абзац «Collision-safe» ниже) — именно поэтому здесь
        retry на unique-violation, а не просто MAX+1.

        Идемпотентно: если у команды уже есть relay_cmd_id (повторная отправка
        после ack-timeout), переиспользуем существующее значение — иначе
        запоздавший rep со старым id уже не разрезолвится. Аллокация происходит
        только когда relay_cmd_id IS NULL (COALESCE на присвоении).

        Перенос на верхней границе: при MAX = 4294967295 следующий id = 1, а не
        `MAX %% 4294967295 + 1` (который тоже даёт 1, но ещё ДО исчерпания
        диапазона — при MAX=4294967295 он вернул бы 1 при живой строке с id=1 →
        unique-violation). CASE WHEN ... >= MAX даёт корректный wrap.

        Collision-safe: каждый вызов — один autocommit-statement (общей
        транзакции нет), поэтому inline-доставка и queue-processor могут
        одновременно прочитать одинаковый MAX(relay_cmd_id) под READ COMMITTED и
        получить unique-violation на ux_locker_commands_relay_cmd_id. Ловим её и
        пере-вычисляем MAX+1 (до _RELAY_CMD_ID_MAX_RETRIES попыток). COALESCE на
        присвоении делает операцию идемпотентной: если у команды уже есть
        relay_cmd_id (другой аллокатор успел раньше / повтор после ack-timeout),
        UPDATE его НЕ бампит и unique-violation не возникает.
        """
        last_exc: Exception | None = None
        for _ in range(_RELAY_CMD_ID_MAX_RETRIES):
            try:
                row = await self._db.fetch_one(
                    """
                    UPDATE locker_commands SET relay_cmd_id = COALESCE(relay_cmd_id, (
                        SELECT CASE
                            WHEN MAX(relay_cmd_id) >= 4294967295 THEN 1
                            ELSE COALESCE(MAX(relay_cmd_id), 0) + 1
                        END
                        FROM locker_commands WHERE locker_id = %s
                    ))
                    WHERE id = %s
                    RETURNING relay_cmd_id
                    """,
                    (locker_id, command_id),
                    read_only=False,
                )
            except Exception as exc:
                # Только unique-violation (тот же MAX+1 занял конкурентный
                # аллокатор) пере-вычисляется; прочие ошибки пробрасываем.
                # is_unique_violation скрывает psycopg2 от repo (драйвер живёт
                # в shared.database).
                if not is_unique_violation(exc):
                    raise
                last_exc = exc
                continue
            if not row:
                raise ValueError(
                    f"command {command_id} not found for relay_cmd_id allocation")
            return int(row["relay_cmd_id"])
        raise RuntimeError(
            f"relay_cmd_id allocation for command {command_id} exhausted "
            f"{_RELAY_CMD_ID_MAX_RETRIES} retries on conflict"
        ) from last_exc

    async def resolve_relay_cmd_id(self, locker_id: str, relay_cmd_id: int) -> str | None:
        """Найти UUID команды по (locker_id, relay_cmd_id) из rep-эхо."""
        row = await self._db.fetch_one(
            """
            SELECT id
            FROM locker_commands
            WHERE locker_id = %s AND relay_cmd_id = %s
            """,
            (locker_id, relay_cmd_id),
        )
        return str(row["id"]) if row else None
