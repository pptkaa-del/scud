"""HTTP-клиент для gateway API.

Все запросы идут ТОЛЬКО через Apache Gateway с cookie-аутентификацией.
Прямые обращения к backend-сервисам не используются.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import requests

logger = logging.getLogger(__name__)


class GatewayClient:
    """Sync HTTP-клиент для запросов через Apache Gateway.

    Аутентификация: cookie-based (access_token), полученный через POST /api/v1/auth/login.
    Все запросы проходят через ACM (роль проверяется автоматически).
    """

    def __init__(self, base_url: str, session: requests.Session):
        self._base = base_url.rstrip("/")
        self._session = session

    # ── Auth ──────────────────────────────────────────────────────────────────

    @classmethod
    def login(cls, base_url: str, phone: str, password: str) -> "GatewayClient":
        """Выполнить вход и вернуть аутентифицированный клиент."""
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        # Keep-alive + увеличенный pool (для параллельных тестов и быстрого fast-mode)
        # Retry на connection errors (RemoteDisconnected после истечения keep-alive Apache)
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[502, 503, 504],
        )
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=retry,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"Connection": "keep-alive"})

        resp = session.post(
            f"{base_url.rstrip('/')}/api/v1/auth/login",
            json={"phone": phone, "password": password},
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"Login failed for {phone}: {resp.status_code} {resp.text}"
        )
        assert "access_token" in session.cookies, "access_token cookie not set after login"
        return cls(base_url, session)

    def logout(self) -> None:
        """Выйти из системы."""
        self._session.post(f"{self._base}/api/v1/auth/logout", timeout=10)

    # ── Users ─────────────────────────────────────────────────────────────────

    def create_user(
        self,
        phone: str,
        full_name: str,
        password: str,
        role: str = "worker",
    ) -> dict:
        """Создать нового пользователя."""
        resp = self._session.post(
            f"{self._base}/api/v1/users",
            json={"phone": phone, "full_name": full_name,
                  "password": password, "role": role},
            timeout=30,
        )
        assert resp.status_code == 201, f"create_user failed: {resp.status_code} {resp.text}"
        return resp.json()

    def get_user(self, user_id: int) -> dict:
        resp = self._session.get(f"{self._base}/api/v1/user/{user_id}", timeout=30)
        assert resp.status_code == 200
        return resp.json()

    def delete_user(self, user_id: int) -> None:
        """Деактивировать пользователя."""
        resp = self._session.delete(f"{self._base}/api/v1/user/{user_id}", timeout=30)
        assert resp.status_code in (204, 404), (
            f"delete_user failed: {resp.status_code} {resp.text}"
        )

    def disable_2fa(self, user_id: int) -> None:
        """Отключить 2FA пользователю.

        Новые пользователи создаются с require_2fa=True по умолчанию; тест-воркеры
        логинятся паролем без привязки Max, поэтому 2FA им нужно снять.
        """
        resp = self._session.post(
            f"{self._base}/api/v1/user/{user_id}/disable-2fa", timeout=30
        )
        assert resp.status_code == 204, (
            f"disable_2fa failed: {resp.status_code} {resp.text}"
        )

    # ── Lockers ───────────────────────────────────────────────────────────────

    def create_locker(self, inventory_number: str) -> dict:
        """Создать шкаф в backend."""
        resp = self._session.post(
            f"{self._base}/api/v1/lockers",
            json={"inventory_number": inventory_number},
            timeout=30,
        )
        assert resp.status_code == 201, f"create_locker failed: {resp.status_code} {resp.text}"
        return resp.json()

    def get_locker(self, locker_id: str) -> dict:
        resp = self._session.get(f"{self._base}/api/v1/locker/{locker_id}", timeout=30)
        assert resp.status_code == 200
        return resp.json()

    def delete_locker(self, locker_id: str) -> None:
        resp = self._session.delete(f"{self._base}/api/v1/locker/{locker_id}", timeout=30)
        assert resp.status_code in (204, 404), (
            f"delete_locker failed: {resp.status_code} {resp.text}"
        )

    def get_locker_state(self, locker_id: str) -> dict:
        """Получить полное состояние шкафа (connectivity, door, operational_status)."""
        resp = self._session.get(f"{self._base}/api/v1/locker/{locker_id}/state", timeout=30)
        assert resp.status_code == 200, f"get_state failed: {resp.status_code}"
        return resp.json()

    def get_connectivity(self, locker_id: str) -> str:
        """Получить состояние связи шкафа (online/offline)."""
        return self.get_locker_state(locker_id)["connectivity"]

    def get_door(self, locker_id: str) -> str:
        """Получить состояние двери (closed/open)."""
        return self.get_locker_state(locker_id)["door"]

    def get_operational_status(self, locker_id: str) -> str:
        """Получить операционный статус (normal/maintenance/alarm)."""
        return self.get_locker_state(locker_id)["operational_status"]

    def set_operational_status(self, locker_id: str, status: str) -> dict:
        """Установить операционный статус шкафа."""
        resp = self._session.put(
            f"{self._base}/api/v1/locker/{locker_id}/state",
            json={"operational_status": status},
            timeout=30,
        )
        assert resp.status_code == 200, f"set_operational_status failed: {resp.status_code}"
        return resp.json()

    def set_public_key(self, locker_id: str, public_key_b64: str) -> dict:
        """Зарегистрировать публичный ключ устройства."""
        resp = self._session.post(
            f"{self._base}/api/v1/locker/{locker_id}/set-public-key",
            json={"public_key": public_key_b64},
            timeout=30,
        )
        assert resp.status_code == 200, f"set_public_key failed: {resp.status_code} {resp.text}"
        return resp.json()

    def create_sensor(
        self,
        locker_id: str,
        serial_number: str,
        sensor_type_id: int,
        current_value: float | None = None,
    ) -> dict:
        """Добавить датчик к шкафу."""
        body: dict[str, Any] = {
            "serial_number": serial_number,
            "sensor_type_id": sensor_type_id,
        }
        if current_value is not None:
            body["current_value"] = current_value
        resp = self._session.post(
            f"{self._base}/api/v1/locker/{locker_id}/sensors",
            json=body,
            timeout=30,
        )
        assert resp.status_code == 201, f"create_sensor failed: {resp.status_code} {resp.text}"
        return resp.json()

    def get_sensor(self, locker_id: str, sensor_id: str) -> dict:
        resp = self._session.get(
            f"{self._base}/api/v1/locker/{locker_id}/sensors/{sensor_id}",
            timeout=30,
        )
        assert resp.status_code == 200, f"get_sensor failed: {resp.status_code}"
        return resp.json()

    def list_sensors(self, locker_id: str) -> list[dict]:
        """Возвращает items из PaginatedResponse `{items, total, limit, offset}`."""
        resp = self._session.get(
            f"{self._base}/api/v1/locker/{locker_id}/sensors",
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def create_threshold(
        self,
        locker_id: str,
        sensor_id: str,
        min_val: float | None = None,
        max_val: float | None = None,
        fixed_val: Any = None,
    ) -> dict:
        """Создать пороговое значение для датчика."""
        body: dict[str, Any] = {}
        if min_val is not None:
            body["min_value"] = min_val
        if max_val is not None:
            body["max_value"] = max_val
        if fixed_val is not None:
            body["fixed_value"] = fixed_val
        resp = self._session.post(
            f"{self._base}/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            json=body,
            timeout=30,
        )
        assert resp.status_code == 201, f"create_threshold failed: {resp.status_code} {resp.text}"
        return resp.json()

    def get_telemetry(
        self,
        locker_id: str | None = None,
        sensor_type_code: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Получить телеметрию (history_api) с фильтром по locker_id."""
        params: dict[str, Any] = {"limit": limit}
        if locker_id is not None:
            params["locker_id"] = locker_id
        if sensor_type_code is not None:
            params["sensor_type_code"] = sensor_type_code
        resp = self._session.get(
            f"{self._base}/api/v1/telemetry/list",
            params=params,
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"get_telemetry failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def get_command_history(
        self,
        locker_id: str | None = None,
        status: str | None = None,
        command_type: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Получить историю команд (history_api)."""
        params: dict[str, Any] = {"limit": limit}
        if locker_id is not None:
            params["locker_id"] = locker_id
        if status is not None:
            params["status"] = status
        if command_type is not None:
            params["command_type"] = command_type
        resp = self._session.get(
            f"{self._base}/api/v1/commands/history",
            params=params,
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"get_command_history failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def get_photos(
        self,
        locker_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Получить список фото (history_api)."""
        params: dict[str, Any] = {"limit": limit}
        if locker_id is not None:
            params["locker_id"] = locker_id
        resp = self._session.get(
            f"{self._base}/api/v1/photos",
            params=params,
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"get_photos failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def get_event_media(self, event_id: int) -> list[dict]:
        """Получить список медиа-файлов, привязанных к событию безопасности."""
        resp = self._session.get(
            f"{self._base}/api/v1/events/security/{event_id}/media",
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"get_event_media failed: {resp.status_code} {resp.text}"
        )
        return resp.json()["items"]

    def get_media_bytes(self, media_id: int) -> tuple[bytes, str]:
        """Скачать байты медиа-файла. Возвращает (bytes, content_type)."""
        resp = self._session.get(
            f"{self._base}/api/v1/media/{media_id}",
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"get_media_bytes failed: {resp.status_code} {resp.text}"
        )
        return resp.content, resp.headers["Content-Type"]

    def get_security_events(
        self,
        locker_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Получить события безопасности (history_api) с фильтром по locker_id/event_type.

        Returns:
            Список событий из ответа PaginatedResponse.items.
        """
        params: dict[str, Any] = {"limit": limit}
        if locker_id is not None:
            params["locker_id"] = locker_id
        if event_type is not None:
            params["event_type"] = event_type
        resp = self._session.get(
            f"{self._base}/api/v1/events/security",
            params=params,
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"get_security_events failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()
        # PaginatedResponse: {items, total, limit, offset}
        return body.get("items", body if isinstance(body, list) else [])

    def send_command(
        self,
        locker_id: str,
        command_type: str,
        parameters: dict | None = None,
        session: requests.Session | None = None,
    ) -> dict:
        """Отправить команду шкафу.

        Args:
            session: если передан — использовать эту сессию вместо admin сессии
                     (для отправки от имени конкретного пользователя)
        """
        s = session or self._session
        resp = s.post(
            f"{self._base}/api/v1/locker/{locker_id}/commands",
            json={"type": command_type, "parameters": parameters or {}},
            timeout=30,
        )
        assert resp.status_code == 201, f"send_command failed: {resp.status_code} {resp.text}"
        return resp.json()

    # ── Sensor types ──────────────────────────────────────────────────────────

    def get_sensor_types(self) -> list[dict]:
        """Возвращает items из PaginatedResponse."""
        resp = self._session.get(f"{self._base}/api/v1/sensor-types", timeout=30)
        assert resp.status_code == 200
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def set_sensor_type_states(self, type_id: int, states: list[dict]) -> dict:
        resp = self._session.put(
            f"{self._base}/api/v1/sensor-types/{type_id}",
            json={"states": states},
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"set_sensor_type_states failed: {resp.status_code} {resp.text}"
        )
        return resp.json()

    def create_sensor_type(
        self,
        code: str,
        name: str,
        unit: str,
        value_type: str,
    ) -> dict:
        resp = self._session.post(
            f"{self._base}/api/v1/sensor-types",
            json={"code": code, "name": name, "unit": unit, "value_type": value_type},
            timeout=30,
        )
        assert resp.status_code in (201, 409), (
            f"create_sensor_type failed: {resp.status_code} {resp.text}"
        )
        if resp.status_code == 409:
            types = self.get_sensor_types()
            return next(t for t in types if t["code"] == code)
        return resp.json()

    # ── Reports ───────────────────────────────────────────────────────────────

    def create_report(self, report_type: str, params: dict) -> dict:
        """Поставить задачу на генерацию отчёта (POST /api/v1/reports).

        Returns:
            dict из ответа 202: {id, status, report_type, format, created_at}.
        """
        resp = self._session.post(
            f"{self._base}/api/v1/reports",
            json={"report_type": report_type, "params": params},
            timeout=30,
        )
        assert resp.status_code == 202, (
            f"create_report failed: {resp.status_code} {resp.text}"
        )
        return resp.json()

    def get_report(self, report_id: str) -> dict:
        """Получить состояние задачи отчёта (GET /api/v1/report/{id})."""
        resp = self._session.get(
            f"{self._base}/api/v1/report/{report_id}", timeout=30
        )
        assert resp.status_code == 200, (
            f"get_report failed: {resp.status_code} {resp.text}"
        )
        return resp.json()

    def download_report(self, report_id: str) -> requests.Response:
        """Скачать сгенерированный файл отчёта (GET /api/v1/report/{id}/download).

        Returns:
            requests.Response — у вызывающего есть доступ к .content / заголовкам.
        """
        resp = self._session.get(
            f"{self._base}/api/v1/report/{report_id}/download", timeout=60
        )
        assert resp.status_code == 200, (
            f"download_report failed: {resp.status_code} {resp.text}"
        )
        return resp

    def delete_report(self, report_id: str) -> None:
        """Удалить/истечь задачу отчёта (DELETE /api/v1/report/{id})."""
        resp = self._session.delete(
            f"{self._base}/api/v1/report/{report_id}", timeout=30
        )
        assert resp.status_code in (204, 404), (
            f"delete_report failed: {resp.status_code} {resp.text}"
        )

    # ── Notifications ─────────────────────────────────────────────────────────

    def get_active_notifications_by_tag(
        self,
        tag_key: str,
        tag_value: str,
    ) -> list[dict]:
        """Получить активные постоянные уведомления с фильтром по тегу.

        Запрос идёт через gateway с cookie-аутентификацией.
        """
        resp = self._session.get(
            f"{self._base}/api/v1/notifications",
            params={
                "tag_key": tag_key,
                "tag_value": tag_value,
                "type": "persistent",
                "active_only": "true",
                "limit": "50",
            },
            timeout=30,
        )
        assert resp.status_code == 200, f"get_notifications failed: {resp.status_code} {resp.text}"
        body = resp.json()
        return body.get("items", body if isinstance(body, list) else [])

    def get_all_notifications_by_locker(
        self,
        locker_id: str,
        event: str | None = None,
    ) -> list[dict]:
        """Получить активные уведомления для шкафа (по тегу locker_id).

        Args:
            locker_id: UUID шкафа
            event: если передан — фильтровать по tags.event
                   (например "locker_opened" или "threshold_violation")
        """
        notifs = self.get_active_notifications_by_tag("locker_id", locker_id)
        if event:
            notifs = [n for n in notifs if n.get("tags", {}).get("event") == event]
        return notifs


def unique_phone() -> str:
    """Генерировать уникальный российский номер телефона для тестов."""
    return "+7" + str(random.randint(9000000000, 9999999999))
