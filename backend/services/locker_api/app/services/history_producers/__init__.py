"""Продьюсеры событий в history_api.

Все продьюсеры публикуют через персистентную очередь
(`outbound_history_events`), которую обрабатывает `OutboundDispatcher`.
Это гарантирует доставку даже при недоступности history_api: сбой
сетевого вызова не теряется, а превращается в новую попытку через backoff.
"""

from ._base import BaseHistoryProducer

__all__ = ["BaseHistoryProducer"]
