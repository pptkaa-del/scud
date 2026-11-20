"""
Базовые исключения для inter-service HTTP клиентов.

Каждый клиент upstream-сервиса (UserApiClient, NotificationClient, ACM's
auth_api client) при 5xx или сетевом сбое должен бросать наследника
`UpstreamUnavailableError`, а не молча возвращать None/[]/False — иначе
operator не отличит «нормальный 401» от «auth_api лежит» и мониторинг
(структурный лог + Zabbix-триггер на скачок 5xx) не зафиксирует деградацию инфры.

`ProblemDetailsHandler.write_error` ловит этот класс централизованно и
отдаёт RFC 7807 503; caller-ам не нужны точечные `try/except` в каждом
handler'е.
"""

from __future__ import annotations


class UpstreamUnavailableError(Exception):
    """Upstream HTTP-сервис лежит (5xx или сетевой сбой).

    Перехватывается в `ProblemDetailsHandler.write_error` → 503 с upstream-тегом.
    Используется inter-service клиентами вместо silent return None.
    """

    def __init__(
        self,
        message: str,
        *,
        upstream: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.upstream = upstream
        self.status = status
