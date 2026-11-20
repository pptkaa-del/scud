"""Проверки TLS-терминатора MQTT: конфиг, хендшейк, набор сюит.

Гоняется в стадии `test` образа backend/mqtt-tls/Dockerfile, где есть настоящий
haproxy 2.6.23 из сертифицированного main. Брокер не нужен: все проверки
заканчиваются на TLS-хендшейке, а `tcp-request content reject` не пускает
сессию без данных дальше фронтенда.
"""

import re
import socket
import ssl
import subprocess
import time

import pytest

CONFIG = "/etc/haproxy/haproxy.cfg"
CERT = "/etc/haproxy/certs/server.pem"
HOST = "127.0.0.1"
PORT = 8883
SERVER_NAME = "mqtt-tls.test"
MQTT_CONNACK = b"\x20"
# keepAlive платы 95 c * 1.5: ниже этого haproxy рвёт простаивающую сессию сам.
MIN_TIMEOUT_S = 143
# Проводной канал платы умеет РОВНО эту сюиту (требования вендора, п.2).
BOARD_WIRED_CIPHER = "AES128-SHA256"
# Приёмный буфер платы 1024 Б, Certificate она не пересобирает из записей (п.6).
DER_LIMIT = 1000

COMPAT_CIPHERS = [
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES128-SHA256",
    "ECDHE-RSA-AES256-SHA384",
    "ECDHE-RSA-AES128-SHA",
    "ECDHE-RSA-AES256-SHA",
    "DHE-RSA-AES128-GCM-SHA256",
    "DHE-RSA-AES256-GCM-SHA384",
    "DHE-RSA-AES128-SHA",
    "DHE-RSA-AES256-SHA",
    "AES128-GCM-SHA256",
    "AES256-GCM-SHA384",
    "AES128-SHA256",
    "AES256-SHA256",
    "AES128-SHA",
    "AES256-SHA",
]

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def _config_text():
    with open(CONFIG, encoding="utf-8") as fh:
        return fh.read()


def _configured_ciphers():
    line = re.search(r"^\s*ssl-default-bind-ciphers\s+(\S+)", _config_text(), re.M)
    assert line, "ssl-default-bind-ciphers не задан"
    return [c for c in line.group(1).split(":") if not c.startswith("@")]


def _timeout_seconds(kind):
    match = re.search(rf"^\s*timeout {kind}\s+(\d+)(ms|s|m|h)?\s*$", _config_text(), re.M)
    assert match, f"timeout {kind} не задан явно"
    unit = match.group(2)
    assert unit, f"timeout {kind} без единицы измерения — haproxy трактует это как мс"
    return int(match.group(1)) * _UNITS[unit]


def _issue_cert():
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-days", "1", "-nodes",
            "-keyout", "/tmp/t.key", "-out", "/tmp/t.crt",
            "-subj", f"/CN={SERVER_NAME}",
            "-addext", f"subjectAltName=DNS:{SERVER_NAME}",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        ],
        check=True,
        capture_output=True,
    )
    with open(CERT, "wb") as out:
        for part in ("/tmp/t.crt", "/tmp/t.key"):
            with open(part, "rb") as src:
                out.write(src.read())


@pytest.fixture(scope="module")
def haproxy():
    _issue_cert()
    check = subprocess.run(
        ["haproxy", "-c", "-dW", "-f", CONFIG], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stderr
    assert "[WARNING]" not in (check.stdout + check.stderr), check.stdout + check.stderr
    proc = subprocess.Popen(["haproxy", "-db", "-f", CONFIG])
    for _ in range(50):
        try:
            socket.create_connection((HOST, PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("haproxy не забиндил 8883")
    yield proc
    proc.terminate()
    proc.wait(timeout=10)


def _ctx(cipher=None, minimum=None, maximum=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if minimum is not None:
        ctx.minimum_version = minimum
    if maximum is not None:
        ctx.maximum_version = maximum
    if cipher is not None:
        ctx.set_ciphers(f"{cipher}:@SECLEVEL=0")
    return ctx


def test_crt_path_matches_compose_mount():
    assert "crt /etc/haproxy/certs/server.pem" in _config_text()


def test_proxy_protocol_is_not_used():
    """mosquitto 2.0.23 не понимает PROXY protocol — сессия умрёт на первом байте."""
    assert "send-proxy" not in _config_text()


def test_tls_floor_is_declared():
    assert re.search(r"ssl-min-ver\s+TLSv1\.2", _config_text())


@pytest.mark.parametrize("kind", ["client", "server"])
def test_timeout_outlives_board_keepalive(kind):
    assert _timeout_seconds(kind) >= MIN_TIMEOUT_S


def test_cipher_list_matches_tested_set():
    assert _configured_ciphers() == COMPAT_CIPHERS


def test_process_stays_up(haproxy):
    assert haproxy.poll() is None


def test_tls12_handshake_succeeds(haproxy):
    ctx = _ctx(minimum=ssl.TLSVersion.TLSv1_2, maximum=ssl.TLSVersion.TLSv1_2)
    with ctx.wrap_socket(
        socket.create_connection((HOST, PORT), 5), server_hostname=SERVER_NAME
    ) as sock:
        assert sock.version() == "TLSv1.2"


def test_server_key_is_rsa2048(haproxy):
    out = subprocess.run(
        ["openssl", "x509", "-in", CERT, "-noout", "-text"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "rsaEncryption" in out
    assert "2048 bit" in out


@pytest.mark.parametrize("cipher", COMPAT_CIPHERS)
def test_embedded_client_cipher_is_offered(haproxy, cipher):
    """Каждая сюита, которой может быть ограничен встраиваемый стек платы.

    Не-ECDHE `AES*` — статический RSA: плата вообще без ECDHE всё равно
    договорится.
    """
    ctx = _ctx(
        cipher=cipher,
        minimum=ssl.TLSVersion.TLSv1_2,
        maximum=ssl.TLSVersion.TLSv1_2,
    )
    with ctx.wrap_socket(
        socket.create_connection((HOST, PORT), 5), server_hostname=SERVER_NAME
    ) as sock:
        assert sock.cipher()[0] == cipher


@pytest.mark.parametrize("version", [ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1])
def test_tls_below_12_is_refused(haproxy, version):
    ctx = _ctx(cipher="ALL", minimum=version, maximum=version)
    with pytest.raises(ssl.SSLError):
        ctx.wrap_socket(
            socket.create_connection((HOST, PORT), 5),
            server_hostname=SERVER_NAME,
        )


def test_plaintext_mqtt_is_refused(haproxy):
    sock = socket.create_connection((HOST, PORT), 5)
    sock.sendall(bytes.fromhex("101800044d5154540402005f"))
    sock.settimeout(5)
    assert sock.recv(16)[:1] != MQTT_CONNACK
    sock.close()


def test_board_wired_cipher_is_present():
    """п.2: без AES128-SHA256 проводная плата не подключится вовсе."""
    assert BOARD_WIRED_CIPHER in _configured_ciphers()


def test_cipher_list_is_not_reduced_to_the_board_cipher():
    """п.2: список не должен быть сведён к единственной сюите."""
    assert len(_configured_ciphers()) > 1


def test_tls13_is_disabled():
    """Прошивка платы не поддерживает TLS 1.3 (требования вендора, §8)."""
    assert re.search(r"ssl-max-ver\s+TLSv1\.2", _config_text())


def test_tls13_handshake_is_refused(haproxy):
    ctx = _ctx(minimum=ssl.TLSVersion.TLSv1_3, maximum=ssl.TLSVersion.TLSv1_3)
    with pytest.raises(ssl.SSLError):
        ctx.wrap_socket(socket.create_connection((HOST, PORT), 5),
                        server_hostname=SERVER_NAME)


def test_board_cipher_actually_negotiates(haproxy):
    """Клиент, умеющий ровно AES128-SHA256, должен договориться."""
    ctx = _ctx(cipher=BOARD_WIRED_CIPHER,
               minimum=ssl.TLSVersion.TLSv1_2, maximum=ssl.TLSVersion.TLSv1_2)
    with ctx.wrap_socket(socket.create_connection((HOST, PORT), 5),
                         server_hostname=SERVER_NAME) as sock:
        assert sock.cipher()[0] == BOARD_WIRED_CIPHER


def test_served_chain_is_exactly_one_certificate(haproxy):
    """п.4: два сертификата не влезают в буфер платы — рукопожатие рвётся."""
    out = subprocess.run(
        ["openssl", "s_client", "-connect", f"{HOST}:{PORT}",
         "-servername", SERVER_NAME, "-showcerts", "-tls1_2"],
        input="", capture_output=True, text=True, timeout=20,
    ).stdout
    assert out.count("BEGIN CERTIFICATE") == 1, out.count("BEGIN CERTIFICATE")


def test_served_certificate_fits_board_buffer(haproxy):
    """п.6: серверный сертификат в DER не больше 1000 Б."""
    der = ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate((HOST, PORT)))
    assert len(der) <= DER_LIMIT, len(der)
