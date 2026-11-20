"""Gateway+ACM end-to-end: создание mqtt-локера через Apache → ACM → locker_api.

Проверяет, что POST /api/v1/lockers с transport=mqtt + relay_id проходит весь
путь (Apache ProxyPass → ACM `POST ^/api/v1/lockers$` → OPERATOR → locker_api),
шкаф создаётся с правильными полями, а handler провижинит broker-креды relay-{id}
в shared config volume (mqtt_config), не падая на этом шаге.

ACM-правило и ProxyPass для /lockers уже существуют (coarse) — отдельных
изменений gateway/ACM для mqtt-полей не требуется: транспорт валидируется
в самом handler'е.
"""
import time

from conftest import GATEWAY_URL


def _relay_id() -> int:
    # Уникальный relay_id в диапазоне uint32 (partial unique index в БД).
    return 800_000 + int(time.time() * 1000) % 100_000


def test_operator_creates_mqtt_locker_through_gateway(operator_session):
    session, _ = operator_session  # fixture returns (session, user_id)
    relay_id = _relay_id()
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/lockers",
        json={
            "inventory_number": f"GW-MQTT-{int(time.time() * 1000) % 10**8}",
            "transport": "mqtt",
            "relay_id": relay_id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["transport"] == "mqtt"
    assert body["relay_id"] == relay_id

    # cleanup (soft-delete через DELETE)
    session.delete(f"{GATEWAY_URL}/api/v1/locker/{body['id']}")


def test_mqtt_locker_requires_relay_id(operator_session):
    session, _ = operator_session  # fixture returns (session, user_id)
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/lockers",
        json={
            "inventory_number": f"GW-MQTT-BAD-{int(time.time() * 1000) % 10**8}",
            "transport": "mqtt",
        },
    )
    # transport=mqtt без relay_id отклоняется валидацией DTO (400), не 201.
    assert resp.status_code == 400, resp.text
