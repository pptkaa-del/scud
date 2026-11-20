import asyncio
import logging

from shared.database import AsyncPostgres

from ..notification_data.db.repositories.outbound_max import OutboundMaxRepository

logger = logging.getLogger(__name__)


class NotificationScheduler:
    """
    Фоновый планировщик обслуживания подсистемы уведомлений.

    Единственная периодическая задача — чистка Max-outbox: просроченные/
    терминальные OTP-записи не должны оставаться plaintext в БД (SEC-18).

    Паттерн аналогичен StateMonitor / CommandQueue в locker_api.
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db
        self._outbox = OutboundMaxRepository(db)
        self._running = False

    async def start(self, check_interval: int = 30) -> None:
        """Запустить фоновый процесс планировщика"""
        self._running = True
        logger.info("Notification scheduler started")

        while self._running:
            try:
                await self._purge_outbound_max()
                await asyncio.sleep(check_interval)
            except Exception as e:
                logger.error(f"Error in notification scheduler: {e}", exc_info=True)
                await asyncio.sleep(check_interval)

        logger.info("Notification scheduler stopped")

    def stop(self) -> None:
        """Остановить фоновый процесс планировщика"""
        self._running = False

    async def _purge_outbound_max(self) -> None:
        """Удалить просроченные / старые терминальные записи Max-outbox."""
        await self._outbox.purge_expired()
