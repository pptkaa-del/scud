"""Интеграционные тесты для Command History API"""
from datetime import datetime, timedelta

from .conftest import generate_unique_command_id, generate_unique_locker_id


def test_command_history_create(api_client):
    """Тест: запись истории команды"""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    # 1. Создаем запись истории команды
    payload = {
        "command_id": command_id,
        "locker_id": locker_id,
        "locker_inventory_number": "СО №12345",
        "command_type": "open",
        "parameters": {"timeout": 30},
        "created_by": 42,
        "user_phone": "+79991234567",
        "user_full_name": "Иванов Иван Иванович",
        "status": "pending"
    }

    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 200
    history = response.json()
    assert history["command_id"] == command_id
    assert history["locker_id"] == locker_id
    assert history["command_type"] == "open"
    assert history["status"] == "pending"
    assert "id" in history
    assert "created_at" in history


def test_command_history_list_filters(api_client):
    """Тест: фильтрация истории команд"""
    locker_id = generate_unique_locker_id()
    user_id = 123

    # 1. Создаем несколько команд
    commands_data = [
        {"command_type": "open", "status": "executed"},
        {"command_type": "close", "status": "executed"},
        {"command_type": "reboot", "status": "pending"},
    ]

    for cmd_data in commands_data:
        payload = {
            "command_id": generate_unique_command_id(),
            "locker_id": locker_id,
            "command_type": cmd_data["command_type"],
            "status": cmd_data["status"],
            "created_by": user_id,
            "user_phone": "+79991234567",
            "user_full_name": "Тестовый Пользователь"
        }
        response = api_client.post("/api/v1/commands/history", json=payload)
        assert response.status_code == 200

    # 2. Фильтр по locker_id
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert data["total"] >= 3

    # 3. Фильтр по user_id
    response = api_client.get(f"/api/v1/commands/history?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3

    # 4. Фильтр по command_type
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}&command_type=open")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1

    # 5. Фильтр по status
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}&status=executed")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2

    # 6. Фильтр по датам
    now = datetime.utcnow()
    from_date = (now - timedelta(hours=1)).isoformat() + "Z"
    to_date = (now + timedelta(hours=1)).isoformat() + "Z"
    response = api_client.get(
        f"/api/v1/commands/history?locker_id={locker_id}&from={from_date}&to={to_date}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3


def test_command_history_denormalized_data(api_client):
    """Тест: проверка денормализованных данных пользователя"""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    # 1. Создаем команду с полными данными пользователя и шкафа
    payload = {
        "command_id": command_id,
        "locker_id": locker_id,
        "locker_inventory_number": "ЩУ ТОИ №777",
        "command_type": "open",
        "created_by": 99,
        "user_phone": "+79165551234",
        "user_full_name": "Петров Петр Петрович",
        "status": "executed"
    }

    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 200
    history = response.json()

    # 2. Проверяем, что денормализованные данные сохранились
    assert history["locker_inventory_number"] == "ЩУ ТОИ №777"
    assert history["user_phone"] == "+79165551234"
    assert history["user_full_name"] == "Петров Петр Петрович"
    assert history["created_by"] == 99

    # 3. Получаем запись из списка и проверяем денормализованные данные
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= 1

    # Находим нашу команду
    found = False
    for item in items:
        if item["command_id"] == command_id:
            assert item["locker_inventory_number"] == "ЩУ ТОИ №777"
            assert item["user_phone"] == "+79165551234"
            assert item["user_full_name"] == "Петров Петр Петрович"
            found = True
            break
    assert found


def test_duplicate_command_id_is_upserted(api_client):
    """Повторный POST с тем же command_id обновляет существующую запись (idempotent)."""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    # 1. Создаём команду в pending
    payload_pending = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "open",
        "parameters": {"timeout": 30},
        "created_by": 42,
        "user_phone": "+79991234567",
        "user_full_name": "Иванов И.И.",
        "status": "pending",
    }
    resp = api_client.post("/api/v1/commands/history", json=payload_pending)
    assert resp.status_code == 200
    pending = resp.json()
    assert pending["status"] == "pending"
    assert pending["sent_at"] is None
    assert pending["executed_at"] is None
    record_id = pending["id"]
    created_at = pending["created_at"]

    # 2. Повторный POST с тем же command_id и status=sent → 200, та же запись (id, created_at), sent_at проставлен
    resp = api_client.post(
        "/api/v1/commands/history",
        json={**payload_pending, "status": "sent"},
    )
    assert resp.status_code == 200
    sent = resp.json()
    assert sent["id"] == record_id
    assert sent["created_at"] == created_at
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None
    assert sent["executed_at"] is None
    sent_at_value = sent["sent_at"]

    # 3. Третий POST с status=executed → executed_at проставлен, sent_at НЕ перезаписан
    resp = api_client.post(
        "/api/v1/commands/history",
        json={
            **payload_pending,
            "status": "executed",
            "result": {"door": "opened"},
        },
    )
    assert resp.status_code == 200
    executed = resp.json()
    assert executed["id"] == record_id
    assert executed["status"] == "executed"
    assert executed["sent_at"] == sent_at_value  # не перезаписан
    assert executed["executed_at"] is not None
    assert executed["result"] == {"door": "opened"}

    # 4. Денормализованные поля сохранены через все апдейты
    assert executed["user_phone"] == "+79991234567"
    assert executed["created_by"] == 42


def test_late_lower_rank_status_does_not_regress(api_client):
    """COR-07 real-DB: запоздавший статус с НИЗШИМ рангом не откатывает уже
    достигнутый. executed (rank 3) → late sent (rank 1) → остаётся executed.

    Это out-of-order/retried доставка: command_history.upsert монотонно
    продвигает статус по рангу (pending<sent<delivered<{executed,failed}).
    Forward-переходы покрыты test_duplicate_command_id_is_upserted; здесь —
    обратный (regress) случай, который и был багом до фикса rank-guard.
    """
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()
    base = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "open",
        "created_by": 7,
    }

    # 1. Команда сразу долетает как executed (с результатом).
    resp = api_client.post(
        "/api/v1/commands/history",
        json={**base, "status": "executed", "result": {"door": "opened"}},
    )
    assert resp.status_code == 200
    executed = resp.json()
    assert executed["status"] == "executed"
    assert executed["executed_at"] is not None

    # 2. Запоздавший sent (низший ранг) приходит ПОСЛЕ executed.
    resp = api_client.post(
        "/api/v1/commands/history",
        json={**base, "status": "sent"},
    )
    assert resp.status_code == 200
    after_late = resp.json()

    # Статус НЕ регрессировал: остаётся executed, executed_at сохранён,
    # result не затёрт пустым телом late-апдейта.
    assert after_late["status"] == "executed", (
        "late sent не должен откатывать executed (COR-07 rank-guard)"
    )
    assert after_late["executed_at"] == executed["executed_at"]
    assert after_late["result"] == {"door": "opened"}


def _post_history(api_client, body: dict) -> dict:
    resp = api_client.post("/api/v1/commands/history", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_failed_then_executed_keeps_failed_and_its_fields(api_client):
    """Поздний executed после failed по TTL не протекает в executed_at/result/error_message."""
    base = {
        "command_id": generate_unique_command_id(),
        "locker_id": generate_unique_locker_id(),
        "command_type": "open",
    }
    reason = "истёк срок ожидания ответа шкафа"
    failed = _post_history(api_client, {
        **base, "status": "failed", "error_message": reason,
        "result": {"success": False, "message": reason},
    })
    after = _post_history(api_client, {
        **base, "status": "executed", "result": {"rep_result": "SUCCESS"},
    })
    assert after["status"] == "failed"
    assert after["executed_at"] is None
    assert after["failed_at"] == failed["failed_at"]
    assert after["result"] == {"success": False, "message": reason}
    assert after["error_message"] == reason


def test_executed_then_failed_keeps_executed(api_client):
    base = {
        "command_id": generate_unique_command_id(),
        "locker_id": generate_unique_locker_id(),
        "command_type": "open",
    }
    executed = _post_history(api_client, {
        **base, "status": "executed", "result": {"rep_result": "SUCCESS"},
    })
    after = _post_history(api_client, {
        **base, "status": "failed", "error_message": "failed",
        "result": {"success": False, "message": "failed"},
    })
    assert after["status"] == "executed"
    assert after["failed_at"] is None
    assert after["executed_at"] == executed["executed_at"]
    assert after["result"] == {"rep_result": "SUCCESS"}
    assert after["error_message"] is None


def test_late_pending_does_not_regress_sent(api_client):
    """COR-07 real-DB: sent (rank 1) затем late pending (rank 0) → остаётся sent."""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()
    base = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "close",
    }

    resp = api_client.post("/api/v1/commands/history", json={**base, "status": "sent"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"

    resp = api_client.post("/api/v1/commands/history", json={**base, "status": "pending"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent", "late pending не должен откатывать sent"


def test_command_history_status_lifecycle(api_client):
    """Тест: жизненный цикл статусов команды"""
    locker_id = generate_unique_locker_id()

    # Проверяем создание команд с разными статусами
    statuses = ["pending", "sent", "delivered", "executed", "failed"]

    for status in statuses:
        command_id = generate_unique_command_id()
        payload = {
            "command_id": command_id,
            "locker_id": locker_id,
            "command_type": "open",
            "status": status
        }
        response = api_client.post("/api/v1/commands/history", json=payload)
        assert response.status_code == 200
        history = response.json()
        assert history["status"] == status


def test_command_history_with_parameters(api_client):
    """Тест: команда с параметрами"""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    payload = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "config",
        "parameters": {
            "threshold_temperature": 35.0,
            "threshold_humidity": 80.0,
            "reporting_interval": 30
        },
        "status": "pending"
    }

    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 200
    history = response.json()
    assert "parameters" in history
    assert history["parameters"]["threshold_temperature"] == 35.0


def test_command_history_relay_types_round_trip(api_client):
    locker_id = generate_unique_locker_id()
    for command_type in ("relay_on", "relay_off", "relay_restart"):
        response = api_client.post("/api/v1/commands/history", json={
            "command_id": generate_unique_command_id(),
            "locker_id": locker_id,
            "command_type": command_type,
            "parameters": {"relay": 2},
            "status": "pending",
        })
        assert response.status_code == 200, response.text
        assert response.json()["command_type"] == command_type

    response = api_client.get(
        f"/api/v1/commands/history?locker_id={locker_id}&command_type=relay_off",
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["command_type"] for item in items] == ["relay_off"]
    assert items[0]["parameters"] == {"relay": 2}


def test_command_history_pagination(api_client):
    """Тест: пагинация истории команд"""
    locker_id = generate_unique_locker_id()

    # 1. Создаем несколько команд
    for _i in range(5):
        payload = {
            "command_id": generate_unique_command_id(),
            "locker_id": locker_id,
            "command_type": "open",
            "status": "executed"
        }
        response = api_client.post("/api/v1/commands/history", json=payload)
        assert response.status_code == 200

    # 2. Получаем с лимитом
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}&limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) <= 3
    assert data["limit"] == 3

    # 3. Получаем со смещением
    response = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}&limit=2&offset=2")
    assert response.status_code == 200
    data = response.json()
    assert data["offset"] == 2


def test_command_history_validation(api_client):
    """Тест: валидация создания истории команд"""
    # 1. Отсутствует обязательное поле command_id
    payload = {
        "locker_id": generate_unique_locker_id(),
        "command_type": "open",
        "status": "pending"
    }
    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 400

    # 2. Недопустимый command_type
    payload = {
        "command_id": generate_unique_command_id(),
        "locker_id": generate_unique_locker_id(),
        "command_type": "invalid_command",
        "status": "pending"
    }
    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 400

    # 3. Недопустимый status
    payload = {
        "command_id": generate_unique_command_id(),
        "locker_id": generate_unique_locker_id(),
        "command_type": "open",
        "status": "invalid_status"
    }
    response = api_client.post("/api/v1/commands/history", json=payload)
    assert response.status_code == 400
