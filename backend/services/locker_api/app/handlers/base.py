"""
Базовые handlers для Locker API.

Использует shared компоненты с mixins для аутентификации.
"""

import logging

from shared.access_control import LockerScope, require_locker_grant, resolve_locker_scope
from shared.auth import ProblemDetailsHandler, UserAuthMixin

logger = logging.getLogger(__name__)


class BaseHandler(UserAuthMixin, ProblemDetailsHandler):
    """
    Базовый handler для Control Gateway (REST API управления).

    Наследует:
    - UserAuthMixin: Аутентификация через X-Remote-User (без валидации в БД, первый для правильного MRO!)
    - ProblemDetailsHandler: JSON сериализация + RFC 7807 Problem Details хелперы
      (включает `get_json_body_as` для типобезопасной десериализации DTO).

    Важно: Миксины идут первыми в наследовании, чтобы их prepare() вызывались в правильном порядке по MRO.
    """

    def locker_scope(self) -> LockerScope:
        """Область видимости шкафов для текущего запроса (списочные ручки).

        Пара к ``ensure_locker_grant``: тот гейтит ОДИН шкаф, эта отвечает,
        какие строки вообще показывать. ACM гейтит коллекцию только по роли
        (в её пути нет UUID) и не может отфильтровать строки чужой БД, поэтому
        область строит locker_api — из того же ``X-Remote-Lockers``.

        Пустая область — не ошибка: worker без Допусков получает 200 и пустой
        список. 403 здесь сломал бы session-scoped probe интеграционных тестов
        (``tests/integration/conftest.py`` принимает только 200/401), а запрос
        совсем без личности отсекает ``UserAuthMixin.prepare`` (401) ещё до
        handler'а — «нет личности» остаётся громкой ошибкой, а не тихой пустотой.
        """
        return resolve_locker_scope(
            self.request.headers.get("X-Remote-Lockers"), self.remote_role,
        )

    def ensure_locker_grant(self, locker_id: str) -> bool:
        """Defense-in-depth (SEC-02): worker должен иметь Допуск на шкаф.

        ACM уже выполняет URL-level проверку Допуска, но locker_api не доверяет
        этому слепо: сверяем канонизированный ``locker_id`` со списком из
        ``X-Remote-Lockers`` (canonicalization обеих сторон закрывает обход
        через не-канонический вид UUID). operator/administrator и full_access
        (``X-Remote-Lockers == "*"``) пропускаются — они управляются ролевыми
        правилами ACM.

        Returns:
            True если доступ разрешён. Иначе пишет 403 ProblemDetails и
            возвращает False — вызывающий handler обязан немедленно выйти.
        """
        role = self.remote_role
        remote_lockers = self.request.headers.get("X-Remote-Lockers")
        if require_locker_grant(remote_lockers, role, locker_id):
            return True
        logger.warning(
            "Locker grant denied: user_id=%s role=%r locker=%s",
            getattr(self, "current_user_id", None), role, locker_id,
        )
        self.write_forbidden(f"No active access grant for locker {locker_id}")
        return False
