import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import provision  # noqa: E402


def _fake_resp(payload):
    m = mock.Mock()
    m.read.return_value = json.dumps(payload).encode()
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: False
    return m


def test_api_call_wraps_jsonrpc_and_returns_result():
    with mock.patch(
        "provision.urllib.request.urlopen",
        return_value=_fake_resp({"jsonrpc": "2.0", "result": "OK", "id": 1}),
    ):
        out = provision.api_call("apiinfo.version", {}, "http://z/api_jsonrpc.php")
    assert out == "OK"


def test_api_call_raises_on_jsonrpc_error():
    with mock.patch(
        "provision.urllib.request.urlopen",
        return_value=_fake_resp(
            {"jsonrpc": "2.0", "error": {"code": -32602, "message": "bad"}, "id": 1}
        ),
    ):
        try:
            provision.api_call("template.import", {}, "http://z/api_jsonrpc.php")
            raise AssertionError("expected ZabbixApiError")
        except provision.ZabbixApiError as exc:
            assert "bad" in str(exc)


def test_import_template_uses_idempotent_rules():
    captured = {}

    def _spy(method, params, url, auth=None):
        captured["method"], captured["params"] = method, params
        return True

    with mock.patch("provision.api_call", side_effect=_spy):
        provision.import_template("http://z/api_jsonrpc.php", "tok", "zabbix_export: {}")
    assert captured["method"] == "configuration.import"
    rules = captured["params"]["rules"]
    assert rules["templates"] == {"createMissing": True, "updateExisting": True}
    assert rules["triggers"]["updateExisting"] is True


def test_provision_hosts_creates_service_and_infra_hosts():
    created_hosts = []

    def fake(method, params, url, auth=None):
        if method == "hostgroup.get":
            return []
        if method == "hostgroup.create":
            return {"groupids": ["10"]}
        if method == "template.get":
            # filter={"host": <template name>} → return a stable id per name
            return [{"templateid": "T-" + params["filter"]["host"]}]
        if method == "host.get":
            return []
        if method == "host.create":
            created_hosts.append(params)
            return {"hostids": ["1"]}
        return []

    with mock.patch("provision.api_call", side_effect=fake):
        provision.provision_hosts("http://z/api_jsonrpc.php", "tok")

    by_name = {h["host"]: h for h in created_hosts}
    # 7 service hosts + 1 infra host
    assert set(by_name) == {
        "scud-user_api", "scud-locker_api", "scud-history_api", "scud-auth_api",
        "scud-acm", "scud-notification_api", "scud-reports_api", "scud-agent2",
    }
    # each service host links exactly its own scud-service-<svc> template, no interface
    locker = by_name["scud-locker_api"]
    assert locker["templates"] == [{"templateid": "T-scud-service-locker_api"}]
    assert "interfaces" not in locker
    # the infra host carries an agent interface + all five infra templates
    infra = by_name["scud-agent2"]
    assert infra["interfaces"][0]["dns"] == "zabbix_agent2"
    assert {t["templateid"] for t in infra["templates"]} == {
        "T-" + n for n in provision._INFRA_TEMPLATES
    }


def test_provision_hosts_idempotent_when_hosts_exist():
    massadd, creates = [], []

    def fake(method, params, url, auth=None):
        if method == "hostgroup.get":
            return [{"groupid": "10"}]
        if method == "template.get":
            return [{"templateid": "T1"}]
        if method == "host.get":
            return [{"hostid": "99"}]
        if method == "host.massadd":
            massadd.append(params)
            return {}
        if method == "host.create":
            creates.append(params)
            return {"hostids": ["1"]}
        return []

    with mock.patch("provision.api_call", side_effect=fake):
        provision.provision_hosts("http://z/api_jsonrpc.php", "tok")

    assert creates == []  # nothing re-created
    assert len(massadd) == 8  # templates ensured on all 8 existing hosts


def test_main_invokes_provision_dashboards_after_hosts():
    """main() must provision dashboards as the last step, after the hosts (whose
    items the dashboard widgets resolve) exist."""
    order = []
    env = {"ZABBIX_URL": "http://z", "ZABBIX_PASSWORD": "pw"}
    with mock.patch("provision.login", return_value="tok"), \
            mock.patch("provision.import_template"), \
            mock.patch("provision.import_alert_delivery"), \
            mock.patch("provision.provision_alert_routing"), \
            mock.patch("provision.provision_max_token"), \
            mock.patch("provision.provision_hosts", side_effect=lambda *a, **k: order.append("hosts")), \
            mock.patch("provision_dashboards.provision_dashboards",
                       side_effect=lambda *a, **k: order.append("dashboards")), \
            mock.patch.dict(os.environ, env, clear=False):
        rc = provision.main()
    assert rc == 0
    assert order == ["hosts", "dashboards"]


def test_provision_hosts_skips_host_when_templates_absent():
    creates = []

    def fake(method, params, url, auth=None):
        if method == "hostgroup.get":
            return [{"groupid": "10"}]
        if method == "template.get":
            return []  # no templates imported yet
        if method == "host.get":
            return []
        if method == "host.create":
            creates.append(params)
            return {"hostids": ["1"]}
        return []

    with mock.patch("provision.api_call", side_effect=fake):
        provision.provision_hosts("http://z/api_jsonrpc.php", "tok")

    assert creates == []  # no host created without a template to link
