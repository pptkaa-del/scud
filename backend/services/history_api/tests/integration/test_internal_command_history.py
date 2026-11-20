"""Интеграционные тесты для internal GET /internal/api/v1/commands/history.

Эндпойнт доступен без X-Remote-User (закрыт снаружи через Apache), вызывается
reports_api для построения отчётов. Сидируем `command_history` напрямую через
psycopg2 (нужен полный контроль над executed_at), затем запрашиваем эндпойнт
с фильтрами и проверяем выборку (только подходящие строки), порядок
(executed_at ASC) и форму ответа (ПОЛНЫЕ строки, не проекция).
"""

from __future__ import annotations

from datetime import datetime
import os

import psycopg2
import requests

from .conftest import DB_CONFIG, generate_unique_command_id, generate_unique_locker_id

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8002")

INTERNAL_URL = f"{BASE_URL}/internal/api/v1/commands/history"


def _insert_command(
    *,
    command_id: str,
    locker_id: str,
    command_type: str,
    status: str,
    executed_at: str | None,
    locker_inventory_number: str | None = None,
    created_by: int | None = None,
    user_full_name: str | None = None,
) -> None:
    """Вставить строку command_history с явным executed_at."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO command_history (
                    command_id, locker_id, locker_inventory_number,
                    command_type, created_by, user_full_name,
                    created_at, status, executed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s)
                """,
                (
                    command_id,
                    locker_id,
                    locker_inventory_number,
                    command_type,
                    created_by,
                    user_full_name,
                    status,
                    executed_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_internal_get_filters_orders_and_returns_full_rows(wait_for_api):
    """Только open+executed в окне; порядок по executed_at ASC; полные строки."""
    locker_id = generate_unique_locker_id()
    inv = f"INV-{locker_id[:8]}"

    matching = [
        ("2026-07-10T12:00:00+00:00", 1, "Первый П."),
        ("2026-07-10T08:00:00+00:00", 2, "Второй В."),
        ("2026-07-10T20:00:00+00:00", 3, "Третий Т."),
    ]
    for executed_at, created_by, full_name in matching:
        _insert_command(
            command_id=generate_unique_command_id(),
            locker_id=locker_id,
            locker_inventory_number=inv,
            command_type="open",
            status="executed",
            executed_at=executed_at,
            created_by=created_by,
            user_full_name=full_name,
        )

    # Не-open команда (исключается фильтром command_type)
    _insert_command(
        command_id=generate_unique_command_id(),
        locker_id=locker_id,
        locker_inventory_number=inv,
        command_type="close",
        status="executed",
        executed_at="2026-07-10T13:00:00+00:00",
        created_by=4,
        user_full_name="Закрывающий З.",
    )
    # open, но не executed (исключается фильтром status)
    _insert_command(
        command_id=generate_unique_command_id(),
        locker_id=locker_id,
        locker_inventory_number=inv,
        command_type="open",
        status="failed",
        executed_at=None,
        created_by=5,
        user_full_name="Неудачник Н.",
    )
    # open+executed, но executed_at вне окна (исключается диапазоном)
    _insert_command(
        command_id=generate_unique_command_id(),
        locker_id=locker_id,
        locker_inventory_number=inv,
        command_type="open",
        status="executed",
        executed_at="2026-08-01T00:00:00+00:00",
        created_by=6,
        user_full_name="Поздний П.",
    )

    resp = requests.get(
        INTERNAL_URL,
        params={
            "command_type": "open",
            "status": "executed",
            "executed_from": "2026-07-10T00:00:00+00:00",
            "executed_to": "2026-07-10T23:59:59+00:00",
            "order": "executed_at_asc",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Сужаем до записей нашего шкафа (БД может содержать чужие данные)
    items = [i for i in body["items"] if i["locker_inventory_number"] == inv]
    assert len(items) == 3, items

    # Порядок по executed_at по возрастанию (datetime parse-able через fromisoformat)
    parsed = [datetime.fromisoformat(i["executed_at"]) for i in items]
    assert parsed == sorted(parsed)
    assert parsed[0] == datetime.fromisoformat("2026-07-10T08:00:00+00:00")

    # ПОЛНЫЕ строки command_history, не 4-полевая проекция.
    for item in items:
        for field in (
            "id", "command_id", "locker_id", "locker_inventory_number",
            "command_type", "parameters", "created_by", "user_phone",
            "user_full_name", "created_at", "sent_at", "executed_at",
            "failed_at", "status", "result", "error_message",
        ):
            assert field in item, item

    # Денормализованные поля доехали; первый по порядку — "Второй В."
    assert items[0]["created_by"] == 2
    assert items[0]["user_full_name"] == "Второй В."

    assert body["total"] >= 3
    assert body["limit"] == 1000
    assert body["offset"] == 0


def test_internal_get_no_auth_required(wait_for_api):
    """Эндпойнт internal — без X-Remote-User должен отдавать 200 (не 401)."""
    resp = requests.get(INTERNAL_URL, params={"limit": "1"})
    assert resp.status_code == 200


def test_internal_get_rejects_bad_executed_date(wait_for_api):
    """Неразбираемая дата → 400."""
    resp = requests.get(INTERNAL_URL, params={"executed_from": "garbage"})
    assert resp.status_code == 400


def test_internal_get_rejects_bad_order(wait_for_api):
    """Недопустимый order → 400."""
    resp = requests.get(INTERNAL_URL, params={"order": "totally_bogus"})
    assert resp.status_code == 400
