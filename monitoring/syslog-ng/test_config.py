"""Structural tests for the syslog-ng aggregator config, log rotation and the
service allowlist (no Docker required — pure file inspection)."""
import os
import re

HERE = os.path.dirname(__file__)

SCUD_SERVICES = (
    "user_api", "locker_api", "history_api", "auth_api",
    "acm", "notification_api", "reports_api",
)


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def test_allowlist_filter_defined_with_all_services():
    conf = _read("syslog-ng.conf")
    assert "filter f_scud_services" in conf, "service allowlist filter missing"
    for svc in SCUD_SERVICES:
        assert re.search(rf"\b{svc}\b", conf), f"{svc} not in allowlist"


def test_inner_json_parser_exposes_service_field():
    """The Docker wrapper only gives .docker.log (the app JSON string); an inner
    json-parser must expose the app's own 'service' field for the allowlist."""
    conf = _read("syslog-ng.conf")
    assert "json-parser" in conf
    # The allowlist must match on the inner-parsed service field, not the raw line.
    assert re.search(r"match\(.*\.app\.service", conf) or re.search(
        r"value\(\"\.app\.service\"\)", conf
    ), "allowlist must match the inner-parsed .app.service field"


def test_allowlist_in_pipeline_after_json_guard():
    conf = _read("syslog-ng.conf")
    log_block = conf[conf.index("log {"):]
    assert "filter(f_json_only)" in log_block
    assert "filter(f_scud_services)" in log_block
    assert log_block.index("filter(f_json_only)") < log_block.index("filter(f_scud_services)")


def test_logrotate_policy():
    conf = _read("scud-logrotate.conf")
    assert "/var/log/scud/scud-all.log" in conf
    assert "rotate 14" in conf
    assert "compress" in conf
    assert "daily" in conf
    assert "postrotate" in conf
    assert "syslog-ng-ctl reopen" in conf


def test_entrypoint_rotates_and_execs_syslogng():
    sh = _read("entrypoint.sh")
    assert sh.startswith("#!/bin/sh")
    assert "logrotate" in sh
    assert re.search(r"exec\s+\S*syslog-ng", sh), "entrypoint must exec syslog-ng as PID 1"


def test_dockerfile_installs_logrotate_from_main():
    df = _read("Dockerfile")
    assert "logrotate" in df, "logrotate must be installed"
    assert "sed -i '/extended/d' /etc/apt/sources.list" in df, "extended repo must be removed"
    assert "pip install" not in df, "no pip in a certified production image"
    assert "/usr/local/bin/entrypoint.sh" in df, "ENTRYPOINT must use the rotation entrypoint"
    assert "cron" not in df, "cron is unnecessary — a small sh timer drives logrotate"
