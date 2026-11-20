"""Broker credential derivation + provisioning for relay devices.

username = "relay-{id}"; password = hex(sha256("relay-{id}")[:16]).
ACL: a device may write the shared stat/rep/photos topics and read only its own
com/{id}. The derivation lives here so backend and emulator agree byte-for-byte
(VERIFIED against RelayEmulator dev_v2 emulator.cpp:548-550 —
``sha256First16().toHex()`` = first 16 BYTES, 32 hex chars).

Provisioning appends a PLAINTEXT password entry + ACL block to the shared broker
config volume (`mqtt_config` → MQTT_CONFIG_DIR). Plaintext is correct, not a
placeholder: a `WITH_TLS=no` Mosquitto 2.0.23 build has no ``mosquitto_passwd``
and compares passwords with plain ``strcmp`` (verified in src/security_default.c).
The broker's config watcher SIGHUPs within ~2s to reload — no hashing involved.

Security note (Phase 1): the per-device password is derivable from the public
``relay_id`` and is NOT a secret. Acceptable for the plaintext-network Phase 1;
TLS + per-device random secrets are required for production.
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
import fcntl
import hashlib
import os

# Каталог конфигурации брокера (общий named-volume `mqtt_config`).
MQTT_CONFIG_DIR = os.getenv("MQTT_CONFIG_DIR", "/mosquitto/config")

_PASSWORD_FILE = "passwordfile"
_ACL_FILE = "aclfile"
_LOCK_FILE = ".provision.lock"

# password = hex(первые 16 БАЙТ sha256(username)) = 32 hex-символа.
_PASSWORD_BYTES = 16


def broker_username(relay_id: int) -> str:
    return f"relay-{relay_id}"


def broker_password(relay_id: int) -> str:
    digest = hashlib.sha256(broker_username(relay_id).encode("ascii")).digest()
    return digest[:_PASSWORD_BYTES].hex()


def acl_block(relay_id: int) -> str:
    user = broker_username(relay_id)
    return (
        f"user {user}\n"
        # Общие топики — для прошивок без суффикса, per-id — для ПИВ v2.
        # Права на общие оставлены ради совместимости; именно они позволяют
        # плате писать от чужого имени и снимаются, когда схема парка известна.
        f"topic write stat\n"
        f"topic write rep\n"
        f"topic write photos\n"
        f"topic write stat/{relay_id}\n"
        f"topic write rep/{relay_id}\n"
        f"topic write photos/{relay_id}\n"
        f"topic read com/{relay_id}\n"
    )


@contextlib.contextmanager
def _provision_lock() -> Iterator[None]:
    """Эксклюзивный fcntl.flock на весь provisioning одного устройства.

    Параллельные POST /lockers пишут в один passwordfile/aclfile (shared
    volume). Без блокировки два append'а могут перемешать строки или
    продублировать запись (idempotency-проверка читает stale-снимок). Лок
    держится на отдельном lock-файле в MQTT_CONFIG_DIR на всё время операции.
    """
    os.makedirs(MQTT_CONFIG_DIR, exist_ok=True)
    lock_path = os.path.join(MQTT_CONFIG_DIR, _LOCK_FILE)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def provision_device(relay_id: int) -> str:
    """Append broker user + ACL for a relay device. Idempotent. Returns the password.

    Skips if the user already exists in the password file. The broker watcher
    SIGHUPs to reload the plaintext password/ACL files (no hashing).

    Конкурентно-безопасно: вся операция под fcntl.flock (см. _provision_lock).
    ACL-блок пишется ПЕРЕД строкой пароля — если брокер перечитает конфиг в
    середине операции, у устройства не окажется креда без read-ACL (иначе оно
    могло бы получить право писать в shared-топики, не имея ограничения чтения
    своего com/{id}).
    """
    pw = broker_password(relay_id)
    user = broker_username(relay_id)
    pw_path = os.path.join(MQTT_CONFIG_DIR, _PASSWORD_FILE)
    acl_path = os.path.join(MQTT_CONFIG_DIR, _ACL_FILE)

    with _provision_lock():
        existing = ""
        if os.path.exists(pw_path):
            with open(pw_path, encoding="ascii") as fh:
                existing = fh.read()

        if f"{user}:" not in existing and f"{user}\n" not in existing:
            # ACL ПЕРЕД паролем: кред не должен существовать без своего read-ACL.
            with open(acl_path, "a", encoding="ascii") as fh:
                fh.write("\n" + acl_block(relay_id))
            with open(pw_path, "a", encoding="ascii") as fh:
                fh.write(f"{user}:{pw}\n")  # plaintext; broker watcher reloads on SIGHUP

    return pw
