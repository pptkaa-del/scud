"""
Базовый handler для Auth API.

Auth endpoints не требуют аутентификации (они её выдают),
поэтому BaseHandler наследует только ProblemDetailsHandler без UserAuthMixin.
"""
from shared.auth import ProblemDetailsHandler

# Имя/флаги сессионного cookie — security-sensitive, единый источник для
# login/verify/access. HttpOnly (нет доступа из JS), SameSite=Strict (нет
# cross-site отправки), Secure добавляется только если settings["secure_cookies"]
# (по умолчанию True; отключается в dev/тестах по HTTP). Path=/ — cookie на весь
# домен.
ACCESS_COOKIE_NAME = "access_token"
_DEFAULT_ACCESS_TOKEN_TTL_HOURS = 720
_SECONDS_PER_HOUR = 3600


class BaseHandler(ProblemDetailsHandler):
    """
    Базовый handler для Auth API.

    Не содержит auth mixin — эти endpoints сами являются аутентификацией.
    Все RFC 7807 хелперы (`write_validation_error`, `write_unauthorized`,
    `write_too_many_requests` и т.д.) предоставляются `ProblemDetailsHandler`.

    Дополнительные настройки (jwt_secret, user_api_url и т.д.) берутся из self.settings.
    """

    def access_token_ttl_hours(self) -> int:
        """TTL сессионного cookie в часах (settings → дефолт 720)."""
        return int(
            self.settings.get(
                "access_token_ttl_hours", _DEFAULT_ACCESS_TOKEN_TTL_HOURS,
            ),
        )

    def _set_access_cookie(self, token: str) -> None:
        """Установить сессионный access-cookie с security-флагами.

        Единый источник для login/verify/access: имя, HttpOnly/SameSite=Strict/
        Path=/, Max-Age (из TTL) и опциональный Secure формируются здесь, чтобы
        три endpoint'а не расходились по security-флагам.
        """
        max_age = self.access_token_ttl_hours() * _SECONDS_PER_HOUR
        cookie = (
            f"{ACCESS_COOKIE_NAME}={token}; "
            f"HttpOnly; "
            f"SameSite=Strict; "
            f"Path=/; "
            f"Max-Age={max_age}"
        )
        if self.settings.get("secure_cookies", True):
            cookie += "; Secure"
        self.set_header("Set-Cookie", cookie)
