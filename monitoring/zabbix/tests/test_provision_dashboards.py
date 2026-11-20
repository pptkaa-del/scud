"""Unit tests for provision_dashboards.py (mock Zabbix JSON-RPC)."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import provision  # noqa: E402,F401  (imported so provision_dashboards can resolve it)
import provision_dashboards as pd  # noqa: E402


def _fake_api(captured, *, dashboard_exists=False, missing_keys=()):
    """Mock provision.api_call. host.get/item.get resolve deterministic ids;
    item.get returns [] for keys in ``missing_keys``."""

    def _api(method, params, url, auth=None):
        captured.append((method, params))
        if method == "host.get":
            return [{"hostid": "H-" + params["filter"]["host"]}]
        if method == "item.get":
            key = params["filter"]["key_"]
            if key in missing_keys:
                return []
            return [{"itemid": "I-" + key}]
        if method == "dashboard.get":
            return [{"dashboardid": "7"}] if dashboard_exists else []
        if method == "dashboard.create":
            return {"dashboardids": ["1"]}
        if method == "dashboard.update":
            return {}
        return []

    return _api


def _created(captured):
    return [p for m, p in captured if m == "dashboard.create"]


def test_creates_five_dashboards_with_scud_keys():
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    created = _created(captured)
    names = {c["name"] for c in created}
    assert names == {
        pd.DASH_SERVICE_OVERVIEW, pd.DASH_INFRA, pd.DASH_SLO,
        pd.DASH_GATEWAY, pd.DASH_OUTBOX,
    }
    # A known widget must bind the resolved itemid of a real scud.* key.
    all_field_values = [
        f["value"]
        for c in created
        for page in c["pages"]
        for w in page["widgets"]
        for f in w["fields"]
        if f["type"] == pd._FIELD_TYPE_ITEM
    ]
    assert "I-scud.metric.locker_api.scud_http_requests_5xx" in all_field_values
    assert "I-scud.dbprobe[pg,postgres_user,deadlocks]" in all_field_values
    assert "I-scud.sli.auth_api.login_burn_fast" in all_field_values
    # Gateway dashboard binds the mod_status worker-saturation series.
    assert "I-scud.gateway.worker_util_pct" in all_field_values
    assert "I-scud.gateway.busy_workers" in all_field_values
    # Outbox dashboard binds the outbox dbprobe series.
    assert "I-scud.dbprobe[pg,postgres_locker,outbox_pending]" in all_field_values
    assert "I-scud.dbprobe[pg,postgres_locker,outbox_deadletter]" in all_field_values


def test_idempotent_updates_when_dashboard_exists():
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured, dashboard_exists=True)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    assert not _created(captured), "must not create when dashboards already exist"
    updates = [p for m, p in captured if m == "dashboard.update"]
    assert {u["dashboardid"] for u in updates} == {"7"}
    assert len(updates) == 5


def test_widget_skipped_when_item_missing():
    captured = []
    missing = ("scud.metric.acm.scud_http_requests_5xx",)
    with mock.patch("provision.api_call", side_effect=_fake_api(captured, missing_keys=missing)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    created = _created(captured)
    # Dashboards still built; the missing item never appears as a widget field.
    field_values = [
        f["value"]
        for c in created
        for page in c["pages"]
        for w in page["widgets"]
        for f in w["fields"]
    ]
    assert "I-scud.metric.acm.scud_http_requests_5xx" not in field_values
    # The overview 5xx widget still has the other 6 services.
    assert "I-scud.metric.user_api.scud_http_requests_5xx" in field_values


def test_every_widget_has_required_svggraph_fields():
    """svggraph in Zabbix 7.0 requires a unique per-dashboard reference and a
    time_period; missing either makes dashboard.create reject the widget."""
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    for c in _created(captured):
        refs = []
        for page in c["pages"]:
            for w in page["widgets"]:
                names = {f["name"] for f in w["fields"]}
                assert "reference" in names, f"{c['name']}/{w['name']}: no reference"
                assert "time_period.from" in names and "time_period.to" in names
                refs.append(next(f["value"] for f in w["fields"] if f["name"] == "reference"))
        assert len(refs) == len(set(refs)), f"{c['name']}: duplicate widget references"


def test_infra_widgets_target_agent2_host():
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    # Every dbprobe item lookup must have used the scud-agent2 host id.
    for method, params in captured:
        if method == "item.get" and params["filter"]["key_"].startswith("scud.dbprobe["):
            assert params["hostids"] == "H-scud-agent2"


def test_gateway_widgets_target_agent2_host():
    """The detailed gateway dashboard's mod_status series live on the infra
    host (scud-agent2), like the dbprobe series."""
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    resolved = [
        params for method, params in captured
        if method == "item.get" and params["filter"]["key_"].startswith("scud.gateway.")
    ]
    assert resolved, "no gateway item lookups happened"
    for params in resolved:
        assert params["hostids"] == "H-scud-agent2"


def test_gateway_dashboard_has_saturation_page():
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    gw = next(c for c in _created(captured) if c["name"] == pd.DASH_GATEWAY)
    page_names = {p["name"] for p in gw["pages"]}
    assert "Workers & saturation" in page_names
    assert "Scoreboard" in page_names


def test_outbox_dashboard_has_outbox_page_on_agent2():
    """The Outbox dashboard surfaces the queue-health series (pending/age/bytes/
    dead-letter) from the dbprobe items on the infra host."""
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    ob = next(c for c in _created(captured) if c["name"] == pd.DASH_OUTBOX)
    assert {p["name"] for p in ob["pages"]} == {"Outbox"}
    # Outbox series resolve against the infra host, like the other dbprobe series.
    for method, params in captured:
        if (method == "item.get"
                and "outbox_" in params["filter"]["key_"]):
            assert params["hostids"] == "H-scud-agent2"


def test_sli_widgets_target_service_hosts():
    captured = []
    with mock.patch("provision.api_call", side_effect=_fake_api(captured)):
        pd.provision_dashboards("http://z/api_jsonrpc.php", "tok")
    for method, params in captured:
        if method == "item.get" and params["filter"]["key_"].startswith("scud.sli.auth_api."):
            assert params["hostids"] == "H-scud-auth_api"
        if method == "item.get" and params["filter"]["key_"].startswith("scud.sli.locker_api."):
            assert params["hostids"] == "H-scud-locker_api"
