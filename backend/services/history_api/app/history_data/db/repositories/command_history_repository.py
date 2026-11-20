from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.command_history import CommandHistory, CommandStatus, CommandType

_DACITE_CONFIG = Config(cast=[CommandType, CommandStatus])

# COR-07: ранг статусов для монотонного продвижения при upsert. Команда не
# должна регрессировать (executed→sent) из-за out-of-order/retried доставки.
# Порядок: pending < sent < delivered < {executed, failed} (терминальные).
# Ранги заданы явным маппингом из enum CommandStatus (без bare-литералов).
_STATUS_RANK: dict[CommandStatus, int] = {
    CommandStatus.PENDING: 0,
    CommandStatus.SENT: 1,
    CommandStatus.DELIVERED: 2,
    CommandStatus.EXECUTED: 3,
    CommandStatus.FAILED: 3,
}

# Разрешённые SQL-выражения для _build_status_rank_case: только статические
# ссылки на колонку статуса (developer-only, не пользовательский ввод).
_STATUS_RANK_COLUMNS = frozenset({"EXCLUDED.status", "command_history.status"})


def _build_status_rank_case(column: str) -> str:
    """Сгенерировать SQL CASE, мапящий значение статуса в его ранг.

    `column` — SQL-выражение со значением статуса (например ``EXCLUDED.status``
    или ``command_history.status``). Значения статусов и ранги берутся из
    allowlist `_STATUS_RANK` — пользовательский ввод в SQL не интерполируется.
    Сам `column` тоже сверяется с allowlist `_STATUS_RANK_COLUMNS`, чтобы
    функция не стала вектором инъекции при добавлении новых вызовов.
    """
    if column not in _STATUS_RANK_COLUMNS:
        raise ValueError(f"Недопустимая ссылка на колонку статуса: {column!r}")
    whens = "\n".join(
        f"                    WHEN {column} = '{status.value}' THEN {rank}"
        for status, rank in _STATUS_RANK.items()
    )
    # Неизвестный статус трактуем как наименьший ранг (-1): он не сможет
    # перебить уже сохранённый известный статус.
    return f"CASE\n{whens}\n                    ELSE -1\n                END"


_TERMINAL: tuple[CommandStatus, ...] = (CommandStatus.EXECUTED, CommandStatus.FAILED)


def _terminal_conflict_sql() -> str:
    """Условие «терминальный статус сменяется другим терминальным».

    Такой upsert (например, поздний executed после failed по TTL) не должен
    протекать в таймстампы, result и error_message сохранённой записи.
    Литералы берутся из allowlist `_TERMINAL`.
    """
    terminal = ", ".join(f"'{status.value}'" for status in _TERMINAL)
    return (
        f"(command_history.status IN ({terminal}) "
        f"AND EXCLUDED.status IN ({terminal}) "
        f"AND EXCLUDED.status <> command_history.status)"
    )


class CommandHistoryOrder(StrEnum):
    """Допустимые варианты сортировки выборки истории команд.

    Значение enum используется как публичный контракт query-параметра
    `order`. Каждому значению соответствует ФИКСИРОВАННЫЙ SQL-фрагмент
    из `_ORDER_BY_SQL` — пользовательский ввод НИКОГДА не интерполируется
    в SQL, маппинг идёт только по allowlist.
    """

    CREATED_DESC = "created_at_desc"
    EXECUTED_ASC = "executed_at_asc"


# Allowlist: enum → фиксированный ORDER BY-фрагмент. Только эти два варианта.
_ORDER_BY_SQL: dict[CommandHistoryOrder, str] = {
    CommandHistoryOrder.CREATED_DESC: "created_at DESC",
    CommandHistoryOrder.EXECUTED_ASC: "executed_at ASC",
}


class CommandHistoryRepository:
    """
    Репозиторий для работы с историей команд.

    Хранит историю всех отправленных команд с денормализованными данными
    пользователей и шкафов для быстрого формирования отчетов.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def upsert(
        self,
        command_id: str,
        locker_id: str,
        command_type: str,
        status: str,
        locker_inventory_number: str | None = None,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        user_phone: str | None = None,
        user_full_name: str | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None
    ) -> CommandHistory:
        """
        Идемпотентно создать или обновить запись истории команды.

        Запись идентифицируется по `command_id` (UNIQUE). При первом вызове
        выполняется INSERT, при последующих — UPDATE, при этом:
        - `created_at` сохраняется от INSERT;
        - таймстампы переходов состояний (`sent_at`, `executed_at`, `failed_at`)
          выводятся из `status` и фиксируются на ПЕРВОМ таком переходе через COALESCE;
        - денормализованные поля и `result`/`error_message` дополняются по мере
          поступления данных и не затираются null/пустыми значениями.

        Returns:
            Актуальная запись истории после операции.
        """
        now = datetime.now()
        sent_at = now if status in (
            CommandStatus.SENT, CommandStatus.DELIVERED,
            CommandStatus.EXECUTED, CommandStatus.FAILED
        ) else None
        executed_at = now if status == CommandStatus.EXECUTED else None
        failed_at = now if status == CommandStatus.FAILED else None

        # COR-07: статус продвигается только вперёд по рангу. Если входящий
        # статус имеет ранг НЕ выше уже сохранённого (out-of-order/retry),
        # сохранённый статус остаётся неизменным.
        new_rank = _build_status_rank_case("EXCLUDED.status")
        existing_rank = _build_status_rank_case("command_history.status")
        status_set = (
            f"status = CASE\n"
            f"                    WHEN ({new_rank}) > ({existing_rank})\n"
            f"                    THEN EXCLUDED.status\n"
            f"                    ELSE command_history.status\n"
            f"                END"
        )
        conflict = _terminal_conflict_sql()

        # status_set встраивается конкатенацией (а не f-string), чтобы литералы
        # '{}' в CASE-ветках result/parameters не ломали парсер f-строк.
        query = (
            """
            INSERT INTO command_history (
                command_id, locker_id, locker_inventory_number,
                command_type, parameters, created_by,
                user_phone, user_full_name, created_at,
                status, result, error_message,
                sent_at, executed_at, failed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s)
            ON CONFLICT (command_id) DO UPDATE SET
                """
            + status_set
            + """,
                sent_at = COALESCE(command_history.sent_at, EXCLUDED.sent_at),
                executed_at = CASE WHEN """ + conflict + """ THEN command_history.executed_at
                    ELSE COALESCE(command_history.executed_at, EXCLUDED.executed_at) END,
                failed_at = CASE WHEN """ + conflict + """ THEN command_history.failed_at
                    ELSE COALESCE(command_history.failed_at, EXCLUDED.failed_at) END,
                result = CASE
                    WHEN """ + conflict + """ THEN command_history.result
                    WHEN EXCLUDED.result::text != '{}' THEN EXCLUDED.result
                    ELSE command_history.result
                END,
                error_message = CASE WHEN """ + conflict + """ THEN command_history.error_message
                    ELSE COALESCE(EXCLUDED.error_message, command_history.error_message) END,
                locker_inventory_number = COALESCE(EXCLUDED.locker_inventory_number, command_history.locker_inventory_number),
                parameters = CASE
                    WHEN EXCLUDED.parameters::text != '{}' THEN EXCLUDED.parameters
                    ELSE command_history.parameters
                END,
                created_by = COALESCE(EXCLUDED.created_by, command_history.created_by),
                user_phone = COALESCE(EXCLUDED.user_phone, command_history.user_phone),
                user_full_name = COALESCE(EXCLUDED.user_full_name, command_history.user_full_name)
            RETURNING *
        """
        )

        # INSERT … ON CONFLICT … DO UPDATE RETURNING всегда возвращает строку
        # (либо вставленную, либо обновлённую). Strict-вариант ловит регрессию,
        # если кто-то поменяет CTE на DO NOTHING.
        row = await self._db.fetch_one_strict(
            query,
            (
                command_id,
                locker_id,
                locker_inventory_number,
                command_type,
                json.dumps(parameters) if parameters else '{}',
                created_by,
                user_phone,
                user_full_name,
                status,
                json.dumps(result) if result else '{}',
                error_message,
                sent_at,
                executed_at,
                failed_at,
            ),
        )

        return from_dict(CommandHistory, row, config=_DACITE_CONFIG)

    async def get_by_filters(
        self,
        locker_id: str | None = None,
        user_id: int | None = None,
        command_type: str | None = None,
        status: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        executed_from: datetime | None = None,
        executed_to: datetime | None = None,
        order: CommandHistoryOrder = CommandHistoryOrder.CREATED_DESC,
        limit: int = 100,
        offset: int = 0
    ) -> list[CommandHistory]:
        """
        Получить историю команд с фильтрацией.

        Args:
            locker_id: UUID шкафа
            user_id: ID пользователя
            command_type: Тип команды
            status: Статус команды
            from_date: Начало диапазона по `created_at`
            to_date: Конец диапазона по `created_at`
            executed_from: Начало диапазона по `executed_at` (включительно)
            executed_to: Конец диапазона по `executed_at` (включительно)
            order: Порядок сортировки (allowlisted, см. CommandHistoryOrder)
            limit: Максимальное количество записей
            offset: Смещение для пагинации

        Returns:
            Список записей истории команд
        """
        query = "SELECT * FROM command_history WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if user_id:
            query += " AND created_by = %s"
            params.append(user_id)

        if command_type:
            query += " AND command_type = %s"
            params.append(command_type)

        if status:
            query += " AND status = %s"
            params.append(status)

        if from_date:
            query += " AND created_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND created_at <= %s"
            params.append(to_date)

        if executed_from:
            query += " AND executed_at >= %s"
            params.append(executed_from)

        if executed_to:
            query += " AND executed_at <= %s"
            params.append(executed_to)

        # order — enum, маппится в фиксированный SQL-фрагмент из allowlist;
        # пользовательский ввод не попадает в строку запроса.
        query += f" ORDER BY {_ORDER_BY_SQL[order]} LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(CommandHistory, row, config=_DACITE_CONFIG) for row in rows]

    async def get_count(
        self,
        locker_id: str | None = None,
        user_id: int | None = None,
        command_type: str | None = None,
        status: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        executed_from: datetime | None = None,
        executed_to: datetime | None = None
    ) -> int:
        """
        Получить количество записей истории с фильтрацией.

        Args:
            locker_id: UUID шкафа
            user_id: ID пользователя
            command_type: Тип команды
            status: Статус команды
            from_date: Начало диапазона по `created_at`
            to_date: Конец диапазона по `created_at`
            executed_from: Начало диапазона по `executed_at` (включительно)
            executed_to: Конец диапазона по `executed_at` (включительно)

        Returns:
            Количество записей
        """
        query = "SELECT COUNT(*) as count FROM command_history WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if user_id:
            query += " AND created_by = %s"
            params.append(user_id)

        if command_type:
            query += " AND command_type = %s"
            params.append(command_type)

        if status:
            query += " AND status = %s"
            params.append(status)

        if from_date:
            query += " AND created_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND created_at <= %s"
            params.append(to_date)

        if executed_from:
            query += " AND executed_at >= %s"
            params.append(executed_from)

        if executed_to:
            query += " AND executed_at <= %s"
            params.append(executed_to)

        row = await self._db.fetch_one(query, tuple(params) if params else ())
        return row['count'] if row else 0
