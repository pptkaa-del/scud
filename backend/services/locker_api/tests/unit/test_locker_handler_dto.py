"""Unit-тесты валидации LockerCreateDTO.__post_init__.

- неизвестный transport → ValueError;
- transport='ws' → ValueError (legacy, больше не создаётся);
- transport по умолчанию 'mqtt' → без relay_id → ValueError;
- transport='mqtt' без relay_id → ValueError;
- transport='mqtt' с relay_id → ok.
"""
import pytest

from app.handlers.locker import LockerCreateDTO


def test_rejects_unknown_transport():
    with pytest.raises(ValueError):
        LockerCreateDTO(inventory_number="INV-1", transport="carrier-pigeon")


def test_rejects_ws_transport():
    """'ws' — legacy: новые ws-шкафы не создаются."""
    with pytest.raises(ValueError):
        LockerCreateDTO(inventory_number="INV-1", transport="ws", relay_id=42)


def test_mqtt_requires_relay_id():
    with pytest.raises(ValueError):
        LockerCreateDTO(inventory_number="INV-1", transport="mqtt")


def test_default_transport_is_mqtt_requires_relay_id():
    """transport опущен → дефолт 'mqtt' → без relay_id ValueError."""
    with pytest.raises(ValueError):
        LockerCreateDTO(inventory_number="INV-1")


def test_mqtt_with_relay_id_ok():
    dto = LockerCreateDTO(inventory_number="INV-1", transport="mqtt", relay_id=42)
    assert dto.transport == "mqtt"
    assert dto.relay_id == 42


def test_default_transport_with_relay_id_ok():
    dto = LockerCreateDTO(inventory_number="INV-1", relay_id=7)
    assert dto.transport == "mqtt"
    assert dto.relay_id == 7


def test_empty_inventory_number_rejected():
    with pytest.raises(ValueError):
        LockerCreateDTO(inventory_number="", transport="mqtt", relay_id=1)
