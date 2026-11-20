import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_zabbix_readme_has_import_and_manual_fallback():
    txt = _read("monitoring", "zabbix", "README.md")
    assert "provision.py" in txt
    assert "ZABBIX_PASSWORD" in txt
    assert "Data collection" in txt or "ручной" in txt.lower() or "manual" in txt.lower()


def test_deployment_documents_monitoring_profile():
    txt = _read("backend", "DEPLOYMENT.md")
    assert "profile monitoring" in txt or "--profile monitoring" in txt
    assert "postgres_zabbix_password" in txt
    assert "LOG_FORMAT" in txt


def test_changelog_has_monitoring_entry():
    txt = _read("CHANGELOG.md")
    lower = txt.lower()
    assert "monitoring" in lower
    assert "zabbix" in lower


def test_zabbix_readme_documents_infra_template_and_catalogue():
    txt = _read("monitoring", "zabbix", "README.md")
    assert "template-scud-infra.yaml" in txt
    # provisioning both templates
    assert "ZABBIX_TEMPLATE=monitoring/zabbix/template-scud-infra.yaml" in txt
    # infra metric catalogue mentions the three components + key signals
    assert "scud.infra.redis" in txt
    assert "scud.dbprobe" in txt
    assert "cl_waiting" in txt


def test_deployment_documents_agent2_and_pgb_admin_rotation():
    txt = _read("backend", "DEPLOYMENT.md")
    assert "zabbix_agent2" in txt
    assert "pgb_admin_password" in txt
    # rotation runbook keyword
    lower = txt.lower()
    assert "ротац" in lower or "rotat" in lower


def test_changelog_has_etap2_infra_entry():
    txt = _read("CHANGELOG.md").lower()
    assert "agent2" in txt or "infra" in txt
    assert "pgbouncer" in txt
    assert "$sys" in txt or "mqtt" in txt


def test_cert_gate_documents_etap2_gates():
    txt = _read("monitoring", "CERT_GATE.md")
    assert "zabbix-agent2" in txt
    lower = txt.lower()
    assert "redis" in lower
    assert "pgbouncer" in lower or "dbprobe" in lower


def test_pgbouncer_readme_documents_admin_secret():
    txt = _read("backend", "pgbouncer", "README.md")
    assert "pgb_admin_password" in txt
