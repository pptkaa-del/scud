"""Structural and provisioner-unit tests for alert-delivery.yaml and related provision.py logic."""
import logging
import os
import sys
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import provision  # noqa: E402

ALERT_YAML = os.path.join(os.path.dirname(__file__), "..", "alert-delivery.yaml")


def _load():
    with open(ALERT_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Structural YAML tests (media type — the only importable export section)
# ---------------------------------------------------------------------------

def test_media_type_is_webhook():
    doc = _load()
    media_types = doc["zabbix_export"]["media_types"]
    assert len(media_types) >= 1
    mt = media_types[0]
    assert mt["type"] == "WEBHOOK"
    assert mt["name"] == "Max Messenger"


def test_media_type_token_parameter_references_macro():
    doc = _load()
    mt = doc["zabbix_export"]["media_types"][0]
    params_by_name = {p["name"]: p for p in mt["parameters"]}
    assert "token" in params_by_name, "parameter 'token' missing"
    assert "{$MAX.BOT.TOKEN}" in params_by_name["token"]["value"]


def test_media_type_has_required_parameters():
    doc = _load()
    mt = doc["zabbix_export"]["media_types"][0]
    names = {p["name"] for p in mt["parameters"]}
    assert "token" in names
    assert "max_user_id" in names
    assert "subject" in names
    assert "message" in names


def test_media_type_timeout_and_no_process_tags():
    doc = _load()
    mt = doc["zabbix_export"]["media_types"][0]
    assert mt.get("timeout") == "30s"
    # process_tags should be NO or absent (falsy)
    process_tags = mt.get("process_tags", "NO")
    assert process_tags in ("NO", False, None, "")


def test_export_only_contains_importable_sections():
    """Zabbix 7.0 configuration.import rejects usergroups/users/actions tags."""
    export = _load()["zabbix_export"]
    for forbidden in ("usergroups", "users", "actions"):
        assert forbidden not in export, (
            f"{forbidden!r} is not importable via configuration.import in Zabbix 7.0"
        )


def test_all_uuids_unique():
    doc = _load()
    seen: set[str] = set()
    export = doc["zabbix_export"]
    for section in ("media_types",):
        for obj in export.get(section, []):
            uid = obj.get("uuid")
            if uid is not None:
                assert uid not in seen, f"duplicate uuid {uid!r} in section {section}"
                seen.add(uid)


def test_yaml_is_valid_zabbix_export():
    doc = _load()
    assert "zabbix_export" in doc
    assert doc["zabbix_export"]["version"] == "7.0"


# ---------------------------------------------------------------------------
# Provisioner unit tests
# ---------------------------------------------------------------------------

def _spy_factory():
    captured = []

    def _spy(method, params, url, auth=None):
        captured.append({"method": method, "params": params})
        return True

    return _spy, captured


def test_import_alert_delivery_uses_mediatypes_rule_key():
    """Zabbix 7.0 rules schema only accepts the camelCase ``mediaTypes`` key."""
    spy, captured = _spy_factory()
    with mock.patch("provision.api_call", side_effect=spy):
        provision.import_alert_delivery("http://z/api_jsonrpc.php", "tok", "zabbix_export: {}")
    assert captured, "api_call was not called"
    call = captured[0]
    assert call["method"] == "configuration.import"
    rules = call["params"]["rules"]
    assert "mediaTypes" in rules, "mediaTypes missing from import rules"
    # The un-importable sections must NOT be in the rules.
    for forbidden in ("media_types", "actions", "usergroups", "users"):
        assert forbidden not in rules, f"{forbidden} must not be an import rule key"


def test_import_alert_delivery_rules_are_idempotent():
    spy, captured = _spy_factory()
    with mock.patch("provision.api_call", side_effect=spy):
        provision.import_alert_delivery("http://z/api_jsonrpc.php", "tok", "zabbix_export: {}")
    rules = captured[0]["params"]["rules"]
    assert rules["mediaTypes"].get("createMissing") is True
    assert rules["mediaTypes"].get("updateExisting") is True


def test_provision_alert_routing_creates_group_user_action_when_absent():
    """When nothing exists yet, the group, user and action are all created."""
    calls = []

    def _api(method, params, url, auth=None):
        calls.append((method, params))
        if method.endswith(".get"):
            if method == "mediatype.get":
                return [{"mediatypeid": "10"}]  # media type already imported
            return []  # group / user / action absent
        if method == "usergroup.create":
            return {"usrgrpids": ["20"]}
        if method == "user.create":
            return {"userids": ["30"]}
        if method == "action.create":
            return {"actionids": ["40"]}
        return True

    with mock.patch("provision.api_call", side_effect=_api):
        provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")

    created = {m for m, _ in calls if m.endswith(".create")}
    assert "usergroup.create" in created
    assert "user.create" in created
    assert "action.create" in created


def test_provision_alert_routing_idempotent_when_all_present():
    """When the group, user and action already exist, no .create call is made."""
    calls = []

    def _api(method, params, url, auth=None):
        calls.append((method, params))
        if method == "mediatype.get":
            return [{"mediatypeid": "10"}]
        if method == "usergroup.get":
            return [{"usrgrpid": "20"}]
        if method == "user.get":
            return [{"userid": "30"}]
        if method == "action.get":
            return [{"actionid": "40"}]
        return True

    with mock.patch("provision.api_call", side_effect=_api):
        provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")

    created = [m for m, _ in calls if m.endswith(".create")]
    assert not created, f"unexpected create calls on idempotent run: {created}"


def test_provision_alert_routing_skips_when_media_type_missing():
    """Without the imported media type, routing provisioning is skipped safely."""
    calls = []

    def _api(method, params, url, auth=None):
        calls.append((method, params))
        if method == "mediatype.get":
            return []  # media type not imported
        return True

    with mock.patch("provision.api_call", side_effect=_api):
        provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")

    created = [m for m, _ in calls if m.endswith(".create")]
    assert not created, "must not create routing entities without media type"


def test_oncall_media_severity_includes_high_bit():
    """Media severity mask must have the High bit (16) set — «32» (Disaster only) is wrong."""
    captured_user = {}

    def _api(method, params, url, auth=None):
        if method == "mediatype.get":
            return [{"mediatypeid": "10"}]
        if method.endswith(".get"):
            return []
        if method == "usergroup.create":
            return {"usrgrpids": ["20"]}
        if method == "user.create":
            captured_user.update(params)
            return {"userids": ["30"]}
        if method == "action.create":
            return {"actionids": ["40"]}
        return True

    with mock.patch("provision.api_call", side_effect=_api):
        provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")

    assert captured_user, "user.create was not called"
    medias = captured_user.get("medias", [])
    assert medias, "medias list is empty in user.create params"
    severity = int(medias[0]["severity"])
    assert severity & 16 == 16, (
        f"severity mask {severity} does not include High(16) — on-call would miss HIGH alerts"
    )


def test_provision_alert_routing_action_filters_on_high_severity():
    """The created action filters on TRIGGER_SEVERITY >= HIGH and sends a message."""
    captured_action = {}

    def _api(method, params, url, auth=None):
        if method == "mediatype.get":
            return [{"mediatypeid": "10"}]
        if method.endswith(".get"):
            return []
        if method == "usergroup.create":
            return {"usrgrpids": ["20"]}
        if method == "user.create":
            return {"userids": ["30"]}
        if method == "action.create":
            captured_action.update(params)
            return {"actionids": ["40"]}
        return True

    with mock.patch("provision.api_call", side_effect=_api):
        provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")

    conds = captured_action["filter"]["conditions"]
    assert any(
        c["conditiontype"] == provision._COND_TRIGGER_SEVERITY
        and c["operator"] == provision._OPERATOR_GE
        and c["value"] == provision._SEVERITY_HIGH
        for c in conds
    ), "action must filter on TRIGGER_SEVERITY >= HIGH"
    op_types = {op["operationtype"] for op in captured_action["operations"]}
    assert provision._OP_SEND_MESSAGE in op_types
    assert captured_action.get("recovery_operations"), "recovery_operations missing"


def test_provision_max_token_skipped_when_env_unset(capfd):
    """When MAX_BOT_TOKEN is not set, macro call is skipped and no token value appears in logs."""
    spy, captured = _spy_factory()
    env = {k: v for k, v in os.environ.items() if k != "MAX_BOT_TOKEN"}
    with mock.patch("provision.api_call", side_effect=spy):
        with mock.patch.dict(os.environ, env, clear=True):
            provision.provision_max_token("http://z/api_jsonrpc.php", "tok")
    # No macro API calls should have been made
    macro_calls = [c for c in captured if "usermacro" in c["method"]]
    assert not macro_calls, f"unexpected macro API calls: {macro_calls}"


def test_provision_max_token_never_logs_value(caplog):
    """Sentinel token value must not appear in any log output regardless of env state."""
    sentinel = "SENTINEL_TOKEN_ABC123"

    spy, _ = _spy_factory()

    with caplog.at_level(logging.DEBUG, logger="provision"):
        with mock.patch("provision.api_call", side_effect=spy):
            with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN": ""}, clear=False):
                provision.provision_max_token("http://z/api_jsonrpc.php", "tok")

    for record in caplog.records:
        assert sentinel not in record.getMessage(), \
            f"sentinel token appeared in log: {record.getMessage()}"


def test_provision_max_token_failure_does_not_leak_sentinel(caplog):
    """Even if the server echoes the token in an error, the warning log must not contain it."""
    sentinel = "SENTINEL_TOKEN_LEAK_CHECK"

    def _echo_exc(method, params, url, auth=None):
        if "usermacro" in method:
            # Simulate a hostile server that echoes the token value in its error message.
            raise provision.ZabbixApiError(
                f"macro value rejected: {sentinel}"
            )
        return True

    with caplog.at_level(logging.WARNING, logger="provision"):
        with mock.patch("provision.api_call", side_effect=_echo_exc):
            with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN": sentinel}, clear=False):
                provision.provision_max_token("http://z/api_jsonrpc.php", "tok")

    # A warning must have been emitted (branch executed).
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "expected at least one WARNING log from the failure branch"

    # The sentinel must not appear anywhere in the captured log output.
    full_log = caplog.text
    assert sentinel not in full_log, (
        f"sentinel token leaked into log output: {full_log!r}"
    )


def test_media_type_max_user_id_uses_alert_sendto():
    """The webhook recipient must come from the per-user media (``{ALERT.SENDTO}``).

    A hard-coded empty ``max_user_id`` posts to ``?user_id=`` and reaches nobody,
    regardless of the user's media ``sendto`` — so the parameter must reference the
    macro that Zabbix substitutes with the recipient's "Send to" value.
    """
    doc = _load()
    mt = doc["zabbix_export"]["media_types"][0]
    params_by_name = {p["name"]: p for p in mt["parameters"]}
    assert params_by_name["max_user_id"]["value"] == "{ALERT.SENDTO}"


def _routing_api(captured, *, user_exists):
    """Build a mock ``api_call`` that records calls and reports the on-call user
    as present (``user_exists``) or absent."""

    def _api(method, params, url, auth=None):
        captured.append((method, params))
        if method == "mediatype.get":
            return [{"mediatypeid": "10"}]
        if method == "usergroup.get":
            return [{"usrgrpid": "20"}]
        if method == "user.get":
            return [{"userid": "30"}] if user_exists else []
        if method == "action.get":
            return [{"actionid": "40"}]
        if method == "usergroup.create":
            return {"usrgrpids": ["20"]}
        if method == "user.create":
            return {"userids": ["30"]}
        if method == "action.create":
            return {"actionids": ["40"]}
        return True

    return _api


def test_oncall_media_enabled_with_recipient_on_create():
    """With ONCALL_MAX_USER_ID set, a freshly created user gets an ENABLED media
    addressed to that recipient."""
    captured: list = []
    with mock.patch("provision.api_call", side_effect=_routing_api(captured, user_exists=False)):
        with mock.patch.dict(os.environ, {"ONCALL_MAX_USER_ID": "12345"}, clear=False):
            provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")
    create = next(p for m, p in captured if m == "user.create")
    media = create["medias"][0]
    assert media["sendto"] == "12345"
    assert media["active"] == provision._MEDIA_STATUS_ENABLED


def test_oncall_media_disabled_without_recipient(caplog):
    """Without ONCALL_MAX_USER_ID the media is left DISABLED with the placeholder
    (no failed sends in the action log) and a warning is logged."""
    captured: list = []
    env = {k: v for k, v in os.environ.items() if k != "ONCALL_MAX_USER_ID"}
    with caplog.at_level(logging.WARNING, logger="provision"):
        with mock.patch("provision.api_call", side_effect=_routing_api(captured, user_exists=False)):
            with mock.patch.dict(os.environ, env, clear=True):
                provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")
    create = next(p for m, p in captured if m == "user.create")
    media = create["medias"][0]
    assert media["active"] == provision._MEDIA_STATUS_DISABLED
    assert media["sendto"] == provision._ONCALL_PLACEHOLDER_SENDTO
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_existing_user_media_patched_via_user_update():
    """Re-running against an existing on-call user must patch its media via
    user.update (regression: previously the whole user block was skipped, so the
    media never got re-applied)."""
    captured: list = []
    with mock.patch("provision.api_call", side_effect=_routing_api(captured, user_exists=True)):
        with mock.patch.dict(os.environ, {"ONCALL_MAX_USER_ID": "999"}, clear=False):
            provision.provision_alert_routing("http://z/api_jsonrpc.php", "tok")
    assert not any(m == "user.create" for m, _ in captured)
    upd = next(p for m, p in captured if m == "user.update")
    assert upd["userid"] == "30"
    assert upd["medias"][0]["sendto"] == "999"
    assert upd["medias"][0]["active"] == provision._MEDIA_STATUS_ENABLED


def test_provision_max_token_with_value_set_calls_createglobal(caplog):
    """When MAX_BOT_TOKEN is set, createglobal is attempted and value is never logged."""
    sentinel = "SENTINEL_TOKEN_XYZ789"
    spy, captured = _spy_factory()

    with caplog.at_level(logging.DEBUG, logger="provision"):
        with mock.patch("provision.api_call", side_effect=spy):
            with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN": sentinel}, clear=False):
                provision.provision_max_token("http://z/api_jsonrpc.php", "tok")

    # Some macro API call must have been attempted
    macro_calls = [c for c in captured if "usermacro" in c["method"]]
    assert macro_calls, "no usermacro call made when MAX_BOT_TOKEN is set"

    # Token value must not appear in any log message
    for record in caplog.records:
        assert sentinel not in record.getMessage(), \
            f"token value appeared in log: {record.getMessage()}"
