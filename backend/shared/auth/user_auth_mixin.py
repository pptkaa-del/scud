"""Mixin аутентификации пользователей через ``X-Remote-User`` header.

Доверяет прокинутому gateway контексту (trust boundary), НЕ валидирует
пользователя в БД. Замешивается в handler-классы вида
``class MyHandler(JSONHandler, UserAuthMixin)``.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from tornado.web import Finish, RequestHandler

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    # Type-only base: декларирует, что миксин всегда замешан с RequestHandler.
    # В runtime UserAuthMixin наследуется от object (см. else-ветку) и MRO
    # выстраивается через классы-потребители вида
    # `class MyHandler(JSONHandler, UserAuthMixin)`.
    _UserAuthMixinBase = RequestHandler
else:
    _UserAuthMixinBase = object


class UserAuthMixin(_UserAuthMixinBase):
    """
    Mixin для аутентификации пользователей через X-Remote-User header.

    Извлекает user ID из заголовка и доверяет прокинутому контексту от gateway.
    НЕ выполняет валидацию пользователя в БД (trust boundary).

    Использование:
        class MyHandler(JSONHandler, UserAuthMixin):
            async def get(self):
                user_id = self.current_user_id
                ...
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._current_user_id: int | None = None

    def _write_unauthorized(self, detail: str) -> None:
        """Внутренний 401 в формате RFC 7807. Используется до того как
        полноценные `write_*` хелперы из BaseHandler становятся доступны.
        """
        from ..http import ProblemTitle, ProblemType
        self.set_status(401)
        self.set_header("Content-Type", "application/problem+json; charset=utf-8")
        self.write(json.dumps({
            "type": ProblemType.UNAUTHORIZED,
            "title": ProblemTitle.UNAUTHORIZED,
            "status": 401,
            "detail": detail,
        }))

    async def prepare(self) -> None:
        """
        Асинхронная подготовка запроса: извлекаем user ID из X-Remote-User.

        Note: НЕ проверяет существование пользователя в БД.
        Доверяет gateway, что ID валидный и авторизованный.

        Raises:
            Finish: При ошибке авторизации для прерывания обработки запроса
        """
        # Cooperative chain: дать вышестоящим prepare() (особенно
        # JSONHandler.prepare(), который ставит _metrics_start) отработать
        # ДО того, как мы потенциально кинем Finish. Иначе Prometheus-
        # histogram пишет 0.0 длительность для ВСЕХ auth-fail и happy-path
        # запросов через UserAuthMixin (поскольку JSONHandler в MRO дальше
        # по цепочке и без super().prepare() сюда никогда не дойдёт).
        result = super().prepare()
        if result is not None:
            await result

        user_id_str = self.request.headers.get("X-Remote-User")
        logger.debug(f"UserAuthMixin: Authenticating user from X-Remote-User header: {user_id_str}")

        if not user_id_str:
            logger.warning(f"Missing X-Remote-User header for {self.request.method} {self.request.uri}")
            self._write_unauthorized("Missing X-Remote-User header")
            raise Finish

        try:
            self._current_user_id = int(user_id_str)
            logger.debug(f"UserAuthMixin: User authenticated, user_id={self._current_user_id}")
        except (ValueError, TypeError):
            logger.warning(f"Invalid X-Remote-User header value: {user_id_str}")
            self._write_unauthorized("Invalid X-Remote-User header")
            raise Finish from None

    @property
    def current_user_id(self) -> int | None:
        """Возвращает ID текущего пользователя из X-Remote-User"""
        return self._current_user_id

    @property
    def remote_role(self) -> str:
        """Сырое значение заголовка ``X-Remote-Role`` (gateway → downstream).

        Возвращает строку как прислал gateway, либо ``""`` если заголовок
        отсутствует (тот же default, что у прямого ``headers.get(..., "")``).
        Регистр/пробелы НЕ нормализуются здесь — вызывающие, которым нужна
        матч-форма, делают ``.strip().lower()`` сами (default-deny при пустой
        роли сохраняется: ``""`` не матчит ни одну запись матрицы).
        """
        return self.request.headers.get("X-Remote-Role", "")
