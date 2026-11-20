import os

import yaml

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "template-scud-infra.yaml",
)

GROUP_UUID = "02cbee81499c40b39e2bcb6b91427aca"

# 6 DB instances, enumerated EXPLICITLY (no LLD, no {#INSTANCE}).
INSTANCES = ("user", "locker", "history", "auth", "notification", "reports")

INFRA_TEMPLATES = {
    "redis": ("SCUD Infra Redis", "scud-infra-redis"),
    "pgbouncer": ("SCUD Infra PgBouncer", "scud-infra-pgbouncer"),
    "postgres": ("SCUD Infra Postgres", "scud-infra-postgres"),
    "gateway": ("SCUD Infra Gateway", "scud-infra-gateway"),
}

# PgBouncer metric tokens per instance (the dbprobe $3 query key).
PGB_METRICS = ("cl_waiting", "sv_active", "maxwait", "avg_query_time")

# Postgres metric tokens per instance.
PG_METRICS = (
    "numbackends",
    "max_connections",
    "deadlocks",
    "xact_rollback",
    "longest_txn_seconds",
)


def _load():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _all_items(tpl):
    """Concrete items only — there are no item_prototypes in this template."""
    return tpl.get("items", [])


def _all_uuids(node):
    """Recursively collect every 'uuid' value anywhere in the document."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uuid" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_all_uuids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_uuids(item))
    return found


def test_zabbix_export_version_and_groups():
    doc = _load()
    export = doc["zabbix_export"]
    assert export["version"] == "7.0"
    groups = {g["uuid"]: g["name"] for g in export["template_groups"]}
    assert groups.get(GROUP_UUID) == "Templates/SCUD"
    assert "Templates/SCUD/Infra" in groups.values()


def test_four_infra_templates_present():
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    for _comp, (name, key) in INFRA_TEMPLATES.items():
        assert name in by_name, name
        assert key in by_key, key


# Outbox queue-health metric tokens (outbound_history_events → history_api).
OUTBOX_METRICS = (
    "outbox_pending",
    "outbox_oldest_age_s",
    "outbox_pending_bytes",
    "outbox_retrying",
    "outbox_deadletter",
)


def test_outbox_items_only_on_postgres_locker():
    """Outbox items exist for postgres_locker (table lives in scud_lockers) and
    NOT for the other instances — mirrors the dbprobe per-instance guard."""
    doc = _load()
    pg = next(t for t in doc["zabbix_export"]["templates"]
              if t["template"] == "scud-infra-postgres")
    keys = {i["key"] for i in pg.get("items", [])}
    for metric in OUTBOX_METRICS:
        assert f"scud.dbprobe[pg,postgres_locker,{metric}]" in keys, metric
    for inst in INSTANCES:
        if inst == "locker":
            continue
        assert f"scud.dbprobe[pg,postgres_{inst},outbox_pending]" not in keys, inst


def test_outbox_threshold_macros_present():
    """Trigger thresholds are macro-driven, not hard-coded numbers."""
    doc = _load()
    pg = next(t for t in doc["zabbix_export"]["templates"]
              if t["template"] == "scud-infra-postgres")
    macros = {m["macro"] for m in pg.get("macros", [])}
    assert {"{$OUTBOX.PENDING.HIGH}", "{$OUTBOX.OLDEST.MAX.S}",
            "{$OUTBOX.BYTES.HIGH}"} <= macros


def test_no_lld_anywhere():
    """R4: explicit enumeration only — no discovery_rules, no prototypes, no macros."""
    doc = _load()
    dumped = yaml.safe_dump(doc)
    assert "discovery_rules" not in dumped, "discovery_rules present"
    assert "item_prototypes" not in dumped, "item_prototypes present"
    assert "trigger_prototypes" not in dumped, "trigger_prototypes present"
    assert "{#INSTANCE}" not in dumped, "{#INSTANCE} LLD macro present"


def test_redis_fallback_http_master_and_dependent():
    """REDIS_PLUGIN_ABSENT fallback: one HTTP_AGENT master scraping acm /metrics,
    one DEPENDENT item extracting scud_redis_up via PROMETHEUS_PATTERN.
    No redis.info, no $REDIS.CONN, no used_memory dependents."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    redis = by_name["SCUD Infra Redis"]
    items = _all_items(redis)
    keys = {i["key"] for i in items}

    # Must NOT have the native redis plugin item
    assert not any(k.startswith("redis.info[") for k in keys), \
        "redis.info[] found — fallback should not use agent2 redis plugin"
    # Must NOT have $REDIS.CONN macro
    macros = {m["macro"] for m in redis.get("macros", [])}
    assert "{$REDIS.CONN}" not in macros, "$REDIS.CONN macro found in fallback"

    # Must have an HTTP_AGENT master item
    master_items = [i for i in items if i.get("type") == "HTTP_AGENT"]
    assert master_items, "No HTTP_AGENT master item found in scud-infra-redis"
    master = master_items[0]

    # Must have a DEPENDENT item keyed scud.infra.redis.up
    assert "scud.infra.redis.up" in keys, "scud.infra.redis.up item missing"
    dep = next(i for i in items if i["key"] == "scud.infra.redis.up")
    assert dep["type"] == "DEPENDENT"
    assert dep["master_item"]["key"] == master["key"]
    # Must have preprocessing to extract scud_redis_up
    assert dep.get("preprocessing"), "scud.infra.redis.up has no preprocessing"
    pp_types = [p["type"] for p in dep["preprocessing"]]
    assert any("PROMETHEUS" in t for t in pp_types), \
        f"Expected PROMETHEUS preprocessing, got: {pp_types}"


def test_pgbouncer_explicit_items_per_instance():
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    keys = {i["key"] for i in _all_items(by_key["scud-infra-pgbouncer"])}
    for inst in INSTANCES:
        for metric in PGB_METRICS:
            expected = f"scud.dbprobe[pgb,pgbouncer_{inst},{metric}]"
            assert expected in keys, expected
    # No pg items leaked into the pgbouncer template.
    assert not any(k.startswith("scud.dbprobe[pg,") for k in keys)


def test_postgres_explicit_items_per_instance():
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    keys = {i["key"] for i in _all_items(by_key["scud-infra-postgres"])}
    for inst in INSTANCES:
        for metric in PG_METRICS:
            expected = f"scud.dbprobe[pg,postgres_{inst},{metric}]"
            assert expected in keys, expected
    # history_partition_bytes ONLY on postgres_history.
    assert "scud.dbprobe[pg,postgres_history,history_partition_bytes]" in keys
    for inst in INSTANCES:
        if inst == "history":
            continue
        assert (
            f"scud.dbprobe[pg,postgres_{inst},history_partition_bytes]" not in keys
        ), inst


def test_gateway_tcp_service_item():
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    keys = {i["key"] for i in _all_items(by_key["scud-infra-gateway"])}
    assert "net.tcp.service[http,apache_gateway,80]" in keys


# Every mod_status field the detailed gateway dashboard plots. The leaf token
# (without the scud.gateway. prefix) maps 1:1 to a dependent item.
GATEWAY_MODSTATUS_KEYS = {
    "busy_workers", "idle_workers", "graceful_workers", "slots_total",
    "worker_util_pct", "req_per_sec", "req_rate", "bytes_per_sec",
    "bytes_per_req", "duration_per_req", "cpu_load", "uptime",
    "total_accesses", "total_kbytes", "load1",
    "sb_sending", "sb_waiting", "sb_reading", "sb_keepalive", "sb_closing",
    "sb_open",
}


def _gateway(doc):
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    return by_key["scud-infra-gateway"]


def test_gateway_modstatus_master_item():
    """One HTTP_AGENT master scrapes the INTERNAL :81 server-status?auto. The
    server (on backend_net) fetches it directly — port 81 is never published."""
    gw = _gateway(_load())
    masters = [i for i in _all_items(gw) if i.get("type") == "HTTP_AGENT"]
    assert len(masters) == 1, "expected exactly one HTTP_AGENT master on the gateway template"
    master = masters[0]
    assert master["key"] == "scud.gateway.status.raw"
    # Internal-only endpoint: port 81, NOT the public :80.
    assert master["url"] == "http://apache_gateway:81/server-status?auto", master["url"]
    assert ":80/" not in master["url"], "master must not scrape the public :80 vhost"
    assert master["value_type"] == "TEXT"
    assert master.get("timeout") == "10s"


def test_gateway_modstatus_dependents_complete():
    """Every dashboard field is a DEPENDENT item off the single master, with
    preprocessing and an on-fail handler so a missing field reads 0, not
    UNSUPPORTED (the live-verify lesson)."""
    gw = _gateway(_load())
    deps = [i for i in _all_items(gw) if i.get("type") == "DEPENDENT"]
    keys = {i["key"] for i in deps}
    for leaf in GATEWAY_MODSTATUS_KEYS:
        assert f"scud.gateway.{leaf}" in keys, f"missing dependent scud.gateway.{leaf}"
    for dep in deps:
        assert dep["master_item"]["key"] == "scud.gateway.status.raw", dep["key"]
        pp = dep.get("preprocessing")
        assert pp, f"{dep['key']}: no preprocessing"
        # Extraction step must default to 0 on a missing field.
        first = pp[0]
        assert first["type"] in ("REGEX", "JAVASCRIPT"), f"{dep['key']}: {first['type']}"
        assert first.get("error_handler") == "CUSTOM_VALUE", f"{dep['key']}: no on-fail handler"
        assert first.get("error_handler_params") == "0", f"{dep['key']}: on-fail != 0"


def test_gateway_worker_util_is_javascript_ratio():
    """worker_util_pct must be computed (busy/slots) in JS, not a raw field —
    it is the saturation signal the triggers fire on."""
    gw = _gateway(_load())
    util = next(i for i in _all_items(gw) if i["key"] == "scud.gateway.worker_util_pct")
    assert util["value_type"] == "FLOAT"
    assert util["units"] == "%"
    assert util["preprocessing"][0]["type"] == "JAVASCRIPT"
    script = util["preprocessing"][0]["parameters"][0]
    assert "BusyWorkers" in script and "Scoreboard" in script


def test_gateway_scoreboard_counts_are_javascript():
    gw = _gateway(_load())
    for leaf in ("sb_sending", "sb_waiting", "sb_reading", "sb_keepalive", "sb_closing", "sb_open", "slots_total"):
        it = next(i for i in _all_items(gw) if i["key"] == f"scud.gateway.{leaf}")
        assert it["preprocessing"][0]["type"] == "JAVASCRIPT", leaf


def test_no_preprocessing_param_has_literal_newline():
    """Zabbix encodes a preprocessing step's params as a newline-joined string,
    so a literal '\\n' inside any single parameter splits into phantom params
    and configuration.import rejects it ('unexpected parameter N'). Anchor with
    (?m)^ instead. Guard every item in the template, not just the gateway."""
    doc = _load()
    for tpl in doc["zabbix_export"]["templates"]:
        for it in _all_items(tpl):
            for step in it.get("preprocessing", []):
                for p in step.get("parameters", []):
                    assert "\n" not in str(p), \
                        f"{it['key']}: literal newline in preprocessing param {p!r}"


def test_gateway_macro_for_saturation_threshold():
    gw = _gateway(_load())
    macros = {m["macro"]: m["value"] for m in gw.get("macros", [])}
    assert "{$GATEWAY.WORKERS.HIGH.PCT}" in macros
    assert macros["{$GATEWAY.WORKERS.HIGH.PCT}"] == "85"


HOST_KEYS = {
    "system.cpu.util", "system.cpu.load[all,avg1]",
    "vm.memory.size[total]", "vm.memory.size[available]", "vm.memory.size[pavailable]",
    "vfs.fs.size[/hostfs,total]", "vfs.fs.size[/hostfs,used]", "vfs.fs.size[/hostfs,pused]",
    "system.uptime",
}


def test_host_template_has_os_metrics():
    """scud-infra-host carries the built-in host OS keys (read via pid:host +
    /hostfs); all passive so the server polls them like the dbprobe items."""
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    assert "scud-infra-host" in by_key, "scud-infra-host template missing"
    items = _all_items(by_key["scud-infra-host"])
    keys = {i["key"] for i in items}
    assert HOST_KEYS <= keys, f"missing host keys: {HOST_KEYS - keys}"
    assert all(i["type"] == "ZABBIX_PASSIVE" for i in items)
    # the disk keys must read the mounted host root, not the container fs
    assert all("/hostfs" in k for k in keys if k.startswith("vfs.fs."))


def _dbprobe_items(doc):
    return [
        item
        for tpl in doc["zabbix_export"]["templates"]
        for item in _all_items(tpl)
        if item["key"].startswith("scud.dbprobe[")
    ]


def test_dbprobe_items_have_30s_timeout():
    """A cold psql connection per passive probe takes ~10s; the server's default
    poll timeout (3-4s) marks the item UNSUPPORTED. Each dbprobe item must carry
    the Zabbix 7.0 per-item timeout: 30s (= agent2 Timeout max)."""
    doc = _load()
    items = _dbprobe_items(doc)
    # 55 base (pgb/pg) + 5 outbox (postgres_locker only) + 1 orphan media (postgres_history).
    assert len(items) == 61, f"expected 61 dbprobe items, found {len(items)}"
    for item in items:
        assert item.get("timeout") == "30s", f"{item['key']}: missing timeout: 30s"


def test_non_dbprobe_items_keep_their_timeout():
    """The redis HTTP master keeps its own 10s timeout; the dbprobe change must
    not have rewritten unrelated items."""
    doc = _load()
    for tpl in doc["zabbix_export"]["templates"]:
        for item in _all_items(tpl):
            if item["key"].startswith("scud.dbprobe["):
                continue
            if item.get("type") == "HTTP_AGENT":
                assert item.get("timeout") == "10s", f"{item['key']}: redis master timeout changed"


def test_every_uuid_is_valid_distinct_v4():
    """R8: EVERY uuid in the document (groups, templates, items, triggers,
    value_maps) is a real distinct 32-hex-lowercase v4 — no spaces, '4' at
    index 12, variant nibble in {8,9,a,b}."""
    doc = _load()
    uuids = _all_uuids(doc)
    assert uuids, "no uuids found"
    assert len(uuids) == len(set(uuids)), "duplicate uuid in document"
    for u in uuids:
        assert isinstance(u, str), u
        assert " " not in u, f"embedded space: {u!r}"
        assert len(u) == 32, f"not 32 chars: {u!r}"
        assert u == u.lower(), f"not lowercase: {u!r}"
        assert all(c in "0123456789abcdef" for c in u), f"non-hex: {u!r}"
        assert u[12] == "4", f"version nibble != 4: {u}"
        assert u[16] in "89ab", f"variant nibble not 8/9/a/b: {u}"


def test_history_orphan_media_item_only_on_postgres_history():
    doc = _load()
    by_key = {t["template"]: t for t in doc["zabbix_export"]["templates"]}
    keys = {i["key"] for i in _all_items(by_key["scud-infra-postgres"])}
    assert "scud.dbprobe[pg,postgres_history,history_orphan_media]" in keys
    for inst in INSTANCES:
        if inst == "history":
            continue
        assert f"scud.dbprobe[pg,postgres_{inst},history_orphan_media]" not in keys, inst
