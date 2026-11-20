"""Unit-тесты MaxBotPoller — long-polling воркер для bot_started.

В проде `poll_updates` — это HTTP long-polling, который сам блочит await
на ~30s. В моках, чтобы сохранить семантику «yield to event loop между
итерациями», подменяем метод собственной async-функцией с
``await asyncio.sleep(0)`` — AsyncMock(side_effect=async-fn) НЕ ожидает
возвращаемую корутину, поэтому event loop не получает яvno yield и
stop_event-сеттер из соседней корутины не пропускается.
"""

import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.notification_data.db.repositories.outbound_max import OutboundMaxMessage
from app.services.max_bot_client import (
    MaxRecipientUnreachableError,
    MaxUpdate,
    MaxUpdates,
    MaxUser,
)
from app.services.max_bot_poller import MaxBotPoller
from app.services.user_api_binding_client import BindMaxResponse, BindMaxResult
from shared.http import UpstreamUnavailableError


def _make_outbox_msg(message_id: int = 1, user_id: int = 42, attempts: int = 0) -> OutboundMaxMessage:
    return OutboundMaxMessage(
        id=message_id,
        user_id=user_id,
        max_user_id=None,
        channel="max",
        address=None,
        body="Код входа: 123456",
        purpose="otp_login",
        attempts=attempts,
        sent_at=None,
        failed_at=None,
        last_error=None,
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(minutes=5),
    )


def _make_bot_started(user_id: int = 99, start_payload: str = "token-abc") -> MaxUpdate:
    # Реальная схема: payload — строка start-параметра, user_id во вложенном user.
    return MaxUpdate(
        update_type="bot_started",
        timestamp=1700000000,
        user=MaxUser(user_id=user_id),
        payload=start_payload,
    )


def _make_outbox_repo() -> MagicMock:
    outbox_repo = MagicMock()
    outbox_repo.set_max_user_id_for_pending = AsyncMock()
    outbox_repo.select_pending_for_user = AsyncMock(return_value=[])
    outbox_repo.mark_sent = AsyncMock()
    outbox_repo.mark_failed = AsyncMock()
    outbox_repo.increment_attempt = AsyncMock()
    return outbox_repo


def _make_state_repo(initial_marker: str | None = None) -> MagicMock:
    state_repo = MagicMock()
    state_repo.get_marker = AsyncMock(return_value=initial_marker)
    state_repo.set_marker = AsyncMock()
    return state_repo


def _make_poller(
    *,
    max_client,
    binding_client,
    state_repo,
    outbox_repo,
) -> MaxBotPoller:
    return MaxBotPoller(
        max_client=max_client,
        binding_client=binding_client,
        state_repo=state_repo,
        outbox_repo=outbox_repo,
        poll_timeout_s=1,
        backoff_on_error_s=0,
    )


class _PollerCallTracker:
    """Простой stub для max_client.poll_updates с реальным async yield.

    AsyncMock + side_effect=async-fn не работает: AsyncMock не awaitит
    возвращаемую корутину. Поэтому делаем callable сами.

    Чтобы тесты не крутили busy-wait цикл (``while call_count < N: await
    sleep``), трекер ведёт событийную сигнализацию: после каждого вызова
    выставляет `asyncio.Event` для всех «ожидающих» порогов call_count.
    Тест берёт `reached(n)` — awaitable, который резолвится как только
    poll_updates вызвали >= n раз.
    """

    def __init__(self, sequence: list):
        self._sequence = list(sequence)
        self.call_count = 0
        self.last_kwargs: dict = {}
        self._thresholds: dict[int, asyncio.Event] = {}

    def reached(self, target: int) -> asyncio.Event:
        """Event, который выставится когда call_count достигнет target."""
        event = self._thresholds.setdefault(target, asyncio.Event())
        if self.call_count >= target:
            event.set()
        return event

    async def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        for target, event in self._thresholds.items():
            if self.call_count >= target:
                event.set()
        await asyncio.sleep(0)
        if not self._sequence:
            # Эмулируем бесконечный пустой long-polling после исчерпания.
            return MaxUpdates(updates=[], marker=None)
        nxt = self._sequence.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class TestPollerLifecycle(unittest.TestCase):

    def test_stop_event_terminates_loop(self):
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([MaxUpdates(updates=[], marker="m-1")])
            max_client.poll_updates = tracker

            binding_client = MagicMock()
            state_repo = _make_state_repo()
            outbox_repo = MagicMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.05)
                stop.set()

            await asyncio.gather(poller.run(stop), stop_soon())
            self.assertGreaterEqual(state_repo.set_marker.await_count, 1)
        asyncio.run(run())

    def test_upstream_unavailable_continues_polling(self):
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                UpstreamUnavailableError("boom", upstream="max_bot_api"),
                MaxUpdates(updates=[], marker="m-1"),
            ])
            max_client.poll_updates = tracker

            binding_client = MagicMock()
            state_repo = _make_state_repo()
            outbox_repo = MagicMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_two_polls():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_two_polls())
            self.assertGreaterEqual(tracker.call_count, 2)
        asyncio.run(run())


class TestBotStartedFlow(unittest.TestCase):

    def test_bot_started_binds_and_delivers(self):
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(user_id=99, start_payload="tok")],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                return_value=BindMaxResponse(BindMaxResult.BOUND, user_id=42),
            )

            state_repo = _make_state_repo()
            outbox_repo = MagicMock()
            outbox_repo.set_max_user_id_for_pending = AsyncMock()
            outbox_repo.select_pending_for_user = AsyncMock(
                return_value=[_make_outbox_msg(message_id=7, user_id=42)],
            )
            outbox_repo.mark_sent = AsyncMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                # Ждём начала второго poll: к этому моменту первая итерация
                # (bind + delivery + persist marker) гарантированно завершена.
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            binding_client.bind_max_user.assert_awaited_with(
                invite_token="tok", max_user_id=99,
            )
            outbox_repo.set_max_user_id_for_pending.assert_awaited_with(42, 99)
            max_client.send_text.assert_awaited_once()
            sent_user_id, _ = max_client.send_text.await_args.args
            self.assertEqual(sent_user_id, "99")
            outbox_repo.mark_sent.assert_awaited_with(7)
        asyncio.run(run())

    def test_token_invalid_does_not_call_outbox(self):
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(start_payload="expired")],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                return_value=BindMaxResponse(BindMaxResult.TOKEN_INVALID),
            )

            state_repo = _make_state_repo()
            outbox_repo = MagicMock()
            outbox_repo.set_max_user_id_for_pending = AsyncMock()
            outbox_repo.select_pending_for_user = AsyncMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                # Ждём начала второго poll: к этому моменту первая итерация
                # обработки update'ов гарантированно завершена.
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())
            outbox_repo.set_max_user_id_for_pending.assert_not_awaited()
            outbox_repo.select_pending_for_user.assert_not_awaited()
            max_client.send_text.assert_not_awaited()
        asyncio.run(run())

    def test_delivery_recipient_unreachable_marks_failed(self):
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started()],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock(
                side_effect=MaxRecipientUnreachableError(
                    "blocked", max_user_id=99, status=404,
                ),
            )

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                return_value=BindMaxResponse(BindMaxResult.BOUND, user_id=42),
            )

            state_repo = _make_state_repo()
            outbox_repo = MagicMock()
            outbox_repo.set_max_user_id_for_pending = AsyncMock()
            outbox_repo.select_pending_for_user = AsyncMock(
                return_value=[_make_outbox_msg(message_id=7)],
            )
            outbox_repo.mark_failed = AsyncMock()
            outbox_repo.mark_sent = AsyncMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                # Ждём начала второго poll: к этому моменту первая итерация
                # обработки update'ов гарантированно завершена.
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())
            outbox_repo.mark_failed.assert_awaited_once()
            outbox_repo.mark_sent.assert_not_awaited()
        asyncio.run(run())

    def test_oversized_start_payload_ignored(self):
        """SEC-21: раздутый start_payload не идёт в user_api (treated as ignored)."""
        async def run():
            from app.services.max_bot_poller import _MAX_INVITE_TOKEN_LEN

            oversized = "A" * (_MAX_INVITE_TOKEN_LEN + 1)
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(start_payload=oversized)],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock()

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            # Раздутый payload не должен ни биндиться, ни доставляться.
            binding_client.bind_max_user.assert_not_awaited()
            outbox_repo.set_max_user_id_for_pending.assert_not_awaited()
            max_client.send_text.assert_not_awaited()
            # Это permanent-ignore (DONE) → marker продвигается.
            state_repo.set_marker.assert_awaited_with("m-1")
        asyncio.run(run())

    def test_unknown_update_type_ignored(self):
        async def run():
            irrelevant = MaxUpdate(
                update_type="message_created",
                timestamp=1700000000,
                user=MaxUser(user_id=99),
            )
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(updates=[irrelevant], marker="m-1"),
            ])
            max_client.poll_updates = tracker

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock()

            state_repo = _make_state_repo()
            outbox_repo = MagicMock()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                # Ждём начала второго poll: к этому моменту первая итерация
                # обработки update'ов гарантированно завершена.
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())
            binding_client.bind_max_user.assert_not_awaited()
        asyncio.run(run())


class TestMarkerAdvanceOnFailure(unittest.TestCase):
    """COR-12: marker не должен продвигаться за транзиентно-упавший update.

    Транзиентный сбой (user_api недоступен → UpstreamUnavailableError) при
    обработке bot_started НЕ должен двигать marker — иначе Max не пере-
    доставит событие, привязка не произойдёт и OTP зависнут навсегда.
    Перманентный сбой (TOKEN_INVALID/CONFLICT) — наоборот, marker обязан
    продвинуться, иначе poison-update заклинит поллер навсегда.
    """

    def test_transient_failure_does_not_advance_marker(self):
        """(a) Единственный update падает транзиентно → marker НЕ сохраняется."""
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(user_id=99, start_payload="tok")],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                side_effect=UpstreamUnavailableError("user_api down", upstream="user_api"),
            )

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            # marker НЕ должен продвинуться за упавший update — Max
            # пере-доставит его на следующем цикле.
            state_repo.set_marker.assert_not_awaited()
        asyncio.run(run())

    def test_transient_failure_on_middle_advances_only_succeeded_prefix(self):
        """(b) Транзиентный сбой на втором из трёх → marker не продвигается за него.

        Первый update успешен, второй падает транзиентно, третий не должен
        даже обрабатываться (а если обрабатывается — всё равно не двигаем
        marker). marker остаётся на старом значении (None), чтобы Max
        пере-доставил начиная с упавшего.
        """
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[
                        _make_bot_started(user_id=1, start_payload="ok-1"),
                        _make_bot_started(user_id=2, start_payload="transient-2"),
                        _make_bot_started(user_id=3, start_payload="never-3"),
                    ],
                    marker="m-batch",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            def _bind(*, invite_token, max_user_id):
                if invite_token == "transient-2":
                    raise UpstreamUnavailableError("user_api down", upstream="user_api")
                return BindMaxResponse(BindMaxResult.BOUND, user_id=max_user_id)

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(side_effect=_bind)

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            # Любой транзиентный сбой в батче запрещает продвижение marker:
            # повтор начнётся со старого marker и пере-доставит упавший update.
            state_repo.set_marker.assert_not_awaited()
            # Третий update не должен биндиться — мы останавливаемся на упавшем.
            bound_tokens = [
                call.kwargs["invite_token"]
                for call in binding_client.bind_max_user.await_args_list
            ]
            self.assertNotIn("never-3", bound_tokens)
        asyncio.run(run())

    def test_permanent_failure_advances_marker(self):
        """(c) Перманентный сбой (TOKEN_INVALID) → marker ПРОДВИГАЕТСЯ.

        poison-update не должен заклинить поллер.
        """
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(start_payload="expired")],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                return_value=BindMaxResponse(BindMaxResult.TOKEN_INVALID),
            )

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            # Перманентный сбой пропускаем — marker обязан продвинуться.
            state_repo.set_marker.assert_awaited_with("m-1")
        asyncio.run(run())

    def test_all_succeed_advances_marker(self):
        """(d) Все update'ы успешны → marker продвигается до конца батча (без регресса)."""
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[
                        _make_bot_started(user_id=1, start_payload="ok-1"),
                        _make_bot_started(user_id=2, start_payload="ok-2"),
                    ],
                    marker="m-end",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                side_effect=lambda *, invite_token, max_user_id: BindMaxResponse(
                    BindMaxResult.BOUND, user_id=max_user_id,
                ),
            )

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                await tracker.reached(2).wait()
                stop.set()

            await asyncio.gather(poller.run(stop), stop_after_first_poll())

            state_repo.set_marker.assert_awaited_with("m-end")
        asyncio.run(run())


class TestPoisonUpdateRobustness(unittest.TestCase):
    """COR-12: неожиданное исключение в обработке update не валит поллер.

    Любое исключение, кроме ``UpstreamUnavailableError`` (транзиентного), при
    обработке одного update должно логироваться и трактоваться как DONE —
    poison-update отбрасывается, marker продвигается, fire-and-forget таск
    ``run()`` продолжает работать, а не умирает молча до рестарта процесса.
    """

    def test_unexpected_exception_is_done_and_advances_marker(self):
        """RuntimeError из binding-клиента → outcome DONE, marker продвигается."""
        async def run():
            max_client = MagicMock()
            tracker = _PollerCallTracker([
                MaxUpdates(
                    updates=[_make_bot_started(user_id=99, start_payload="poison")],
                    marker="m-1",
                ),
            ])
            max_client.poll_updates = tracker
            max_client.send_text = AsyncMock()

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                side_effect=RuntimeError("unexpected boom"),
            )

            state_repo = _make_state_repo()
            outbox_repo = _make_outbox_repo()

            poller = _make_poller(
                max_client=max_client,
                binding_client=binding_client,
                state_repo=state_repo,
                outbox_repo=outbox_repo,
            )
            stop = asyncio.Event()

            async def stop_after_first_poll():
                # Ждём начала второго poll: первая итерация (poison + persist
                # marker) к этому моменту гарантированно завершена. Сам факт,
                # что мы дождались второго poll, доказывает: run() НЕ умер.
                await tracker.reached(2).wait()
                stop.set()

            with self.assertLogs("app.services.max_bot_poller", level="ERROR") as logs:
                await asyncio.gather(poller.run(stop), stop_after_first_poll())

            # poison-update отброшен → marker продвинут до конца батча.
            state_repo.set_marker.assert_awaited_with("m-1")
            # run() пережил исключение и продолжил опрашивать.
            self.assertGreaterEqual(tracker.call_count, 2)
            # Ошибка залогирована, без тела/OTP — только id/тип.
            logged = "\n".join(logs.output)
            self.assertIn("poison update", logged.lower())
            self.assertNotIn("Код входа", logged)

        asyncio.run(run())

    def test_handle_update_returns_done_on_unexpected_exception(self):
        """Прямой контракт _handle_update: RuntimeError → DONE (не пробрасывается)."""
        async def run():
            from app.services.max_bot_poller import _UpdateOutcome

            binding_client = MagicMock()
            binding_client.bind_max_user = AsyncMock(
                side_effect=RuntimeError("unexpected boom"),
            )

            poller = _make_poller(
                max_client=MagicMock(),
                binding_client=binding_client,
                state_repo=_make_state_repo(),
                outbox_repo=_make_outbox_repo(),
            )

            outcome = await poller._handle_update(
                _make_bot_started(user_id=99, start_payload="poison"),
            )
            self.assertIs(outcome, _UpdateOutcome.DONE)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
