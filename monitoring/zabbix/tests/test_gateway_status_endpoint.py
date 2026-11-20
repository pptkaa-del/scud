"""Security regression guard for the gateway mod_status endpoint.

The detailed gateway dashboard needs Apache's server-status, but that page leaks
worker-state, in-flight request URLs and client IPs. It MUST stay internal-only:
served on the unpublished port 81 (reachable only container-to-container on
backend_net) and NEVER on the public :80 vhost. These checks fail loudly if a
future edit re-exposes it.
"""
import os

import yaml

_HERE = os.path.dirname(__file__)
_GATEWAY = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "backend", "gateway"))
_COMPOSE = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "backend", "docker-compose.yaml"))


def _container_port(entry):
    """Container-side port of a compose ports entry (short 'h:c[/proto]' or
    long-form mapping). Returns the int port or None."""
    if isinstance(entry, dict):
        return int(entry["target"]) if "target" in entry else None
    s = str(entry).split("/", 1)[0]  # strip /tcp|/udp
    parts = s.split(":")
    return int(parts[-1])  # container side is the last field


def _read(name):
    with open(os.path.join(_GATEWAY, name), encoding="utf-8") as fh:
        return fh.read()


def test_status_conf_serves_on_internal_port_81():
    conf = _read("status.conf")
    assert "Listen 81" in conf
    assert "<VirtualHost *:81>" in conf
    assert "SetHandler server-status" in conf
    assert "ExtendedStatus On" in conf


def test_status_conf_denies_everything_but_status():
    """The :81 vhost must default-deny so only /server-status is reachable."""
    conf = _read("status.conf")
    assert "Require all denied" in conf
    # The status handler is the only place granting access.
    assert conf.count("Require all granted") == 1


def test_public_vhost_does_not_expose_server_status():
    """The public :80 vhost (apache.conf) must not serve mod_status at all."""
    public = _read("apache.conf")
    assert "server-status" not in public
    assert "SetHandler server-status" not in public
    # And it must not open a second listener; :80 only.
    assert "Listen 81" not in public


def test_mpm_conf_pins_slots_to_real_ceiling():
    """worker_util_pct's denominator is the scoreboard length = ServerLimit.
    If ServerLimit != MaxRequestWorkers, util can never reach 100% and the
    saturation triggers are dead. Pin them equal."""
    import re
    conf = _read("mpm.conf")
    sl = re.search(r"^\s*ServerLimit\s+(\d+)", conf, re.M)
    mrw = re.search(r"^\s*MaxRequestWorkers\s+(\d+)", conf, re.M)
    assert sl and mrw, "mpm.conf must set both ServerLimit and MaxRequestWorkers"
    assert sl.group(1) == mrw.group(1), (
        f"ServerLimit ({sl.group(1)}) != MaxRequestWorkers ({mrw.group(1)}) — "
        "scoreboard length would diverge from the real ceiling"
    )
    df = _read("Dockerfile")
    assert "gateway/mpm.conf /etc/apache2/conf-enabled/scud-mpm.conf" in df


def test_dockerfile_enables_status_but_drops_global_conf():
    df = _read("Dockerfile")
    assert "a2enmod status" in df
    # The distro's global status.conf (which would expose /server-status on
    # every vhost, incl. :80) is removed.
    assert "rm -f /etc/apache2/mods-enabled/status.conf" in df
    # Our internal-only conf is installed into conf-enabled.
    assert "gateway/status.conf /etc/apache2/conf-enabled/scud-status.conf" in df


def test_compose_does_not_publish_port_81():
    """Port 81 stays internal: the apache_gateway service must publish ONLY the
    public :80 — any host mapping to container :81 would expose server-status.
    Parse the real ports list (not a literal-string match, which '8081:81' or a
    bare '- 81' would slip past)."""
    with open(_COMPOSE, encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    ports = compose["services"]["apache_gateway"].get("ports", [])
    container_ports = [_container_port(p) for p in ports]
    assert 81 not in container_ports, f"gateway publishes container port 81: {ports}"
    # The public entry must be exactly 80 -> 80.
    assert any(str(p).replace('"', "") in ("80:80", "80") for p in ports), ports
    assert 80 in container_ports
