"""Relay command-result adapter.

Maps a relay `rep` (uint32 cmd_id + result) back onto the EXISTING locker-command
lifecycle: resolves the UUID via relay_cmd_id, flips status executed/failed through
the same CommandRepository methods the WS path uses, and enqueues the matching
CommandHistoryProducer transition. No SQL/HTTP is duplicated here.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum
import logging
from typing import Any
import uuid

from shared.enums import CommandStatus, CommandTransition, CommandType, RelayMessageOutcome, UserRole

from ..observability import command_metrics
from ..services.command_expiry import DEFAULT_COMMAND_TTL_S
from ..services.command_result_applier import CommandResultApplier
from ..services.history_producers.security_event_producer import CommandLateExecutionProducer
from .relay_protocol import RepResult

logger = logging.getLogger(__name__)

# Текстовое сообщение об ошибке для неуспешных rep (НЕ SUCCESS).
_REASON_NOT_SUPPORTED = "not_supported"
_REASON_FAILED = "failed"
# error_message команд, которые плата отвергла rep'ом (а не просто не ответила).
DEVICE_REJECTION_REASONS: tuple[str, ...] = (_REASON_FAILED, _REASON_NOT_SUPPORTED)

# Result-payload команды (locker_commands.result), который карточка команды
# отдаёт в API. Fix #3: раньше в колонку result писался пустой {} (документация
# обещала {success, message}, а по факту было пусто). Теперь SUCCESS кладёт
# статический {success:true, message}; FAILURE/NOT_SUPPORTED — {success:false,
# message:<reason>}. message для success намеренно generic: тип команды на этом
# этапе rep-обработки неизвестен без доп. запроса, а контракт с фронтендом —
# это форма {success, message} (см. services/locker_api/openapi/schemas/command.yaml).
_RESULT_EXECUTED: dict[str, Any] = {
    "success": True,
    "message": "Command executed successfully",
}

# Тег-схема уведомления об открытии — общая с WS-путём
# (device_handler._notify_locker_opened): notification_api матчит cancel по
# locker_id + event=locker_opened, поэтому форма обязана совпадать.
_NOTIF_EVENT_OPENED = "locker_opened"
NOTIF_DISMISS_BY = "operator_or_service"
_NOTIF_EVENT_LATE = "command_late_execution"
NOTIF_ROLES = [UserRole.OPERATOR.value, UserRole.ADMINISTRATOR.value]


class LateExecutionKind(StrEnum):
    AFTER_FAILURE = "after_failure"
    REPLAY = "replay"


# Retained CLOSE переисполняется платой при каждой переподписке на com/{id}
# (восстановление состояния) — это штатно и не тревога.
_SILENT_REPLAY_TYPES = frozenset({CommandType.CLOSE})

_LATE_EVENT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "scud:locker_api:command_late_execution")
_LATE_EVENT_DEDUP_WINDOW_S = 60
# Bounded LRU of event_uid'ов, по которым уже ушло уведомление (QoS-дубли rep).
_LATE_NOTIFIED_MAX = 1024

_COMMAND_LABELS_RU: dict[CommandType, str] = {
    CommandType.OPEN: "открыть",
    CommandType.CLOSE: "закрыть",
    CommandType.REBOOT: "перезагрузить",
    CommandType.CONFIG: "изменить конфигурацию",
    CommandType.RELAY_ON: "включить реле",
    CommandType.RELAY_OFF: "выключить реле",
    CommandType.RELAY_RESTART: "перезапустить реле",
}
_LATE_CONSEQUENCES_RU: dict[CommandType, str] = {
    CommandType.OPEN: "дверь могла открыться без оператора",
    CommandType.CLOSE: "дверь могла закрыться без оператора",
    CommandType.REBOOT: "шкаф мог перезагрузиться без оператора",
    CommandType.CONFIG: "конфигурация могла измениться без оператора",
    CommandType.RELAY_ON: "реле могло включиться без оператора",
    CommandType.RELAY_OFF: "реле могло выключиться без оператора",
    CommandType.RELAY_RESTART: "реле могло перезапуститься без оператора",
}
_LATE_TEMPLATES: dict[LateExecutionKind, str] = {
    LateExecutionKind.AFTER_FAILURE: (
        "Команда «{command}» выполнена шкафом после того, как была завершена с "
        "ошибкой (ответ шкафа через {delay_s} с после создания); в журнале команд "
        "остаётся «ОШИБКА»: {reason}"
    ),
    LateExecutionKind.REPLAY: (
        "Шкаф повторно выполнил команду «{command}» через {delay_s} с после её "
        "исполнения (вероятно, после переподключения платы)"
    ),
}


def _rejection_reason(result: RepResult) -> str:
    return _REASON_NOT_SUPPORTED if result == RepResult.NOT_SUPPORTED else _REASON_FAILED


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class RelayCommandResults:
    def __init__(
        self,
        command_repo: Any,
        history_producer: Any,
        locker_repo: Any = None,
        notification_client: Any = None,
        *,
        command_ttl_s: int = DEFAULT_COMMAND_TTL_S,
        late_execution_producer: CommandLateExecutionProducer | None = None,
    ) -> None:
        self._commands = command_repo
        self._history = history_producer
        self._ttl_s = command_ttl_s
        self._late = late_execution_producer
        self._late_notified: OrderedDict[str, None] = OrderedDict()
        # locker_repo + notification_client прокидываются из main.py для паритета
        # с WS-путём (door-state + persistent-уведомление). В части unit-тестов,
        # не покрывающих побочные эффекты OPEN/CLOSE, оба опущены — тогда
        # аппликатор не строится и door/notif не трогаются (как было раньше).
        self._locker_repo = locker_repo
        self._notifier = notification_client
        self._applier: CommandResultApplier | None = None
        # Строим аппликатор только при ПОЛНОЙ проводке: half-wired-конструкция
        # (door-repo есть, notifier нет — или наоборот) молча обновляла бы дверь,
        # роняя уведомления. Лучше не трогать ни то, ни другое, чем расходиться.
        if locker_repo is not None and notification_client is not None:
            self._applier = CommandResultApplier(
                locker_repo=locker_repo,
                notify_opened=self._notify_opened,
                notify_closed=self._notify_closed,
            )

    async def handle_rep(
        self,
        locker_id: str,
        cmd_id: int,
        result: RepResult,
        *,
        received_at: datetime | None = None,
    ) -> RelayMessageOutcome:
        received_at = received_at or datetime.now(UTC)
        command_id = await self._commands.resolve_relay_cmd_id(locker_id, cmd_id)
        if command_id is None:
            logger.warning("rep for unknown relay_cmd_id=%s on locker %s", cmd_id, locker_id)
            return RelayMessageOutcome.PROCESSED

        # CAS-методы репозитория требуют locker_id (cross-locker spoofing guard)
        # и меняют только активную команду: None — чужая, неизвестная или уже
        # терминальная (тогда _on_stale_rep).
        inventory: str | None = None
        if self._locker_repo is not None:
            locker = await self._locker_repo.get_by_id(locker_id)
            inventory = locker.inventory_number if locker is not None else None

        if result == RepResult.SUCCESS:
            command = await self._commands.update_status_to_executed(
                command_id, locker_id, result=_RESULT_EXECUTED,
            )
            if command is None:
                return await self._on_stale_rep(locker_id, cmd_id, command_id, result, received_at)
            logger.info("rep accepted: locker=%s relay_cmd_id=%s command=%s result=%s",
                        locker_id, cmd_id, command_id, result.name)
            command_metrics.record_transition(command.type, CommandTransition.EXECUTED)
            command_metrics.record_ack_latency(
                command.type, command.sent_at, command.executed_at
            )
            try:
                await self._history.publish_executed(
                    command_id=command.id,
                    locker_id=command.locker_id,
                    command_type=command.type,
                    result={"rep_result": result.name},
                    parameters=command.parameters,
                    created_by=command.created_by,
                    locker_inventory_number=inventory,
                )
            except Exception as exc:
                logger.error("Failed to enqueue command_history executed for %s: %s",
                             command_id, exc)

            # Паритет с WS-путём (SIMP-10): OPEN/CLOSE синхронизируют door-state
            # и persistent-уведомление через тот же общий аппликатор. None в
            # части unit-тестов, не покрывающих побочные эффекты.
            if self._applier is not None:
                await self._applier.apply_success(command)
            return RelayMessageOutcome.PROCESSED

        reason = _rejection_reason(result)
        command = await self._commands.update_status_to_failed(
            command_id, locker_id, reason,
            result={"success": False, "message": reason},
        )
        if command is None:
            return await self._on_stale_rep(locker_id, cmd_id, command_id, result, received_at)
        logger.info("rep accepted: locker=%s relay_cmd_id=%s command=%s result=%s",
                    locker_id, cmd_id, command_id, result.name)
        command_metrics.record_transition(command.type, CommandTransition.FAILED)
        command_metrics.record_ack_latency(
            command.type, command.sent_at, command.failed_at
        )
        try:
            await self._history.publish_failed(
                command_id=command.id,
                locker_id=command.locker_id,
                command_type=command.type,
                error_message=reason,
                parameters=command.parameters,
                created_by=command.created_by,
                locker_inventory_number=inventory,
            )
        except Exception as exc:
            logger.error("Failed to enqueue command_history failed for %s: %s",
                         command_id, exc)
        return RelayMessageOutcome.PROCESSED

    async def _on_stale_rep(
        self,
        locker_id: str,
        cmd_id: int,
        command_id: str,
        result: RepResult,
        received_at: datetime,
    ) -> RelayMessageOutcome:
        """rep по команде, которую CAS не изменил: статус терминальной не трогаем."""
        current = await self._commands.get_by_id(command_id)
        if current is None or current.locker_id != locker_id:
            logger.warning("rep(%s) for %s on locker %s: command not found",
                           result.name, command_id, locker_id)
            return RelayMessageOutcome.PROCESSED
        logger.info("stale rep ignored: locker=%s relay_cmd_id=%s command=%s status=%s rep=%s",
                    locker_id, cmd_id, command_id, current.status, result.name)
        if result != RepResult.SUCCESS and current.status == CommandStatus.FAILED:
            await self._commands.mark_device_rejected(
                command_id, locker_id, _rejection_reason(result), DEVICE_REJECTION_REASONS)
        kind = self._late_kind(current, result, received_at)
        if kind is not None:
            await self._emit_late_execution(current, cmd_id, kind, received_at)
        return RelayMessageOutcome.STALE_REP

    def _late_kind(
        self, current: Any, result: RepResult, received_at: datetime,
    ) -> LateExecutionKind | None:
        if result != RepResult.SUCCESS:
            return None
        if current.status == CommandStatus.FAILED:
            return LateExecutionKind.AFTER_FAILURE
        if (
            current.status == CommandStatus.EXECUTED
            and current.type not in _SILENT_REPLAY_TYPES
            and current.executed_at is not None
            and (received_at - current.executed_at).total_seconds() > self._ttl_s
        ):
            return LateExecutionKind.REPLAY
        return None

    async def _emit_late_execution(
        self, current: Any, cmd_id: int, kind: LateExecutionKind, received_at: datetime,
    ) -> None:
        """Security event command_late_execution + уведомление операторам. Не бросает."""
        anchor = current.created_at if kind == LateExecutionKind.AFTER_FAILURE else current.executed_at
        delay_s = int((received_at - anchor).total_seconds())
        bucket = int(received_at.timestamp()) // _LATE_EVENT_DEDUP_WINDOW_S
        event_uid = str(uuid.uuid5(_LATE_EVENT_NS, f"{current.id}:{kind}:{bucket}"))
        label = _COMMAND_LABELS_RU.get(current.type, str(current.type))
        if self._late is not None:
            description = _LATE_TEMPLATES[kind].format(
                command=label, delay_s=delay_s, reason=current.error_message or "",
            )
            try:
                await self._late.publish(
                    current.locker_id, None, [description],
                    event_uid=event_uid,
                    extra_metadata={
                        "late_kind": kind.value,
                        "command_id": str(current.id),
                        "command_type": str(current.type),
                        "relay_cmd_id": cmd_id,
                        "created_by": current.created_by,
                        "created_at": _iso(current.created_at),
                        "failed_at": _iso(current.failed_at),
                        "executed_at": _iso(current.executed_at),
                        "error_message": current.error_message,
                        "rep_received_at": received_at.isoformat(),
                        "delay_s": delay_s,
                    },
                )
            except Exception as exc:
                logger.error("Failed to enqueue command_late_execution for %s: %s",
                             current.id, exc)
        await self._notify_late_execution(current, kind, label, event_uid)

    async def _notify_late_execution(
        self, current: Any, kind: LateExecutionKind, label: str, event_uid: str,
    ) -> None:
        """Постоянное уведомление операторам (тот же путь, что «Шкаф открыт»).

        Один event_uid → не больше одного уведомления: ключ занимается
        синхронно до await, поэтому конкурентные QoS-дубли его не обойдут.
        """
        if self._notifier is None or event_uid in self._late_notified:
            return
        self._late_notified[event_uid] = None
        if len(self._late_notified) > _LATE_NOTIFIED_MAX:
            self._late_notified.popitem(last=False)
        consequence = _LATE_CONSEQUENCES_RU.get(current.type)
        tags = {
            "event": _NOTIF_EVENT_LATE,
            "locker_id": current.locker_id,
            "command_id": str(current.id),
            "late_kind": kind.value,
            "event_uid": event_uid,
            "dismiss_by": NOTIF_DISMISS_BY,
        }
        try:
            locker = (await self._locker_repo.get_by_id(current.locker_id)
                      if self._locker_repo is not None else None)
            display = locker_display(locker, current.locker_id)
            text = f"Шкаф {display} выполнил команду «{label}» с опозданием"
            text = f"{text} — {consequence}." if consequence else f"{text}."
            await self._notifier.create_persistent(text=text, tags=tags, roles=NOTIF_ROLES)
        except Exception as exc:
            self._late_notified.pop(event_uid, None)
            logger.error("Failed to create late-execution notification for %s: %s",
                         current.locker_id, exc)

    async def _notify_opened(self, command: Any) -> None:
        """Создать постоянное уведомление об открытии шкафа (best-effort).

        Форма тегов совпадает с WS-путём (device_handler._notify_locker_opened),
        чтобы notification_api корректно матчил последующий cancel по
        locker_id + event=locker_opened. Сбой notification_api не валит
        обработку rep — ошибка логируется и проглатывается."""
        if self._notifier is None:
            return
        locker_id = command.locker_id
        opened_by = str(command.created_by) if command.created_by else "system"
        # Операторам показываем человекочитаемый инвентарный номер (паритет с
        # WS-путём device_handler._notify_locker_opened). UUID locker_id остаётся
        # только в тегах для cancel-матчинга. Fallback на locker_id, если шкаф не
        # найден (например, soft-deleted между rep и lookup).
        locker = await self._locker_repo.get_by_id(locker_id)
        display = locker.inventory_number if locker is not None else locker_id
        text = (
            f"Шкаф {display} открыт. "
            f"Открыл пользователь с ID: {opened_by}. "
            f"Уведомление будет снято автоматически после закрытия шкафа, "
            f"либо вручную оператором или администратором."
        )
        tags = {
            "event": _NOTIF_EVENT_OPENED,
            "locker_id": locker_id,
            "opened_by_user_id": opened_by,
            "dismiss_by": NOTIF_DISMISS_BY,
        }
        try:
            await self._notifier.create_persistent(
                text=text,
                tags=tags,
                roles=NOTIF_ROLES,
            )
        except Exception as exc:
            logger.error("Failed to create locker-open notification for %s: %s",
                         locker_id, exc)

    async def _notify_closed(self, locker_id: str) -> None:
        """Снять уведомление об открытии шкафа (best-effort)."""
        if self._notifier is None:
            return
        try:
            await self._notifier.cancel_locker_open_notifications(locker_id)
        except Exception as exc:
            logger.error("Failed to cancel locker-open notifications for %s: %s",
                         locker_id, exc)


def locker_display(locker: Any, locker_id: str) -> str:
    """Инвентарный номер (и адрес) для текста уведомления; UUID, если шкаф не найден."""
    if locker is None:
        return locker_id
    address = getattr(locker, "location_address", None)
    inventory = str(locker.inventory_number)
    return f"{inventory} ({address})" if address else inventory
