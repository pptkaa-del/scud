import os
import yaml

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "template-scud-services.yaml")

SERVICES = {
    "user_api": 8000, "locker_api": 8001, "history_api": 8002,
    "auth_api": 8003, "acm": 8004, "notification_api": 8005, "reports_api": 8006,
}
KEY_METRICS = [
    "scud_http_requests_total",
    "scud_http_request_duration_seconds",
    "scud_db_pool_connections_waiters",
    "scud_command_lifecycle_ack_timeout_failed",
    "scud_security_events_total",
    "scud_login_attempts_total",
    "scud_interservice_errors",
    "scud_db_errors_total",
]

# Metrics that only locker_api emits (NOT required on other services).
LOCKER_ONLY_METRICS = [
    "scud_command_pending_total",
    "scud_command_lifecycle_failed",
    "scud_ws_events_supersession",
    "scud_mqtt_relay_messages_dropped",
    "scud_relay_photo_frames_reset",
]


def _load():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_zabbix_export_version_and_templates():
    doc = _load()
    assert doc["zabbix_export"]["version"] == "7.0"
    names = {t["name"] for t in doc["zabbix_export"]["templates"]}
    assert {f"SCUD Service {s}" for s in SERVICES} <= names


def test_each_template_has_master_item_pointing_to_metrics():
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    for svc, port in SERVICES.items():
        tpl = by_name[f"SCUD Service {svc}"]
        master = next(i for i in tpl["items"] if i["key"] == f"scud.metrics.raw[{svc}]")
        assert master["type"] == "HTTP_AGENT"
        assert master["url"] == f"http://{svc}:{port}/metrics"
        steps = master["preprocessing"]
        assert any(s["type"] == "PROMETHEUS_TO_JSON" for s in steps)


def test_each_template_has_lld_and_dependent_items():
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    for svc in SERVICES:
        tpl = by_name[f"SCUD Service {svc}"]
        assert any(r["key"] == f"scud.metrics.lld[{svc}]" for r in tpl["discovery_rules"])
        dep_keys = {i["key"] for i in tpl["items"]}
        for m in KEY_METRICS:
            assert any(k.startswith(f"scud.metric.{svc}.{m}") for k in dep_keys), (svc, m)


def test_locker_api_has_domain_specific_items():
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service locker_api"]
    dep_keys = {i["key"] for i in tpl["items"]}
    for m in LOCKER_ONLY_METRICS:
        assert any(k.startswith(f"scud.metric.locker_api.{m}") for k in dep_keys), m


def test_all_uuids_unique():
    doc = _load()
    seen: set[str] = set()
    # collect UUIDs from templates, items, discovery_rules, item_prototypes, triggers
    for tpl in doc["zabbix_export"]["templates"]:
        seen_add = seen.add
        seen_add(tpl["uuid"])
        for item in tpl.get("items", []):
            assert item["uuid"] not in seen, f"duplicate uuid {item['uuid']}"
            seen_add(item["uuid"])
        for rule in tpl.get("discovery_rules", []):
            assert rule["uuid"] not in seen, f"duplicate uuid {rule['uuid']}"
            seen_add(rule["uuid"])
            for proto in rule.get("item_prototypes", []):
                assert proto["uuid"] not in seen, f"duplicate uuid {proto['uuid']}"
                seen_add(proto["uuid"])
    for trg in doc["zabbix_export"].get("triggers", []):
        assert trg["uuid"] not in seen, f"duplicate uuid {trg['uuid']}"
        seen.add(trg["uuid"])


def test_auth_api_has_sli_dependent_items():
    """auth_api template must contain SLI-A login and SLI-C OTP dependent items."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service auth_api"]
    item_keys = {i["key"] for i in tpl["items"]}

    # SLI-A login success counters
    assert "scud.metric.auth_api.scud_login_success_no2fa" in item_keys
    assert "scud.metric.auth_api.scud_login_success_2fa" in item_keys
    assert "scud.metric.auth_api.scud_login_attempts_all" in item_keys

    # SLI-C OTP counters
    assert "scud.metric.auth_api.scud_otp_issue_delivered" in item_keys
    assert "scud.metric.auth_api.scud_otp_issue_total" in item_keys
    assert "scud.metric.auth_api.scud_otp_issue_failed" in item_keys

    # Verify preprocessing idiom for a representative SLI-A item
    login_all = next(i for i in tpl["items"] if i["key"] == "scud.metric.auth_api.scud_login_attempts_all")
    assert login_all["type"] == "DEPENDENT"
    pp_types = [s["type"] for s in login_all["preprocessing"]]
    assert "JSONPATH" in pp_types
    assert "SIMPLE_CHANGE" in pp_types


def test_auth_api_has_sli_calculated_items():
    """auth_api template must contain SLI-A and SLI-C CALCULATED burn-rate items."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service auth_api"]
    item_keys = {i["key"] for i in tpl["items"]}

    assert "scud.sli.auth_api.login_burn_fast" in item_keys
    assert "scud.sli.auth_api.login_burn_slow" in item_keys
    assert "scud.sli.auth_api.otp_burn_fast" in item_keys
    assert "scud.sli.auth_api.otp_burn_slow" in item_keys

    burn_fast = next(i for i in tpl["items"] if i["key"] == "scud.sli.auth_api.login_burn_fast")
    assert burn_fast["type"] == "CALCULATED"
    assert "params" in burn_fast
    assert "0.005" in burn_fast["params"]


def test_locker_api_has_sli_bucket_items():
    """locker_api template must contain SLI-B histogram bucket and count dependent items."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service locker_api"]
    item_keys = {i["key"] for i in tpl["items"]}

    assert "scud.metric.locker_api.scud_cmd_queue_bucket_5s" in item_keys
    assert "scud.metric.locker_api.scud_cmd_queue_count" in item_keys
    assert "scud.metric.locker_api.scud_cmd_ack_bucket_5s" in item_keys
    assert "scud.metric.locker_api.scud_cmd_ack_count" in item_keys

    # Bucket items must NOT have SIMPLE_CHANGE (cumulative counters)
    bucket_item = next(i for i in tpl["items"] if i["key"] == "scud.metric.locker_api.scud_cmd_queue_bucket_5s")
    assert bucket_item["type"] == "DEPENDENT"
    pp_types = [s["type"] for s in bucket_item["preprocessing"]]
    assert "JSONPATH" in pp_types
    assert "SIMPLE_CHANGE" not in pp_types


def test_locker_api_has_sli_calculated_items():
    """locker_api template must contain SLI-B CALCULATED burn-rate items."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service locker_api"]
    item_keys = {i["key"] for i in tpl["items"]}

    assert "scud.sli.locker_api.cmd_latency_burn_fast" in item_keys
    assert "scud.sli.locker_api.cmd_latency_burn_slow" in item_keys

    burn_fast = next(i for i in tpl["items"] if i["key"] == "scud.sli.locker_api.cmd_latency_burn_fast")
    assert burn_fast["type"] == "CALCULATED"
    assert "params" in burn_fast
    assert "0.01" in burn_fast["params"]


def test_history_api_has_partition_failure_item_and_trigger():
    """history_api должен содержать dependent-item и trigger для scud_partition_create_failures_total."""
    doc = _load()
    by_name = {t["name"]: t for t in doc["zabbix_export"]["templates"]}
    tpl = by_name["SCUD Service history_api"]

    expected_key = "scud.metric.history_api.scud_partition_create_failures_total"
    item_keys = {i["key"] for i in tpl["items"]}
    assert expected_key in item_keys, f"item key {expected_key!r} not found in history_api template"

    # Проверяем структуру item: DEPENDENT, master_item указывает на raw-scrape.
    item = next(i for i in tpl["items"] if i["key"] == expected_key)
    assert item["type"] == "DEPENDENT"
    assert item["master_item"]["key"] == "scud.metrics.raw[history_api]"
    # Препроцессинг: JSONPATH + SIMPLE_CHANGE (зеркало idiom scud_db_errors_total)
    pp_types = [s["type"] for s in item["preprocessing"]]
    assert "JSONPATH" in pp_types
    assert "SIMPLE_CHANGE" in pp_types

    # Trigger на partition-create failures должен существовать в top-level triggers.
    triggers = doc["zabbix_export"].get("triggers", [])
    partition_triggers = [
        t for t in triggers
        if "partition_create_failures_total" in t.get("expression", "")
        and "history_api" in t.get("expression", "")
    ]
    assert partition_triggers, "trigger for history_api scud_partition_create_failures_total not found"
    trg = partition_triggers[0]
    assert trg["priority"] == "AVERAGE"


# ---------------------------------------------------------------------------
# On-fail=0 handler on dependent-metric JSONPATH steps (Finding #3)
# ---------------------------------------------------------------------------

ON_FAIL_HANDLER = "CUSTOM_VALUE"
ON_FAIL_VALUE = "0"


def _scalar_metric_jsonpath_steps(doc):
    """Yield every dependent scud.metric.* JSONPATH preprocessing step whose
    parameter aggregates a (possibly empty) array via ``.value.sum()``."""
    for tmpl in doc["zabbix_export"]["templates"]:
        for item in tmpl.get("items", []):
            for step in item.get("preprocessing", []):
                if step.get("type") == "JSONPATH" and step["parameters"][0].endswith(".value.sum()"):
                    yield item["key"], step


def test_all_scalar_metric_jsonpath_have_onfail_zero():
    """On a quiet stand a labeled counter emits no series yet; .sum() on the empty
    selection must fall back to 0, not leave the item UNSUPPORTED."""
    doc = _load()
    steps = list(_scalar_metric_jsonpath_steps(doc))
    assert len(steps) == 86, f"expected 86 scalar metric JSONPATH steps, found {len(steps)}"
    for key, step in steps:
        assert step.get("error_handler") == ON_FAIL_HANDLER, f"{key}: missing Custom-on-fail handler"
        assert step.get("error_handler_params") == ON_FAIL_VALUE, f"{key}: on-fail value must be 0"


def test_lld_filter_jsonpath_have_no_onfail():
    """LLD discovery filters select counter/gauge series; an empty result there is
    valid (no series) and must NOT be coerced to 0."""
    doc = _load()
    for tmpl in doc["zabbix_export"]["templates"]:
        for rule in tmpl.get("discovery_rules", []):
            for step in rule.get("preprocessing", []):
                if step.get("type") == "JSONPATH" and "@.type==" in step["parameters"][0]:
                    assert "error_handler" not in step, "LLD filter must not carry on-fail handler"


# ---------------------------------------------------------------------------
# SLI calculated items must use host-agnostic self-reference (Finding #4)
# ---------------------------------------------------------------------------

def _sli_calculated_items(doc):
    return [
        item
        for tmpl in doc["zabbix_export"]["templates"]
        for item in tmpl.get("items", [])
        if item.get("type") == "CALCULATED" and item["key"].startswith("scud.sli.")
    ]


def test_sli_ratio_params_guard_against_zero_denominator():
    """Error-ratio / fraction SLIs divide by a counter that is 0 on a quiet stand;
    without a guard that is a division-by-zero -> UNSUPPORTED (and dead burn-rate
    triggers). Each must protect the denominator with the Zabbix '+ (D=0)' idiom."""
    doc = _load()
    guarded_suffixes = ("error_ratio_5m", "error_ratio_1h", "fraction_5m", "fraction_1h")
    sli = [
        i for i in _sli_calculated_items(doc)
        if i["key"].rsplit(".", 1)[-1].endswith(guarded_suffixes)
    ]
    assert len(sli) == 6, f"expected 6 ratio/fraction SLIs, found {len(sli)}"
    for item in sli:
        assert "=0))" in item["params"], f"{item['key']}: denominator not zero-guarded"


def test_sli_calculated_params_use_self_reference():
    """Calculated SLI formulas referenced /scud-service-<svc>/ — the TEMPLATE name,
    not the live host (scud-<svc>) — so they pointed at a non-existent host and
    stayed UNSUPPORTED. They must use the host-agnostic //key self-reference."""
    doc = _load()
    sli = _sli_calculated_items(doc)
    assert len(sli) == 12, f"expected 12 SLI calculated items, found {len(sli)}"
    for item in sli:
        params = item["params"]
        assert "/scud-service-" not in params, f"{item['key']}: still references template host"
        assert "//scud." in params, f"{item['key']}: must use //key self-reference"
