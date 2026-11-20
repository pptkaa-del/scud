"""
Базовый handler для User API.

Использует shared компоненты с mixins для аутентификации.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tornado.web import Finish

from app.user_data.db.models.user import User
from app.user_data.db.repositories.user import UserRepository
from shared.auth import ProblemDetailsHandler, UserAuthMixin

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    # `BaseHandler` mixes UserAuthWithValidationMixin with
    # ProblemDetailsHandler. Declaring both as type-time bases lets mypy
    # resolve `self.db` / `self.write_unauthorized` (from
    # ProblemDetailsHandler) and `self._current_user_id` (from
    # UserAuthMixin) through the mixin without affecting MRO at runtime.
    class _MixinBase(UserAuthMixin, ProblemDetailsHandler):
        pass
else:
    _MixinBase = UserAuthMixin


class UserAuthWithValidationMixin(_MixinBase):
    """
    Mixin для аутентификации пользователей с валидацией в БД.

    Расширяет UserAuthMixin и дополнительно проверяет существование
    пользователя в БД, загружая полный объект User.

    Использование:
        class MyHandler(UserAuthWithValidationMixin, ProblemDetailsHandler):
            async def get(self):
                user = self.current_user  # полный объект User
                ...
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._current_user: User | None = None

    async def prepare(self) -> None:
        """
        Извлекает user ID и загружает полный объект User из БД.

        Raises:
            Finish: При ошибке валидации пользователя в БД
        """
        await super().prepare()

        if self._current_user_id is None:
            logger.debug("UserAuthWithValidationMixin: Skipping user validation, no user_id")
            return

        logger.debug(f"UserAuthWithValidationMixin: Loading user {self._current_user_id} from DB")

        repo = UserRepository(self.db)
        self._current_user = await repo.get_by_id(self._current_user_id)

        if not self._current_user:
            logger.warning(f"User {self._current_user_id} not found in database")
            # `write_unauthorized` предоставляется ProblemDetailsHandler
            # в финальной иерархии BaseHandler.
            self.write_unauthorized("User not found")
            raise Finish

        logger.debug(f"UserAuthWithValidationMixin: User {self._current_user.id} loaded successfully")

    @property
    def current_user(self) -> User | None:
        """Возвращает полный объект User"""
        logger.debug(f"current_user property accessed, returning: {self._current_user}")
        return self._current_user

    @current_user.setter
    def current_user(self, value: User | None) -> None:
        # Tornado's RequestHandler.current_user is a read/write property;
        # providing a setter keeps the override compatible without losing
        # the typed return on `.current_user`.
        self._current_user = value


def caller_role(handler: UserAuthWithValidationMixin) -> str | None:
    """Нормализованная роль вызывающего из ``current_user`` (загружен из БД).

    Источник истины — роль из объекта ``current_user``, загруженного из БД в
    ``prepare()`` (X-Remote-User валидируется против БД). Это надёжнее
    заголовка ``X-Remote-Role``: роль нельзя «обнулить», просто не прислав
    заголовок.

    Возвращает ``None``, если ``current_user`` отсутствует — вызывающие хелперы
    матрицы прав (``can_modify_target`` / ``can_assign_role``) трактуют ``None``
    как default-deny. На практике хендлеры всегда сначала отбивают отсутствие
    пользователя через ``if not self.current_user`` (401), но ``None``-контракт
    делает хелпер безопасным сам по себе (используется и read-эндпоинтами без
    такого 401-гарда).

    ``.strip().lower()`` — defense-in-depth: матрица прав хранит lowercase-ключи
    (``UserRole.value``); канонические value уже lowercase, нормализуем
    симметрично.
    """
    user = handler.current_user
    if user is None:
        return None
    # UserRole — StrEnum, член является str; str(...) даёт value и типизируется
    # как str (в отличие от StrEnum.value → Any).
    return str(user.role).strip().lower()


class BaseHandler(UserAuthWithValidationMixin, ProblemDetailsHandler):
    """
    Базовый handler для User API.

    Наследует:
    - UserAuthWithValidationMixin: Аутентификация через X-Remote-User с валидацией в БД (первый для правильного MRO!)
    - ProblemDetailsHandler: JSON сериализация + RFC 7807 Problem Details хелперы

    Важно: Миксины идут первыми в наследовании, чтобы их prepare() вызывались в правильном порядке по MRO.

    Отличие от Locker API:
    User API валидирует пользователя в БД и загружает полный объект User.
    Это нужно для работы с пользовательскими данными.

    Использование:
        class MyHandler(BaseHandler):
            async def get(self):
                user = self.current_user  # полный объект User
                # Работа с данными пользователя...

    `get_json_body` / `get_json_body_as` берутся из ProblemDetailsHandler.
    """
