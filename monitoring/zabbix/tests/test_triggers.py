import os
import yaml

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "template-scud-services.yaml")
SERVICES = ["user_api", "locker_api", "history_api", "auth_api", "acm",
            "notification_api", "reports_api"]


def _load():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_six_triggers_per_service():
    """Zabbix 7.0 YAML: triggers live at zabbix_export.triggers, not per-template."""
    doc = _load()
    triggers = doc["zabbix_export"].get("triggers", [])
    by_svc: dict[str, list] = {s: [] for s in SERVICES}
    for trg in triggers:
        for svc in SERVICES:
            if f"scud-service-{svc}" in trg.get("expression", ""):
                by_svc[svc].append(trg)
                break
    for svc in SERVICES:
        trg_list = by_svc[svc]
        assert len(trg_list) >= 6, (svc, len(trg_list))
        names = " ".join(t["name"] for t in trg_list).lower()
        for token in ("down", "5xx", "waiters", "ack", "security", "bad_credential"):
            assert token in names, (svc, token)
        # all services must have interservice and DB error rate triggers
        assert "interservice error" in names, (svc, "missing interservice error trigger")
        assert "db error" in names, (svc, "missing db error trigger")

    # locker_api must have additional domain-specific triggers
    locker_names = " ".join(t["name"] for t in by_svc["locker_api"]).lower()
    assert "relay drop" in locker_names, "missing relay drop spike trigger"
    assert "backlog" in locker_names, "missing command backlog saturation trigger"
    assert "failure rate" in locker_names, "missing command failure rate trigger"


def test_slo_burn_rate_triggers_auth_api():
    """At least 2 SLO burn triggers must reference auth_api SLI items; fast-burn must be HIGH."""
    doc = _load()
    triggers = doc["zabbix_export"].get("triggers", [])
    auth_burn = [
        t for t in triggers
        if "scud-service-auth_api" in t.get("expression", "")
        and "burn" in t.get("name", "").lower()
    ]
    assert len(auth_burn) >= 2, f"expected >=2 auth_api burn triggers, got {len(auth_burn)}"

    fast = [t for t in auth_burn if "fast" in t.get("name", "").lower()]
    assert fast, "no fast-burn trigger found for auth_api"
    for t in fast:
        assert t["priority"] == "HIGH", f"fast-burn trigger must be HIGH: {t['name']}"

    slow = [t for t in auth_burn if "slow" in t.get("name", "").lower()]
    assert slow, "no slow-burn trigger found for auth_api"
    for t in slow:
        assert t["priority"] == "AVERAGE", f"slow-burn trigger must be AVERAGE: {t['name']}"


def test_slo_burn_rate_triggers_locker_api():
    """At least 2 SLO burn triggers must reference locker_api SLI items; fast-burn must be HIGH."""
    doc = _load()
    triggers = doc["zabbix_export"].get("triggers", [])
    locker_burn = [
        t for t in triggers
        if "scud-service-locker_api" in t.get("expression", "")
        and "burn" in t.get("name", "").lower()
    ]
    assert len(locker_burn) >= 2, f"expected >=2 locker_api burn triggers, got {len(locker_burn)}"

    fast = [t for t in locker_burn if "fast" in t.get("name", "").lower()]
    assert fast, "no fast-burn trigger found for locker_api"
    for t in fast:
        assert t["priority"] == "HIGH", f"fast-burn trigger must be HIGH: {t['name']}"

    slow = [t for t in locker_burn if "slow" in t.get("name", "").lower()]
    assert slow, "no slow-burn trigger found for locker_api"
    for t in slow:
        assert t["priority"] == "AVERAGE", f"slow-burn trigger must be AVERAGE: {t['name']}"


def test_trigger_expressions_reference_item_keys():
    doc = _load()
    triggers = doc["zabbix_export"].get("triggers", [])
    for svc in SERVICES:
        svc_triggers = [t for t in triggers if f"scud-service-{svc}" in t.get("expression", "")]
        expr = " ".join(t["expression"] for t in svc_triggers)
        assert f"scud.metrics.raw[{svc}]" in expr, f"nodata() trigger missing for {svc}"
        assert f"scud.metric.{svc}.scud_db_pool_connections_waiters" in expr, svc
        assert f"scud.metric.{svc}.scud_interservice_errors" in expr, svc
        assert f"scud.metric.{svc}.scud_db_errors_total" in expr, svc
