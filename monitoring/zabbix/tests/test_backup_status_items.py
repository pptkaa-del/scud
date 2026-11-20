"""Структурные тесты для scud.backup.status: item + triggers в infra-шаблоне."""
import os
import yaml

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "template-scud-infra.yaml",
)
USERPARAMS = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..", "monitoring", "zabbix-agent2",
    "zabbix_agent2.userparams.conf",
)


def _load():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _triggers():
    return _load()["zabbix_export"].get("triggers", [])


def test_backup_template_present():
    """Шаблон scud-infra-backup должен существовать в документе."""
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    assert "scud-infra-backup" in by_key, "шаблон scud-infra-backup отсутствует"


def test_backup_status_item_present():
    """Item scud.backup.status должен быть в шаблоне scud-infra-backup."""
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    assert "scud-infra-backup" in by_key, "scud-infra-backup template missing"
    tpl = by_key["scud-infra-backup"]
    keys = {i["key"] for i in tpl.get("items", [])}
    assert "scud.backup.status" in keys, f"item scud.backup.status не найден; items: {keys}"


def test_backup_status_item_type_active():
    """Item scud.backup.status должен быть ZABBIX_ACTIVE."""
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_key["scud-infra-backup"]
    item = next(i for i in tpl.get("items", []) if i["key"] == "scud.backup.status")
    assert item["type"] == "ZABBIX_ACTIVE", f"ожидался ZABBIX_ACTIVE, получен {item['type']}"


def test_backup_fail_trigger_high():
    """Триггер HIGH на FAIL должен существовать и ссылаться на scud.backup.status."""
    backup_triggers = [
        t for t in _triggers()
        if "scud-infra-backup" in t["expression"] and "scud.backup.status" in t["expression"]
    ]
    fail_triggers = [t for t in backup_triggers if "FAIL" in t["expression"]]
    assert fail_triggers, "триггер на FAIL для scud.backup.status не найден"
    assert any(t["priority"] == "HIGH" for t in fail_triggers), \
        f"ни один FAIL-триггер не имеет priority=HIGH: {[t['priority'] for t in fail_triggers]}"


def test_backup_nodata_trigger_average():
    """Триггер AVERAGE на nodata(>25h) должен существовать."""
    backup_triggers = [
        t for t in _triggers()
        if "scud-infra-backup" in t["expression"] and "scud.backup.status" in t["expression"]
    ]
    nodata_triggers = [t for t in backup_triggers if "nodata" in t["expression"]]
    assert nodata_triggers, "триггер nodata для scud.backup.status не найден"
    assert any(t["priority"] == "AVERAGE" for t in nodata_triggers), \
        f"nodata-триггер не имеет priority=AVERAGE: {[t['priority'] for t in nodata_triggers]}"
    # Порог — 90000 секунд (>25 часов).
    assert any("90000" in t["expression"] for t in nodata_triggers), \
        "nodata-триггер не содержит порог 90000 с"


def test_userparams_contains_backup_status():
    """zabbix_agent2.userparams.conf должен содержать scud.backup.status."""
    with open(USERPARAMS, encoding="utf-8") as fh:
        content = fh.read()
    assert "scud.backup.status" in content, \
        "scud.backup.status не найден в zabbix_agent2.userparams.conf"
