import os
import re

import yaml

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "template-scud-infra.yaml",
)

INSTANCES = ("user", "locker", "history", "auth", "notification", "reports")


def _load():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _triggers():
    return _load()["zabbix_export"].get("triggers", [])


def test_triggers_live_at_top_level_only():
    """Zabbix 7.0 YAML: triggers under zabbix_export.triggers, not per-template,
    and never as trigger_prototypes (R4: no LLD)."""
    doc = _load()
    assert doc["zabbix_export"].get("triggers"), "no/empty top-level triggers"
    for tpl in doc["zabbix_export"]["templates"]:
        assert "triggers" not in tpl, tpl["template"]
        assert "discovery_rules" not in tpl, tpl["template"]


def test_redis_unreachable_trigger_fallback():
    """REDIS_PLUGIN_ABSENT fallback: trigger fires on scud.infra.redis.up < 1,
    NOT on used_memory nodata. No memory trigger (plugin absent — internals unavailable)."""
    names = " ".join(t["name"] for t in _triggers()).lower()
    exprs = " ".join(t["expression"] for t in _triggers())
    assert "redis" in names
    assert "down" in names or "unreachable" in names
    assert "scud-infra-redis" in exprs
    assert "scud.infra.redis.up" in exprs, \
        "trigger must reference scud.infra.redis.up (fallback key), not used_memory"
    # Fallback has no memory trigger — confirm used_memory trigger is absent
    assert "used_memory" not in exprs, \
        "used_memory trigger present but redis plugin is absent in this build"


def test_pgbouncer_cl_waiting_sustained_per_instance():
    """One sustained cl_waiting>0 trigger per enumerated instance (R4)."""
    pgb = [
        t for t in _triggers()
        if "scud-infra-pgbouncer" in t["expression"] and "cl_waiting" in t["expression"]
    ]
    assert len(pgb) >= len(INSTANCES), len(pgb)
    joined = " ".join(t["expression"] for t in pgb)
    for inst in INSTANCES:
        assert f"pgbouncer_{inst}" in joined, inst
    # Sustained: a time-window aggregate (min over a window), not an instant >0.
    assert any("min(" in t["expression"] for t in pgb)


def test_postgres_connections_near_max_per_instance():
    pg = [
        t for t in _triggers()
        if "scud-infra-postgres" in t["expression"] and "numbackends" in t["expression"]
    ]
    assert len(pg) >= len(INSTANCES), len(pg)
    joined = " ".join(t["expression"] for t in pg)
    for inst in INSTANCES:
        assert f"postgres_{inst}" in joined, inst
    # numbackends compared against the max_connections item.
    assert "max_connections" in joined


def test_postgres_deadlocks_per_instance():
    pg = [
        t for t in _triggers()
        if "scud-infra-postgres" in t["expression"] and "deadlocks" in t["expression"]
    ]
    assert len(pg) >= len(INSTANCES), len(pg)
    joined = " ".join(t["expression"] for t in pg)
    for inst in INSTANCES:
        assert f"postgres_{inst}" in joined, inst


def test_gateway_down_trigger():
    gw = [t for t in _triggers() if "scud-infra-gateway" in t["expression"]]
    assert gw, "no gateway trigger"
    joined_names = " ".join(t["name"] for t in gw).lower()
    joined_exprs = " ".join(t["expression"] for t in gw)
    assert "gateway" in joined_names
    assert "down" in joined_names or "unreachable" in joined_names
    assert "net.tcp.service[http,apache_gateway,80]" in joined_exprs


def test_gateway_worker_saturation_triggers():
    """The detailed gateway monitoring adds worker-pool saturation alerting —
    the prefork ceiling that throttled the WebSocket fleet."""
    gw = [t for t in _triggers() if "scud-infra-gateway" in t["expression"]]
    util = [t for t in gw if "worker_util_pct" in t["expression"]]
    assert len(util) >= 2, "expected saturating + exhausted worker triggers"
    priorities = {t["priority"] for t in util}
    assert {"AVERAGE", "HIGH"} <= priorities, priorities
    # The soft threshold is macro-driven, not a hard-coded number.
    assert any("{$GATEWAY.WORKERS.HIGH.PCT}" in t["expression"] for t in util)
    # A scrape-health trigger fires when server-status (:81) stops answering.
    # It must target a history-bearing dependent (busy_workers), NOT the master
    # scud.gateway.status.raw (history:"0" makes nodata() unreliable).
    nodata_trigs = [t for t in gw if "nodata(" in t["expression"]]
    assert nodata_trigs, "missing server-status nodata trigger"
    for t in nodata_trigs:
        assert "scud.gateway.status.raw" not in t["expression"], \
            "nodata must not target the history:'0' master item"
        assert "scud.gateway.busy_workers" in t["expression"]


def test_outbox_resilience_triggers():
    """Outbox observability adds 4 triggers on scud-infra-postgres: backlog
    building, delivery stalled, pending-bytes (OOM precursor), dead-letter —
    macro-driven, surfacing the failure class that froze history delivery."""
    ob = [
        t for t in _triggers()
        if "scud-infra-postgres" in t["expression"] and "outbox_" in t["expression"]
    ]
    assert len(ob) >= 4, len(ob)
    exprs = " ".join(t["expression"] for t in ob)
    assert "outbox_pending]" in exprs
    assert "outbox_oldest_age_s]" in exprs
    assert "outbox_pending_bytes]" in exprs
    assert "outbox_deadletter]" in exprs
    # Пороги macro-driven, не зашитые числа.
    assert "{$OUTBOX.PENDING.HIGH}" in exprs
    assert "{$OUTBOX.OLDEST.MAX.S}" in exprs
    assert "{$OUTBOX.BYTES.HIGH}" in exprs
    # Доставка-встала и байты-давление — HIGH; бэклог/dead-letter — AVERAGE.
    assert {"AVERAGE", "HIGH"} <= {t["priority"] for t in ob}


def test_all_trigger_expressions_reference_real_items():
    """Every trigger references a key that is actually defined as an item on the
    named template (no dangling references — R8 wiring sanity)."""
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    item_keys = {
        tpl_key: {i["key"] for i in tpl.get("items", [])}
        for tpl_key, tpl in by_key.items()
    }
    # An item reference is /<template>/<key>, where <key> is either a bare
    # dotted name (e.g. scud.infra.redis.up) or a parametrised key
    # whose [..] args contain commas (e.g. scud.dbprobe[pg,postgres_user,
    # numbackends] or net.tcp.service[http,apache_gateway,80]). Match the
    # whole bracketed form first so the inner commas are not truncated.
    ref = re.compile(r"/(scud-infra-[a-z]+)/([A-Za-z0-9._]+(?:\[[^\]]*\])?)")
    for trg in _triggers():
        for tpl_key, item_key in ref.findall(trg["expression"]):
            assert tpl_key in item_keys, (trg["name"], tpl_key)
            assert item_key in item_keys[tpl_key], (trg["name"], item_key)


def test_all_trigger_uuids_unique_and_v4():
    uuids = [t["uuid"] for t in _triggers()]
    assert uuids
    assert len(uuids) == len(set(uuids)), "duplicate trigger uuid"
    for u in uuids:
        assert " " not in u, f"embedded space: {u!r}"
        assert len(u) == 32, u
        assert u[12] == "4", f"not a v4 uuid: {u}"
        assert u[16] in "89ab", f"bad variant nibble: {u}"


def test_history_orphan_media_trigger():
    """Relay-фото кадры без события дольше 30 минут — WARNING."""
    orphan = [t for t in _triggers() if "history_orphan_media]" in t["expression"]]
    assert len(orphan) == 1, orphan
    assert orphan[0]["priority"] == "WARNING"
    assert orphan[0]["expression"] == (
        "min(/scud-infra-postgres/scud.dbprobe[pg,postgres_history,history_orphan_media],30m)>0"
    )
