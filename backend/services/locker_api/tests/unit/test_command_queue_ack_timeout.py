"""Unit-тесты для CommandQueue.start_ack_timeout_loop.

Sweep делегирует обе ветки CommandRepository (COR-15): не исчерпавшие бюджет →
reset_timed_out_sent_commands (pending, retry_count+1), исчерпавшие →
fail_exhausted_sent_commands (терминальный failed + публикация истории).
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.command_queue import CommandQueue


def _patched_repo(reset_rows=None, failed=None):
    repo = MagicMock()
    repo.reset_timed_out_sent_commands = AsyncMock(return_value=reset_rows or [])
    repo.fail_exhausted_sent_commands = AsyncMock(return_value=failed or [])
    return repo


class TestAckTimeoutLoop(unittest.TestCase):
    def test_calls_both_repo_methods_with_timeout(self):
        """Цикл вызывает обе ветки sweep с корректным timeout_min."""
        async def run():
            db = MagicMock()
            queue = CommandQueue()
            repo = _patched_repo()

            async def stop_after_one_iter():
                await asyncio.sleep(0.05)
                queue.stop_ack_timeout_loop()

            with patch('app.locker_data.db.repositories.command.CommandRepository',
                       return_value=repo):
                stop_task = asyncio.create_task(stop_after_one_iter())
                await queue.start_ack_timeout_loop(db, interval_s=0.01, timeout_min=5)
                await stop_task

            repo.reset_timed_out_sent_commands.assert_awaited_with(5)
            # fail-ветка получает timeout_min + причину провала.
            repo.fail_exhausted_sent_commands.assert_awaited()
            self.assertEqual(repo.fail_exhausted_sent_commands.await_args.args[0], 5)

        asyncio.run(run())

    def test_stop_terminates_loop(self):
        """stop_ack_timeout_loop корректно завершает цикл."""
        async def run():
            db = MagicMock()
            queue = CommandQueue()
            repo = _patched_repo()

            with patch('app.locker_data.db.repositories.command.CommandRepository',
                       return_value=repo):
                task = asyncio.create_task(
                    queue.start_ack_timeout_loop(db, interval_s=0.01)
                )
                await asyncio.sleep(0.05)
                queue.stop_ack_timeout_loop()
                await asyncio.wait_for(task, timeout=1.0)

            self.assertFalse(queue._ack_timeout_running)

        asyncio.run(run())

    def test_db_error_does_not_break_loop(self):
        """Транзиентная ошибка БД не ломает цикл, последующие итерации работают."""
        async def run():
            db = MagicMock()
            queue = CommandQueue()
            repo = _patched_repo()
            # Первая итерация падает в reset, дальше — успех.
            repo.reset_timed_out_sent_commands = AsyncMock(
                side_effect=[Exception("transient"), [], []]
            )

            async def stop_after():
                await asyncio.sleep(0.1)
                queue.stop_ack_timeout_loop()

            with patch('app.locker_data.db.repositories.command.CommandRepository',
                       return_value=repo):
                stop_task = asyncio.create_task(stop_after())
                await queue.start_ack_timeout_loop(db, interval_s=0.02, timeout_min=5)
                await stop_task

            # Несмотря на первую ошибку, цикл продолжил работу.
            self.assertGreater(repo.reset_timed_out_sent_commands.await_count, 1)

        asyncio.run(run())

    def test_custom_timeout_min_passed_to_query(self):
        """Кастомный timeout_min корректно передаётся в обе ветки sweep."""
        async def run():
            db = MagicMock()
            queue = CommandQueue()
            repo = _patched_repo()

            async def stop_after_one_iter():
                await asyncio.sleep(0.05)
                queue.stop_ack_timeout_loop()

            with patch('app.locker_data.db.repositories.command.CommandRepository',
                       return_value=repo):
                stop_task = asyncio.create_task(stop_after_one_iter())
                await queue.start_ack_timeout_loop(db, interval_s=0.01, timeout_min=15)
                await stop_task

            repo.reset_timed_out_sent_commands.assert_awaited_with(15)
            self.assertEqual(repo.fail_exhausted_sent_commands.await_args.args[0], 15)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
