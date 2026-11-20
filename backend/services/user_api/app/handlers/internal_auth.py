"""
Internal endpoints для auth_api.

Доступны только внутри backend_net.
Apache закрывает /internal/* от внешнего доступа (Require all denied).

POST /internal/auth/check      — проверка телефон + password_hash
GET  /internal/user/by-phone   — получение user_id + ФИО + роли по телефону
GET  /internal/user/{user_id}  — получение id + role пользователя по ID
GET  /internal/users/names     — batch resolve ФИО по списку id

Безопасность ``/internal/auth/check``:
    auth_api подписывает каждый запрос HMAC-SHA256 ключом
    ``INTERNAL_AUTH_HMAC_KEY`` (общий секрет с user_api) в generic-форме
    ``timestamp + METHOD + path + sha256(body)``. Если в settings handler'а
    ключ задан — подпись и timestamp обязательны; невалидные запросы
    отклоняются 401 без тела. Если ключ не задан (legacy/dev окружение без
    .env), проверка пропускается. Подпись привязана к method+path (SEC-20),
    а phone входит в канон только через sha256(body) — нет field-injection.
    См. ``shared.utils.compute_request_signature``.

Безопасность PII-read эндпойнтов (by-phone, by-id, names):
    Эти три GET-хендлера возвращают PII (user_id / role / ФИО) другим
    сервисам. Подписываются отдельным per-domain ключом
    ``INTERNAL_USER_READ_HMAC_KEY`` (auth_api и reports_api подписывают,
    user_api проверяет) — компрометация этого канала не открывает
    ``/internal/auth/check`` и наоборот. Семантика идентична: ключ задан →
    подпись обязательна (401 без тела до любого обращения к БД); ключ не
    задан → fail-open для dev/CI. Подпись привязана к method+path
    (для by-id это конкретный ``/internal/user/{id}``). Для by-phone и names
    селектор лежит в query (``phone=``, ``ids=``), поэтому канонизированный
    query сворачивается в подписываемый path через ``signed_path`` и входит
    в подпись — подмена значения внутри replay-окна отвергается (SEC-10).
"""
import json
import logging
from typing import TYPE_CHECKING

import bcrypt
from tornado.web import RequestHandler

from app.user_data.db.repositories.user import UserRepository
from shared.auth import JSONHandler
from shared.enums import UserState
from shared.utils import body_sha256, signed_path, verify_request_signature

logger = logging.getLogger(__name__)

# Имена HTTP-заголовков inter-service HMAC-подписи (общие для всех каналов).
_HDR_TIMESTAMP = "X-Internal-Timestamp"
_HDR_SIGNATURE = "X-Internal-Signature"

# Ключи settings, под которыми main.py кладёт bytes-ключи HMAC.
_SETTING_AUTH_KEY = "internal_auth_hmac_key"
_SETTING_USER_READ_KEY = "internal_user_read_hmac_key"


if TYPE_CHECKING:
    # Type-only base: миксин всегда замешан с RequestHandler у потребителей
    # (`class FooHandler(_InternalSignatureMixin, JSONHandler)`). В runtime —
    # object (см. else), MRO выстраивается через конкретные хендлеры.
    _SignatureMixinBase = RequestHandler
else:
    _SignatureMixinBase = object


class _InternalSignatureMixin(_SignatureMixinBase):
    """Проверка inter-service HMAC-подписи запроса (SEC-10/SEC-20).

    Подмешивается во все internal-хендлеры. Читает bytes-ключ из
    ``self.settings`` по переданному ключу настройки; если ключ не задан —
    fail-open (dev/CI без .env). При заданном ключе требует валидные
    ``X-Internal-Timestamp`` + ``X-Internal-Signature`` над
    ``timestamp + METHOD + signed_path + sha256(body)``.

    ``signed_path`` сворачивает канонизированный query в подписываемый path,
    поэтому для by-phone/names значение селектора (``phone=``/``ids=``) входит
    в подпись (SEC-10). Для эндпойнтов без query — no-op (path как раньше).
    """

    def _verify_internal_signature(self, setting_key: str) -> bool:
        hmac_key = self.settings.get(setting_key)
        if not hmac_key:
            # Ключ не настроен — режим обратной совместимости (dev/CI).
            return True
        timestamp = self.request.headers.get(_HDR_TIMESTAMP, "")
        signature = self.request.headers.get(_HDR_SIGNATURE, "")
        # Подписывается raw-body (sha256), не пересобранный dict — хэш должен
        # совпасть байт-в-байт с тем, что подписал отправитель. Канонизированный
        # query сворачивается в path через signed_path, чтобы селектор в query
        # (phone=/ids=) нельзя было подменить в пределах replay-окна (SEC-10).
        return verify_request_signature(
            signature=signature,
            timestamp=timestamp,
            method=self.request.method or "",
            path=signed_path(self.request.path, self.request.query),
            body_sha256_hex=body_sha256(self.request.body or b""),
            key=hmac_key,
        )

    def _reject_unsigned(self, endpoint: str) -> None:
        # Не логируем тело/подпись — это могут быть секреты. Логируем факт
        # отказа с IP, чтобы заметить попытку обхода.
        logger.warning(
            "%s signature rejected from %s",
            endpoint,
            self.request.remote_ip,
        )
        self.set_status(401)
        self.finish()

# Dummy bcrypt hash для anti-enumeration timing-equalization.
# Генерится один раз при импорте модуля. checkpw против него всегда
# возвращает False, но затрачивает то же время, что и реальный bcrypt
# (~12 cost, ~100ms на современных CPU). Содержимое строки самонесущее —
# валидный bcrypt-формат `$2b$<cost>$<22-char salt><31-char hash>`,
# созданный из bcrypt.hashpw(b"placeholder", bcrypt.gensalt()).
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(
    b"_anti_enum_placeholder_never_match", bcrypt.gensalt(),
).decode("utf-8")


class InternalAuthCheckHandler(_InternalSignatureMixin, JSONHandler):
    """
    Проверяет учётные данные пользователя.

    Body: {"phone": "...", "password_hash": "<SHA-256 hex>"}
    Headers (если ``INTERNAL_AUTH_HMAC_KEY`` задан в settings):
        X-Internal-Timestamp: unix-секунды
        X-Internal-Signature: hex HMAC-SHA256
    Response 200: {"user_id": 42, ...}
    Response 401: если HMAC-подпись невалидна, истекла, либо учётные
        данные неверны / пользователь неактивен. Тело ответа отсутствует —
        клиент не должен различать "неверная подпись" и "неверный пароль".
    """

    async def post(self) -> None:
        try:
            body = json.loads(self.request.body or b"{}")
        except (ValueError, TypeError):
            self.set_status(400)
            self.finish()
            return

        phone = body.get("phone", "").strip()
        password_hash = body.get("password_hash", "").strip()

        if not phone or not password_hash:
            self.set_status(400)
            self.finish()
            return

        # HMAC-проверка подписи запроса. Если ключ не настроен — режим
        # обратной совместимости (dev/CI без .env), подпись не требуется.
        # В production main.py обязан установить ``internal_auth_hmac_key``.
        if not self._verify_internal_signature(_SETTING_AUTH_KEY):
            self._reject_unsigned("internal/auth/check")
            return

        repo = UserRepository(self.db)
        user = await repo.get_by_phone(phone)
        auth_record = await repo.get_auth_record(user.id) if user else None

        # Anti-enumeration: всегда выполняем bcrypt.checkpw, даже если user
        # не найден или неактивен. Иначе response time для «нет такого user»
        # ~ноль, для «есть, но wrong password» ~100ms — атакующий внутри
        # backend_net (компрометированный соседний контейнер) может
        # вытаскивать список зарегистрированных телефонов через timing.
        # Dummy-хэш генерируется один раз и хранится как bcrypt с валидной
        # солью, чтобы checkpw отрабатывал по полному циклу.
        target_hash = (
            auth_record["password_hash"]
            if auth_record
            else _DUMMY_BCRYPT_HASH
        )
        try:
            password_matches = bcrypt.checkpw(
                password_hash.encode("utf-8"),
                target_hash.encode("utf-8"),
            )
        except (ValueError, TypeError):
            # bcrypt бросает ValueError на malformed salt/hash (повреждённая
            # строка в БД, неверный prefix `$2b$`) и TypeError если args
            # не bytes (sanity). Без exc_info это был бы silent failure —
            # все пользователи получают 401 без диагностики.
            logger.exception(
                "bcrypt.checkpw raised — malformed password_hash in DB?"
            )
            password_matches = False

        # Все ветки отказа сводятся к одному 401 без тела ПОСЛЕ выполнения
        # bcrypt — таким образом timing одинаковый для:
        #   • user не найден,
        #   • user.state != ACTIVE,
        #   • auth_record отсутствует,
        #   • пароль не совпал.
        if not user or user.state != UserState.ACTIVE or not auth_record or not password_matches:
            self.set_status(401)
            self.finish()
            return

        self.write_json({
            "user_id": user.id,
            "require_2fa": auth_record["require_2fa"],
            "name": user.full_name,
            "role": str(user.role),
            "max_user_id": user.max_user_id,
        })


class InternalUserByPhoneHandler(_InternalSignatureMixin, JSONHandler):
    """
    Возвращает user_id по номеру телефона.

    Query: ?phone=+79991234567
    Headers (если ``INTERNAL_USER_READ_HMAC_KEY`` задан в settings):
        X-Internal-Timestamp + X-Internal-Signature над
        ``signed_path("/internal/user/by-phone", query)`` — канонизированный
        query (вместе с ``phone=``) входит в подпись (SEC-10).
    Response 200: {"user_id": 42, "name": "Иван Иванов", "role": "worker"}
        (name/role нужны auth_api для симметричного login/verify-ответа).
    Response 401: если подпись требуется, но невалидна/отсутствует
    Response 404: если пользователь не найден
    """

    async def get(self) -> None:
        if not self._verify_internal_signature(_SETTING_USER_READ_KEY):
            self._reject_unsigned("internal/user/by-phone")
            return

        phone = (self.get_query_argument("phone", default="") or "").strip()
        if not phone:
            self.set_status(400)
            self.finish()
            return

        repo = UserRepository(self.db)
        user = await repo.get_by_phone(phone)

        if not user or user.state != UserState.ACTIVE:
            self.set_status(404)
            self.finish()
            return

        self.write_json({
            "user_id": user.id,
            "name": user.full_name,
            "role": str(user.role),
        })


class InternalUserByIdHandler(_InternalSignatureMixin, JSONHandler):
    """
    Возвращает id и роль пользователя по его ID.

    Path param: user_id (int)
    Headers (если ``INTERNAL_USER_READ_HMAC_KEY`` задан в settings):
        X-Internal-Timestamp + X-Internal-Signature над конкретным path
        ``/internal/user/{id}`` — id привязан подписью (SEC-10).
    Response 200: {"id": 42, "role": "administrator"}
    Response 401: если подпись требуется, но невалидна/отсутствует
    Response 404: если пользователь не найден
    """

    async def get(self, user_id: str) -> None:
        if not self._verify_internal_signature(_SETTING_USER_READ_KEY):
            self._reject_unsigned("internal/user/by-id")
            return

        try:
            uid = int(user_id)
        except ValueError:
            self.set_status(400)
            self.finish()
            return

        repo = UserRepository(self.db)
        user = await repo.get_by_id(uid)

        if not user:
            self.set_status(404)
            self.finish()
            return

        self.write_json({"id": user.id, "role": str(user.role)})


# Максимальное число id в одном запросе — защита от abuse/огромных IN.
_MAX_NAMES_IDS = 1000


class InternalUserNamesHandler(_InternalSignatureMixin, JSONHandler):
    """
    Batch-resolve ФИО пользователей по списку id.

    Query: ?ids=1,2,3 (целые, через запятую)
    Headers (если ``INTERNAL_USER_READ_HMAC_KEY`` задан в settings):
        X-Internal-Timestamp + X-Internal-Signature над
        ``signed_path("/internal/users/names", query)`` — канонизированный
        query (вместе с ``ids=``) входит в подпись (SEC-10).
    Response 200: {"names": {"1": "Иванов Иван", "3": "Петров Пётр"}}
        Отсутствующие id просто не попадают в map. ФИО возвращается для
        ЛЮБОГО существующего пользователя независимо от state (исторический
        отчёт по открытию должен показывать ФИО даже деактивированных).
    Response 400: ids невалидны (не целые) или их больше ``_MAX_NAMES_IDS``.
    Response 401: если подпись требуется, но невалидна/отсутствует.

    Пустой / отсутствующий ids → 200 {"names": {}}.
    """

    async def get(self) -> None:
        if not self._verify_internal_signature(_SETTING_USER_READ_KEY):
            self._reject_unsigned("internal/users/names")
            return

        raw = self.get_query_argument("ids", default="") or ""
        if not raw.strip():
            self.write_json({"names": {}})
            return

        try:
            ids = [int(part) for part in raw.split(",") if part.strip()]
        except ValueError:
            self.set_status(400)
            self.finish()
            return

        # Дедуп с сохранением порядка.
        ids = list(dict.fromkeys(ids))
        if len(ids) > _MAX_NAMES_IDS:
            self.set_status(400)
            self.finish()
            return

        repo = UserRepository(self.db)
        names = await repo.get_full_names_by_ids(ids)
        self.write_json({"names": {str(uid): name for uid, name in names.items()}})
