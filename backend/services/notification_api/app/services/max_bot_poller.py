"""Фоновый long-polling воркер для Max-бота.

Опрашивает ``GET /updates`` Max API, ловит события ``bot_started``,
привязывает ``users.max_user_id`` через user_api `/internal/user/by-invite`,
доставляет накопившиеся OTP-записи из ``outbound_max_messages``.

Жизненный цикл:
1. Загружаем marker из БД (`bot_poller_state`).
2. В цикле, пока не выставлен `stop_event`:
   - poll_updates(marker)
   - для каждого update_type=bot_started с непустым start_payload:
     - bind_max_user → user_id (либо 404/409 → лог, идём дальше)
     - set_max_user_id_for_pending(user_id, max_user_id)
     - delivery всех pending OTP (best-effort, MaxRecipientUnreachable
       помечает как failed, UpstreamUnavailableError — increment_attempt
       и оставляем pending)
   - сохраняем новый marker в БД.
3. UpstreamUnavailableError при poll → backoff и retry.
"""
from __future__ import annotations

import asyncio
import contextlib
from enum import Enum, auto
import logging

from shared.http import UpstreamUnavailableError

from ..notification_data.db.repositories.bot_poller_state import BotPollerStateRepository
from ..notification_data.db.repositories.outbound_max import OutboundMaxRepository
from .max_bot_client import (
    MaxBotClient,
    MaxRecipientUnreachableError,
    MaxUpdate,
)
from .user_api_binding_client import (
    BindMaxResult,
    UserApiBindingClient,
)

logger = logging.getLogger(__name__)


_DEFAULT_POLL_TIMEOUT_S = 30
_BACKOFF_ON_ERROR_S = 5
_MAX_DELIVERY_ATTEMPTS = 3

# Верхняя граница длины start_payload → invite_token (SEC-21). Атакующий
# контролирует start-параметр deep-link и мог бы прислать произвольно длинный
# payload, который воркер пересылает дословно в user_api. Легитимный токен —
# secrets.token_urlsafe(32) (~43 символа); 256 с запасом перекрывает его и
# отсекает раздутый payload ДО POST в user_api. user_api дублирует ту же
# защиту на своей стороне (internal_max_binding.MAX_INVITE_TOKEN_LEN).
_MAX_INVITE_TOKEN_LEN = 256


class _UpdateOutcome(Enum):
    """Исход обработки одного update — решает судьбу marker (COR-12).

    ``DONE`` покрывает и успех, и перманентный отказ (TOKEN_INVALID/CONFLICT):
    в обоих случаях update «отработан» и marker можно двигать дальше — иначе
    poison-update заклинит поллер навсегда. ``RETRY`` — транзиентный сбой
    (user_api недоступен): update НЕ отработан, marker двигать нельзя, иначе
    Max не пере-доставит событие и привязка/OTP потеряются безвозвратно.
    """

    DONE = auto()
    RETRY = auto()


class MaxBotPoller:
    def __init__(
        self,
        *,
        max_client: MaxBotClient,
        binding_client: UserApiBindingClient,
        state_repo: BotPollerStateRepository,
        outbox_repo: OutboundMaxRepository,
        poll_timeout_s: int = _DEFAULT_POLL_TIMEOUT_S,
        backoff_on_error_s: int = _BACKOFF_ON_ERROR_S,
    ) -> None:
        self._max_client = max_client
        self._binding = binding_client
        self._state = state_repo
        self._outbox = outbox_repo
        self._poll_timeout = poll_timeout_s
        self._backoff_on_error = backoff_on_error_s

    async def run(self, stop_event: asyncio.Event) -> None:
        """Главный цикл. Завершается когда stop_event выставлен."""
        marker = await self._state.get_marker()
        logger.info("Max bot poller started (marker=%s)", marker)
        while not stop_event.is_set():
            try:
                updates = await self._max_client.poll_updates(
                    marker=marker, long_poll_timeout_s=self._poll_timeout,
                )
            except UpstreamUnavailableError as exc:
                logger.warning("Max poll failed, retrying in %ds: %s", self._backoff_on_error, exc)
                await self._sleep_or_stop(stop_event, self._backoff_on_error)
                continue
            except Exception:
                logger.exception("Max poll raised unexpected error")
                await self._sleep_or_stop(stop_event, self._backoff_on_error)
                continue

            # COR-12: marker двигаем только если ВЕСЬ батч отработан без
            # транзиентных сбоев. На первом RETRY останавливаем обработку
            # батча и НЕ продвигаем marker — Max пере-доставит батч начиная
            # со старого marker на следующем цикле. Уже успешно обработанные
            # ранее update'ы пере-обработаются идемпотентно: invite-токен
            # одноразовый (user_api помечает used_at атомарно при первой
            # привязке), повторный bind вернёт TOKEN_INVALID → безопасный
            # no-op, без двойной привязки.
            batch_fully_processed = True
            for upd in updates.updates:
                if stop_event.is_set():
                    batch_fully_processed = False
                    break
                outcome = await self._handle_update(upd)
                if outcome is _UpdateOutcome.RETRY:
                    batch_fully_processed = False
                    break

            if (
                batch_fully_processed
                and updates.marker is not None
                and updates.marker != marker
            ):
                marker = updates.marker
                try:
                    await self._state.set_marker(marker)
                except Exception:
                    logger.exception("Failed to persist poller marker")
        logger.info("Max bot poller stopped")

    async def _handle_update(self, upd: MaxUpdate) -> _UpdateOutcome:
        """Обработать один update.

        Returns:
            ``_UpdateOutcome.DONE`` — update отработан (успех, перманентный
            отказ либо неожиданное исключение): marker можно двигать дальше.
            ``_UpdateOutcome.RETRY`` — транзиентный сбой (user_api недоступен),
            marker двигать нельзя, нужен повтор на следующем цикле.

        Неожиданное исключение (НЕ ``UpstreamUnavailableError``) логируется и
        трактуется как DONE: один poison-update отбрасывается, а не валит
        fire-and-forget таск поллера (COR-12). Лог пишем только по id/типу —
        тело сообщения/OTP не логируем (no-OTP-logging discipline).
        """
        try:
            return await self._dispatch_update(upd)
        except UpstreamUnavailableError as exc:
            logger.error(
                "user_api binding failed for max_user_id=%s: %s", upd.max_user_id, exc,
            )
            return _UpdateOutcome.RETRY
        except Exception:
            logger.exception(
                "Unexpected error handling update (type=%s max_user_id=%s) — dropping poison update",
                upd.update_type, upd.max_user_id,
            )
            return _UpdateOutcome.DONE

    async def _dispatch_update(self, upd: MaxUpdate) -> _UpdateOutcome:
        """Чистая логика обработки update; исключения ловит ``_handle_update``."""
        if upd.update_type != "bot_started":
            return _UpdateOutcome.DONE
        start_payload = upd.start_payload
        max_user_id = upd.max_user_id
        if not start_payload or max_user_id is None:
            logger.info(
                "bot_started without start_payload or user_id (type=%s) — ignored",
                upd.update_type,
            )
            return _UpdateOutcome.DONE
        if len(start_payload) > _MAX_INVITE_TOKEN_LEN:
            # SEC-21: раздутый start_payload не пересылаем в user_api.
            # Логируем только длину, не сам payload.
            logger.warning(
                "bot_started with oversized start_payload (len=%d) — ignored",
                len(start_payload),
            )
            return _UpdateOutcome.DONE

        result = await self._binding.bind_max_user(
            invite_token=start_payload, max_user_id=max_user_id,
        )

        if result.result == BindMaxResult.TOKEN_INVALID:
            logger.info(
                "bot_started: invite token invalid/expired (max_user_id=%s)", upd.user_id,
            )
            return _UpdateOutcome.DONE
        if result.result == BindMaxResult.CONFLICT:
            logger.warning(
                "bot_started: max_user_id=%s already bound to another user", upd.user_id,
            )
            return _UpdateOutcome.DONE

        assert result.user_id is not None
        await self._deliver_pending(result.user_id, max_user_id)
        return _UpdateOutcome.DONE

    async def _deliver_pending(self, user_id: int, max_user_id: int) -> None:
        """Проставить max_user_id в outbox и попробовать отправить активные коды."""
        await self._outbox.set_max_user_id_for_pending(user_id, max_user_id)
        pending = await self._outbox.select_pending_for_user(user_id)
        for msg in pending:
            target = msg.max_user_id or max_user_id
            try:
                await self._max_client.send_text(str(target), msg.body)
            except MaxRecipientUnreachableError as exc:
                logger.warning(
                    "Max recipient unreachable on delivery: outbox_id=%s status=%s",
                    msg.id, exc.status,
                )
                await self._outbox.mark_failed(msg.id, f"max returned {exc.status}")
            except UpstreamUnavailableError as exc:
                logger.warning(
                    "Max API unavailable on delivery outbox_id=%s — leaving pending: %s",
                    msg.id, exc,
                )
                # Не финализируем как failed: повтор будет на следующем
                # bot_started или /internal/api/v1/otp/send.
                if msg.attempts + 1 >= _MAX_DELIVERY_ATTEMPTS:
                    await self._outbox.mark_failed(msg.id, f"max api retry budget exhausted: {exc}")
                else:
                    await self._outbox.increment_attempt(msg.id, str(exc))
            else:
                await self._outbox.mark_sent(msg.id)

    async def _sleep_or_stop(self, stop_event: asyncio.Event, seconds: int) -> None:
        """Спит указанное время или раньше, если stop_event выставлен."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
