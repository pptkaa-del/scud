"""Unit-тесты для internal handlers notification_api.

Покрывают `InternalNotificationListHandler` (GET/POST) и
`InternalNotificationDetailHandler` (DELETE) — внутренние эндпойнты
`/internal/api/v1/notifications` и `/internal/api/v1/notification/{id}`,
которые НЕ требуют `X-Remote-User` (закрыты Apache от внешних клиентов).

pytest-asyncio в Astra недоступен → используем `AsyncHTTPTestCase` от
Tornado, репозиторий мокаем через `unittest.mock.patch` на уровне модуля
`app.handlers.notification_logic.NotificationRepository`.
"""
from __future__ import annotations

from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.notification_data.db.models.notification import (
    Notification,
    NotificationRecipient,
    NotificationType,
)

_NOTIFICATION_ID = "550e8400-e29b-41d4-a716-446655440000"
_MISSING_ID = "550e8400-e29b-41d4-a716-000000000000"


def _make_notification(**overrides) -> Notification:
    """Notification dataclass для возврата из мока repo.create/cancel."""
    defaults = {
        "id": _NOTIFICATION_ID,
        "type": NotificationType.PERSISTENT,
        "text": "Тестовое уведомление",
        "tags": {"severity": "high"},
        "start_at": datetime(2024, 1, 15, 12, 0, 0),
        "is_cancelled": False,
        "created_at": datetime(2024, 1, 15, 12, 0, 0),
        "created_by": None,  # internal — system-source, NULL
    }
    defaults.update(overrides)
    return Notification(**defaults)


def _make_recipient(**overrides) -> NotificationRecipient:
    defaults = {
        "id": 1,
        "notification_id": _NOTIFICATION_ID,
        "user_id": None,
        "role": "operator",
    }
    defaults.update(overrides)
    return NotificationRecipient(**defaults)


class _InternalHandlerTestBase(AsyncHTTPTestCase):
    """Общая база: подменяем репозиторий, чтобы не трогать реальную БД."""

    def get_app(self):
        # db не используется напрямую — все вызовы идут через NotificationRepository,
        # который мы патчим в каждом тесте. Достаточно «заглушки».
        self.db_mock = MagicMock()
        return make_app(self.db_mock)


class TestInternalListHandlerPost(_InternalHandlerTestBase):
    """POST /internal/api/v1/notifications — создание системного уведомления."""

    def test_post_invalid_json_missing_required_fields(self):
        """Пустое тело → MissingValueError → 400 validation error."""
        # Никаких patch repo — handler упадёт раньше, на парсинге DTO.
        response = self.fetch(
            "/internal/api/v1/notifications",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b"{}",
        )
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")

    def test_post_unknown_notification_type_returns_400(self):
        """Неизвестный type → ValueError из CreateNotificationDTO.__post_init__
        → перехватывается get_json_body_as → 400.

        (До `NotificationType(dto.type)` управление не доходит, потому что
        dacite вызывает __post_init__ ещё внутри from_dict.)
        """
        payload = {
            "type": "bogus_type",
            "text": "irrelevant",
            "start_at": "2024-01-15T12:00:00Z",
            "roles": ["operator"],
        }
        response = self.fetch(
            "/internal/api/v1/notifications",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
        )
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertIn("bogus_type", data["detail"])

    def test_post_valid_persistent_creates_notification(self):
        """Валидный body → repo.create вызван с правильными kwargs,
        repo.get_recipients возвращает список → 201 + notification + recipients."""
        notification = _make_notification()
        recipients = [_make_recipient()]

        with patch(
            "app.handlers.notification_logic.NotificationRepository"
        ) as repo_cls:
            repo_instance = repo_cls.return_value
            repo_instance.create = AsyncMock(return_value=notification)
            repo_instance.get_recipients = AsyncMock(return_value=recipients)

            payload = {
                "type": "persistent",
                "text": "Тестовое уведомление",
                "tags": {"severity": "high"},
                "start_at": "2024-01-15T12:00:00Z",
                "roles": ["operator"],
            }
            response = self.fetch(
                "/internal/api/v1/notifications",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload),
            )

        self.assertEqual(response.code, 201)
        body = json.loads(response.body)
        self.assertIn("notification", body)
        self.assertIn("recipients", body)
        self.assertEqual(body["notification"]["id"], _NOTIFICATION_ID)
        self.assertEqual(len(body["recipients"]), 1)
        self.assertEqual(body["recipients"][0]["role"], "operator")

        # Verify repo.create called once with system-source created_by=None
        repo_instance.create.assert_called_once()
        kwargs = repo_instance.create.call_args.kwargs
        self.assertEqual(kwargs["type"], NotificationType.PERSISTENT)
        self.assertEqual(kwargs["text"], "Тестовое уведомление")
        self.assertEqual(kwargs["tags"], {"severity": "high"})
        self.assertIsNone(kwargs["created_by"])  # internal: system, not user
        self.assertEqual(kwargs["start_at"], "2024-01-15T12:00:00Z")
        self.assertEqual(kwargs["roles"], ["operator"])

        repo_instance.get_recipients.assert_called_once_with(_NOTIFICATION_ID)


class TestInternalListHandlerGet(_InternalHandlerTestBase):
    """GET /internal/api/v1/notifications — список с фильтрами."""

    def test_get_with_type_user_id_role_filters(self):
        """Все три фильтра → repo.get_all/.count получают точные kwargs."""
        with patch(
            "app.handlers.notification_logic.NotificationRepository"
        ) as repo_cls:
            repo_instance = repo_cls.return_value
            repo_instance.get_all = AsyncMock(return_value=[])
            repo_instance.count = AsyncMock(return_value=0)

            response = self.fetch(
                "/internal/api/v1/notifications?type=persistent&user_id=42&role=worker",
                method="GET",
            )

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)

        repo_instance.get_all.assert_called_once_with(
            type="persistent",
            user_id=42,
            role="worker",
            tag_key=None,
            tag_value=None,
            active_only=True,
            limit=100,
            offset=0,
        )
        repo_instance.count.assert_called_once_with(
            type="persistent",
            user_id=42,
            role="worker",
            tag_key=None,
            tag_value=None,
            active_only=True,
        )

    def test_get_active_only_false_passed_as_bool(self):
        """active_only=false (string) → kwarg передан как Python False."""
        with patch(
            "app.handlers.notification_logic.NotificationRepository"
        ) as repo_cls:
            repo_instance = repo_cls.return_value
            repo_instance.get_all = AsyncMock(return_value=[])
            repo_instance.count = AsyncMock(return_value=0)

            response = self.fetch(
                "/internal/api/v1/notifications?active_only=false",
                method="GET",
            )

        self.assertEqual(response.code, 200)
        kwargs = repo_instance.get_all.call_args.kwargs
        self.assertIs(kwargs["active_only"], False)
        count_kwargs = repo_instance.count.call_args.kwargs
        self.assertIs(count_kwargs["active_only"], False)


class TestInternalDetailHandlerDelete(_InternalHandlerTestBase):
    """DELETE /internal/api/v1/notification/{id} — отмена системой."""

    def test_delete_existing_returns_200_with_body(self):
        """repo.cancel вернул объект → 200 + сериализованный notification."""
        cancelled = _make_notification(is_cancelled=True)
        with patch(
            "app.handlers.notification_logic.NotificationRepository"
        ) as repo_cls:
            repo_instance = repo_cls.return_value
            repo_instance.cancel = AsyncMock(return_value=cancelled)

            response = self.fetch(
                f"/internal/api/v1/notification/{_NOTIFICATION_ID}",
                method="DELETE",
            )

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], _NOTIFICATION_ID)
        self.assertTrue(data["is_cancelled"])
        repo_instance.cancel.assert_called_once_with(_NOTIFICATION_ID)

    def test_delete_nonexistent_returns_404(self):
        """repo.cancel вернул None → 404 'Notification not found...'."""
        with patch(
            "app.handlers.notification_logic.NotificationRepository"
        ) as repo_cls:
            repo_instance = repo_cls.return_value
            repo_instance.cancel = AsyncMock(return_value=None)

            response = self.fetch(
                f"/internal/api/v1/notification/{_MISSING_ID}",
                method="DELETE",
            )

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertIn("not found or already cancelled", data["detail"])
        repo_instance.cancel.assert_called_once_with(_MISSING_ID)
