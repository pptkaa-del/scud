from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from tornado.testing import AsyncHTTPTestCase

from app.handlers.media import InternalMediaUploadHandler
from app.history_data.db.models.media import LockerMediaWithBytes
from app.history_data.db.repositories.media_repository import MediaRepository
from app.main import make_app


def _build_multipart(fields: dict[str, str], file_body: bytes = b"\xff\xd8") -> tuple[bytes, str]:
    """Сборка multipart/form-data body.

    Возвращает (body_bytes, content_type_header).
    """
    boundary = "testboundary123"
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(value.encode())
    # файл
    lines.append(f"--{boundary}".encode())
    lines.append(b'Content-Disposition: form-data; name="file"; filename="photo.jpg"')
    lines.append(b"Content-Type: image/jpeg")
    lines.append(b"")
    lines.append(file_body)
    lines.append(f"--{boundary}--".encode())
    body = b"\r\n".join(lines)
    ct = f"multipart/form-data; boundary={boundary}"
    return body, ct


class TestMediaListByEvent(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        self.db.fetch_one = AsyncMock(return_value={"c": 0})
        return make_app(self.db)

    def test_event_media_returns_list(self):
        response = self.fetch(
            "/api/v1/events/security/7/media",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 200)


class TestMediaGetById(AsyncHTTPTestCase):
    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(
            return_value={
                "id": 1,
                "locker_id": uuid4(),
                "sensor_id": None,
                "security_event_id": 7,
                "kind": "photo",
                "mime_type": "image/jpeg",
                "size_bytes": 4,
                "width": None,
                "height": None,
                "duration_ms": None,
                "captured_at": datetime.now(UTC),
                "uploaded_at": datetime.now(UTC),
                "uploaded_by": None,
                "description": None,
                "metadata": {},
                "bytes": b"\xff\xd8\xff\xe0",
            }
        )
        return make_app(self.db)

    def test_returns_bytes_with_content_type(self):
        response = self.fetch(
            "/api/v1/media/1",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.body, b"\xff\xd8\xff\xe0")

    def test_content_disposition_filename_has_jpg_extension(self):
        response = self.fetch(
            "/api/v1/media/1",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 200)
        self.assertEqual(
            response.headers["Content-Disposition"],
            'inline; filename="media-1.jpg"',
        )

    def test_404_when_missing(self):
        self.db.fetch_one = AsyncMock(return_value=None)
        response = self.fetch(
            "/api/v1/media/999",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 404)


class TestInternalMediaUpload(AsyncHTTPTestCase):
    """I4: unit-тесты InternalMediaUploadHandler."""

    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(return_value={"id": 77})
        self.db.fetch_all = AsyncMock(return_value=[])
        return make_app(self.db)

    def _post_multipart(self, fields: dict[str, str], file_body: bytes = b"\xff\xd8") -> object:
        body, ct = _build_multipart(fields, file_body)
        return self.fetch(
            "/internal/api/v1/media",
            method="POST",
            body=body,
            headers={"Content-Type": ct},
        )

    def test_happy_path_201(self):
        """Корректный multipart → 201 + media_id в ответе."""
        locker_id = str(uuid4())
        fields = {
            "locker_id": locker_id,
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 201)
        import json
        body = json.loads(response.body)
        self.assertIn("id", body)

    def test_unknown_kind_400(self):
        """Неверный kind → 400."""
        fields = {
            "locker_id": str(uuid4()),
            "kind": "audio",  # не в MediaKind
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 400)

    def test_unsupported_mime_415(self):
        """Неподдерживаемый mime_type → 415."""
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/gif",
            "captured_at": "2026-06-01T10:00:00+00:00",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 415)

    def test_file_too_large_413(self):
        """Файл больше _MAX_MEDIA_BYTES → 413."""
        from app.handlers.media import _MAX_MEDIA_BYTES
        huge_body = b"x" * (_MAX_MEDIA_BYTES + 1)
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
        }
        response = self._post_multipart(fields, file_body=huge_body)
        self.assertEqual(response.code, 413)

    def test_metadata_invalid_json_400(self):
        """Невалидный JSON в metadata → 400."""
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
            "metadata": "{not valid json",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 400)
        import json
        body = json.loads(response.body)
        self.assertIn("metadata", body.get("detail", ""))

    def test_non_numeric_width_400(self):
        """COR-29: нечисловой width → 400, а не 500.

        Раньше int(width_raw) стоял вне try-блока валидации: ValueError от
        int('abc') долетал до write_error → 500. Теперь конверсия внутри try.
        """
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
            "width": "not-a-number",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 400)

    def test_numeric_width_accepted_201(self):
        """Числовой width проходит валидацию → 201."""
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
            "width": "1920",
            "height": "1080",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 201)

    def test_event_window_lookup_called(self):
        """Без security_event_id но с event_sensor_id + event_occurred_at →
        find_event_in_window вызывается, результат (7) записывается в строку."""
        sensor_id = str(uuid4())
        locker_id = str(uuid4())

        # fetch_one: первый вызов → find_event_in_window → {"id": 7}
        #            второй вызов → insert → {"id": 99}
        call_count = 0

        async def fetch_one_side_effect(sql, params, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"id": 7}  # find_event_in_window
            return {"id": 99}  # insert

        self.db.fetch_one = AsyncMock(side_effect=fetch_one_side_effect)
        fields = {
            "locker_id": locker_id,
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
            "event_sensor_id": sensor_id,
            "event_occurred_at": "2026-06-01T09:59:58+00:00",
        }
        response = self._post_multipart(fields)
        self.assertEqual(response.code, 201)
        # find_event_in_window + insert → 2 fetch_one вызова
        self.assertEqual(call_count, 2)


class TestInternalMediaUploadSecurityEventUid(AsyncHTTPTestCase):
    """Привязка кадра к событию по security_event_uid (relay-фото)."""

    def get_app(self):
        self.db = MagicMock()
        self.calls = MagicMock()
        self.calls.find_event_id_by_uid = AsyncMock(return_value=None)
        self.calls.find_event_in_window = AsyncMock(return_value=None)
        self.calls.insert = AsyncMock(return_value=77)
        self.calls.relink_media = AsyncMock()
        self.calls.relink_by_media_uid = AsyncMock()
        self.patches = [
            patch.object(MediaRepository, name, getattr(self.calls, name))
            for name in (
                "find_event_id_by_uid", "find_event_in_window", "insert",
                "relink_media", "relink_by_media_uid",
            )
        ]
        original_set_status = InternalMediaUploadHandler.set_status

        def _spy_set_status(handler, status_code, reason=None):
            self.calls.set_status(status_code)
            return original_set_status(handler, status_code, reason)

        self.patches.append(
            patch.object(InternalMediaUploadHandler, "set_status", _spy_set_status)
        )
        for p in self.patches:
            p.start()
        return make_app(self.db)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def _post(self, **extra: str):
        fields = {
            "locker_id": str(uuid4()),
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": "2026-06-01T10:00:00+00:00",
            "event_sensor_id": str(uuid4()),
            "event_occurred_at": "2026-06-01T10:00:00+00:00",
            **extra,
        }
        body, ct = _build_multipart(fields)
        return self.fetch(
            "/internal/api/v1/media", method="POST", body=body,
            headers={"Content-Type": ct},
        )

    def _names(self) -> list[str]:
        return [c[0] for c in self.calls.mock_calls]

    def test_uid_found_inserts_with_event_and_skips_relink(self):
        self.calls.find_event_id_by_uid.return_value = 5
        uid = str(uuid4())
        response = self._post(security_event_uid=uid)
        self.assertEqual(response.code, 201)
        self.calls.find_event_id_by_uid.assert_awaited_once_with(uid)
        kwargs = self.calls.insert.await_args.kwargs
        self.assertEqual(kwargs["security_event_id"], 5)
        self.assertEqual(kwargs["security_event_uid"], uid)
        self.calls.relink_media.assert_not_awaited()
        self.calls.find_event_in_window.assert_not_awaited()

    def test_uid_missing_inserts_null_then_relinks_before_201(self):
        uid = str(uuid4())
        response = self._post(security_event_uid=uid)
        self.assertEqual(response.code, 201)
        self.assertIsNone(self.calls.insert.await_args.kwargs["security_event_id"])
        self.calls.relink_media.assert_awaited_once_with(77)
        names = self._names()
        self.assertLess(names.index("insert"), names.index("relink_media"))
        self.assertLess(names.index("relink_media"), names.index("set_status"))

    def test_uid_never_falls_back_to_window(self):
        self._post(security_event_uid=str(uuid4()))
        self.calls.find_event_in_window.assert_not_awaited()

    def test_invalid_uid_400(self):
        response = self._post(security_event_uid="not-a-uuid")
        self.assertEqual(response.code, 400)
        self.calls.insert.assert_not_awaited()

    def test_duplicate_relinks_by_media_uid_then_200(self):
        self.calls.insert.return_value = None
        media_uid = str(uuid4())
        response = self._post(security_event_uid=str(uuid4()), event_uid=media_uid)
        self.assertEqual(response.code, 200)
        self.calls.relink_by_media_uid.assert_awaited_once_with(media_uid)
        names = self._names()
        self.assertLess(names.index("relink_by_media_uid"), names.index("set_status"))

    def test_relink_failure_500(self):
        self.calls.relink_media.side_effect = RuntimeError("db down")
        response = self._post(security_event_uid=str(uuid4()))
        self.assertEqual(response.code, 500)

    def test_duplicate_relink_failure_500(self):
        self.calls.insert.return_value = None
        self.calls.relink_by_media_uid.side_effect = RuntimeError("db down")
        response = self._post(security_event_uid=str(uuid4()), event_uid=str(uuid4()))
        self.assertEqual(response.code, 500)

    def test_legacy_without_uid_uses_window(self):
        self.calls.find_event_in_window.return_value = 7
        response = self._post()
        self.assertEqual(response.code, 201)
        self.calls.find_event_in_window.assert_awaited_once()
        self.calls.find_event_id_by_uid.assert_not_awaited()
        self.assertEqual(self.calls.insert.await_args.kwargs["security_event_id"], 7)
        self.calls.relink_media.assert_not_awaited()


class TestMediaListHandlerFilters(AsyncHTTPTestCase):
    """I4: unit-тесты MediaListHandler — фильтры и валидация."""

    def get_app(self):
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(return_value={"c": 0})
        self.db.fetch_all = AsyncMock(return_value=[])
        return make_app(self.db)

    def test_no_filters_calls_repo_with_all_none(self):
        """Без фильтров → list_paginated вызывается, ответ 200."""
        response = self.fetch(
            "/api/v1/media",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 200)

    def test_locker_id_and_kind_photo(self):
        """locker_id + kind=photo → ответ 200."""
        locker = str(uuid4())
        response = self.fetch(
            f"/api/v1/media?locker_id={locker}&kind=photo",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 200)

    def test_bad_locker_id_400(self):
        """Невалидный locker_id (не UUID) → 400."""
        response = self.fetch(
            "/api/v1/media?locker_id=not-a-uuid",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 400)

    def test_bad_from_date_400(self):
        """Невалидный from-дата → 400."""
        response = self.fetch(
            "/api/v1/media?from=not-a-date",
            headers={"X-Remote-User": "1", "X-Remote-Role": "operator"},
        )
        self.assertEqual(response.code, 400)


if __name__ == "__main__":
    unittest.main()
