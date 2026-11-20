"""
Общие утилиты для всех сервисов.

Содержит вспомогательные функции и классы:
- validators: Валидаторы данных
- formatters: Форматирование данных
- helpers: Прочие вспомогательные функции
- read_secret: Чтение секретов из Docker secrets / env .
"""
import hashlib
import hmac
import os
import time
from typing import overload
import urllib.parse

__all__ = [
    "hash_password",
    "hash_token",
    "compute_request_signature",
    "verify_request_signature",
    "signed_path",
    "body_sha256",
    "read_secret",
    "is_production",
    "DEFAULT_INTERNAL_SIGNATURE_MAX_AGE_SECONDS",
]


# Окно валидности inter-service HMAC-подписи. 30 секунд достаточно для вызова
# по backend_net, но коротко настолько, чтобы перехваченный с замедлением
# запрос быстро устаревал и replay был бесполезен.
DEFAULT_INTERNAL_SIGNATURE_MAX_AGE_SECONDS = 30


# Имя env-переменной, определяющей окружение сервиса.
APP_ENV_VAR = "APP_ENV"
# Значение по умолчанию, когда APP_ENV не задана.
DEFAULT_APP_ENV = "development"
# Значения APP_ENV, при которых окружение считается production. Только в этом
# режиме read_secret(required=True) начинает кидать исключение на слабом/пустом
# секрете (fail-closed). Dev/CI стартуют без APP_ENV и остаются fail-open.
PRODUCTION_APP_ENVS = frozenset({"production", "prod"})

# Известные placeholder-значения секретов: трактуются как «секрет не задан» при
# required=True, чтобы дефолтная заглушка не прошла проверку в production.
#
# Сравнение — строгое equality (`value in _KNOWN_PLACEHOLDER_SECRETS`), без
# substring-логики: реальный 64-hex секрет из `openssl rand -hex 32` не должен
# ложно совпасть. Источник dev-значений — backend/scripts/seed-dev-secrets.sh;
# при добавлении/смене dev-default в скрипте обнови и этот набор, иначе
# production-деплой с засеянными dev-секретами поднимется на слабом ключе.
_KNOWN_PLACEHOLDER_SECRETS = frozenset({
    # jwt_secret dev-default + исторический literal.
    "change-me-in-production-must-be-at-least-32-bytes-long",
    "dev-secret-change-in-production",
    # internal_*_hmac_key dev-defaults (seed-dev-secrets.sh).
    "internal-auth-dev-key-do-not-use-in-prod",
    "internal-otp-dev-key-do-not-use-in-prod",
    "internal-max-binding-dev-key-do-not-use-in-prod",
    "internal-user-read-dev-key-do-not-use-in-prod",
    "internal-session-revoke-dev-key-do-not-use-in-prod",
    "internal-step-up-dev-key-do-not-use-in-prod",
    "internal-history-dev-key-do-not-use-in-prod",
    # mqtt_password / redis_password / postgres_<svc>_password / admin_password dev-defaults.
    "scud_backend_dev_password",
    "redis-dev-only-do-not-use-in-prod",
    "password",
    "changeme",
})


def is_production() -> bool:
    """True, если сервис запущен в production-окружении (``APP_ENV`` in prod-set).

    По умолчанию (переменная не задана) — development, поэтому dev/CI ничего не
    меняют в поведении. Сравнение регистронезависимо.
    """
    return os.getenv(APP_ENV_VAR, DEFAULT_APP_ENV).strip().lower() in PRODUCTION_APP_ENVS


@overload
def read_secret(
    name: str,
    *,
    env_var: str | None = ...,
    default: str,
    required: bool = ...,
) -> str: ...


@overload
def read_secret(
    name: str,
    *,
    env_var: str | None = ...,
    default: None = ...,
    required: bool = ...,
) -> str | None: ...


def read_secret(
    name: str,
    *,
    env_var: str | None = None,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    """Прочитать секрет в порядке приоритета: Docker secret → env → default.

    1. ``/run/secrets/<name>`` — основной prod-источник (Docker secret file-mount).
       Содержимое читается полностью, обрезаются ведущие/хвостовые пробелы и
       перевод строки (echo "..." >file добавляет ``\\n``). Если файл существует
       но пустой — пропускаем (трактуем как «секрет не сконфигурирован»),
       чтобы env-fallback не потерял реальное значение.
    2. ``os.getenv(env_var)`` — fallback для dev/test и интеграционных тестов,
       которые не монтируют secrets-файлы. Если переменная задана (даже пустая
       строка-литерал) — возвращаем её.
    3. ``default`` — последний резерв; возвращается если ни secret-файл, ни
       env-переменная не найдены.

    При ``required=False`` (дефолт) никогда не кидает исключение — все ошибки
    чтения файла трактуются как «секрет не доступен через файл», переключаемся
    на следующий источник.

    При ``required=True`` (fail-closed) итоговое значение проверяется: если оно
    отсутствует, пустое или совпадает с известным placeholder'ом — кидается
    ``RuntimeError``. Используется в production-стартапе сервисов, чтобы
    misconfigured deploy не поднялся на слабом/дефолтном секрете.
    """
    secret_file = f"/run/secrets/{name}"
    value: str | None = None
    try:
        if os.path.isfile(secret_file):
            with open(secret_file, encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                value = content
    except OSError:
        # Файл существует, но прочитать не получилось — fallback к env/default.
        pass

    if value is None and env_var:
        v = os.getenv(env_var)
        if v is not None:
            value = v

    if value is None:
        value = default

    if required and (not value or value in _KNOWN_PLACEHOLDER_SECRETS):
        raise RuntimeError(
            f"Secret '{name}' is required but missing, empty or a known "
            f"placeholder. Configure /run/secrets/{name} or "
            f"{env_var or '<env>'} with a real value before production deploy."
        )

    return value


def hash_password(password: str) -> str:
    """SHA-256 хэш пароля в hex. Единая реализация для всех сервисов."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    """SHA-256 хэш токена в hex. Единая реализация для всех сервисов."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def body_sha256(body: bytes) -> str:
    """SHA-256(body) → hex. Используется как stable summary для HMAC-подписи."""
    return hashlib.sha256(body).hexdigest()


def signed_path(path: str, query: str | None = None) -> str:
    """Path-компонент для HMAC-подписи, включающий канонизированный query.

    Для эндпойнтов, где селектор лежит в query (by-phone ?phone=, names ?ids=),
    query обязан входить в подпись, иначе атакующий внутри backend_net может
    подменить значение в пределах replay-окна (SEC-10). Пары сортируются для
    детерминизма: подписант и проверяющий получают идентичную строку
    независимо от исходного порядка/кодирования. Пустой query → path без
    изменений (by-id / auth/check / by-invite подписываются как раньше).
    """
    if not query:
        return path
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    canonical = urllib.parse.urlencode(sorted(pairs))
    return f"{path}?{canonical}"


def compute_request_signature(
    timestamp: str,
    method: str,
    path: str,
    body_sha256_hex: str,
    key: bytes,
) -> str:
    """HMAC-SHA256 подпись inter-service запроса, привязанная к method + path.

    Подписываются четыре поля, разделённые `\\n`:
        <timestamp>\\n<METHOD>\\n<path>\\n<body_sha256_hex>

    ``method`` нормализуется к верхнему регистру. ``path`` — только компонент
    пути URL (без схемы, хоста и query-строки): на принимающей стороне это
    ровно ``self.request.path`` (Tornado отбрасывает query). Привязка к
    method+path означает, что подпись, снятая для одного эндпойнта, не
    принимается на другом — захваченный в 30-секундном окне запрос нельзя
    переиграть против иного метода/пути (SEC-20).

    Тело участвует только через sha256-хэш, поэтому никакое значение в теле
    (например телефон) не может сдвинуть поля канона — нет field-injection.

    Args:
        timestamp: Unix-время в секундах строкой (заголовок
            ``X-Internal-Timestamp``).
        method: HTTP-метод запроса (``GET``/``POST``/...); регистр не важен.
        path: компонент пути URL без query (например ``/internal/auth/check``).
        body_sha256_hex: ``body_sha256(raw_body)`` — hex SHA-256 сырого тела.
        key: общий секрет (bytes). См. ``INTERNAL_*_HMAC_KEY``.

    Returns:
        hex-строка длиной 64 символа.
    """
    message = f"{timestamp}\n{method.upper()}\n{path}\n{body_sha256_hex}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_request_signature(
    signature: str,
    timestamp: str,
    method: str,
    path: str,
    body_sha256_hex: str,
    key: bytes,
    max_age_seconds: int = DEFAULT_INTERNAL_SIGNATURE_MAX_AGE_SECONDS,
) -> bool:
    """Проверить подпись из ``compute_request_signature`` + replay-окно.

    Возвращает ``True`` если:
    1. Все аргументы непустые / валидны (timestamp — целое число).
    2. ``timestamp`` отличается от текущего времени не более чем на
       ``max_age_seconds`` секунд (в любую сторону — защита от рассинхрона
       часов).
    3. Подпись совпадает с ``compute_request_signature(...)`` над тем же
       method+path+body — сравнение через ``hmac.compare_digest`` для
       устойчивости к timing-атакам.

    Любая ошибка валидации (нечисловой timestamp, пустые поля, нечётная
    длина hex и т.д.) → ``False``. Никогда не кидаем исключение наверх.
    """
    if (
        not signature
        or not timestamp
        or not method
        or not path
        or not body_sha256_hex
        or not key
    ):
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False

    now = int(time.time())
    if abs(now - ts_int) > max_age_seconds:
        return False

    expected = compute_request_signature(
        timestamp, method, path, body_sha256_hex, key,
    )
    return hmac.compare_digest(expected, signature)
