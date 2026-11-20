"""
Базовый handler для ACM.

ACM endpoints не требуют аутентификации пользователя (они её осуществляют),
поэтому BaseHandler наследует только ProblemDetailsHandler без UserAuthMixin.

Здесь же живут общие для CheckHandler и IdentityHandler шаги: валидация токена
через auth_api и загрузка UserContext из БД (кэш — на стороне вызывающего).
"""
import logging

from tornado.httpclient import HTTPError

from shared.auth import ProblemDetailsHandler
from shared.http import RetryHTTPClient, UpstreamUnavailableError

from ..cache import UserContext
from ..db import UserRepository

logger = logging.getLogger(__name__)
_validate_client = RetryHTTPClient()


class BaseHandler(ProblemDetailsHandler):
    """
    Базовый handler для ACM.

    Конфигурация сервиса (auth_api_url, cache_ttl_seconds) берётся из self.settings.
    """

    async def _validate_token(self, token: str) -> int | None:
        """
        Валидирует access_token через auth_api /auth/validate.

        Returns:
            user_id при успехе, None при 4xx (валидный 401 для клиента).

        Raises:
            UpstreamUnavailableError: auth_api отдал 5xx или сетевой сбой —
                ACM не может определить, валиден ли токен; caller отвечает 503.
        """
        auth_url = self.settings.get("auth_api_url", "http://auth_api:8003")
        url = f"{auth_url}/auth/validate"

        try:
            resp = await _validate_client.fetch(
                url,
                method="GET",
                headers={"X-Access-Token": token},
            )
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                return None
            logger.error("auth_api 5xx on /auth/validate: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='auth_api') from exc
        except (OSError, ConnectionError) as exc:
            logger.error("auth_api network failure on /auth/validate: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='auth_api') from exc
        except ValueError as exc:
            logger.error("auth_api validate misconfiguration: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='auth_api') from exc

        user_id_str = resp.headers.get("X-User-Id", "").strip()
        if not user_id_str:
            logger.warning("auth_api returned 200 without X-User-Id header")
            return None
        try:
            return int(user_id_str)
        except ValueError:
            logger.error("auth_api returned non-int X-User-Id: %r", user_id_str)
            return None

    async def _load_user_context(self, user_id: int) -> UserContext | None:
        """Загружает роль, state и Допуски пользователя из БД и строит UserContext."""
        repo = UserRepository(self.db)

        user = await repo.get_by_id(user_id)
        if user is None:
            return None

        access = await repo.get_active_access(user_id)

        return UserContext(
            user_id=user_id,
            role=user.role,
            state=user.state,
            full_access=access.full_access,
            locker_ids=tuple(access.locker_ids),
            require_2fa=user.require_2fa,
        )
