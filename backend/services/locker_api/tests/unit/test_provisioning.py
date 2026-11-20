import hashlib
import os
import tempfile

from app.mqtt import provisioning
from app.mqtt.provisioning import (
    acl_block,
    broker_password,
    broker_username,
    provision_device,
)


def test_broker_username():
    assert broker_username(12345) == "relay-12345"


def test_broker_password_derivation():
    # password = hex(first 16 bytes of sha256("relay-{id}"))
    expected = hashlib.sha256(b"relay-1").digest()[:16].hex()
    assert broker_password(1) == expected
    assert len(broker_password(1)) == 32


def test_acl_block_grants_shared_write_and_own_read():
    block = acl_block(7)
    assert "user relay-7\n" in block
    assert "topic write stat\n" in block
    assert "topic write rep\n" in block
    assert "topic write photos\n" in block
    assert "topic read com/7\n" in block


def test_provision_device_writes_plaintext_and_acl(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(provisioning, "MQTT_CONFIG_DIR", tmp)
        pw = provision_device(42)
        assert pw == broker_password(42)

        pw_path = os.path.join(tmp, "passwordfile")
        acl_path = os.path.join(tmp, "aclfile")
        with open(pw_path, encoding="ascii") as fh:
            pw_content = fh.read()
        with open(acl_path, encoding="ascii") as fh:
            acl_content = fh.read()

        # PLAINTEXT entry "relay-42:{pw}" (no hashing).
        assert f"relay-42:{pw}\n" in pw_content
        assert "user relay-42\n" in acl_content
        assert "topic read com/42\n" in acl_content


def test_provision_device_is_idempotent(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(provisioning, "MQTT_CONFIG_DIR", tmp)
        provision_device(100)
        provision_device(100)  # second call must not duplicate the entry

        with open(os.path.join(tmp, "passwordfile"), encoding="ascii") as fh:
            pw_content = fh.read()
        assert pw_content.count("relay-100:") == 1
        # ACL тоже не дублируется.
        with open(os.path.join(tmp, "aclfile"), encoding="ascii") as fh:
            acl_content = fh.read()
        assert acl_content.count("user relay-100\n") == 1


def test_provision_writes_acl_before_password(monkeypatch):
    """Fix 4: ACL-блок устройства должен оказаться записан ДО строки пароля.

    Проверяем по mtime/порядку записей: aclfile получает запись об устройстве, а
    кред в passwordfile появляется только после. Эмулируем reload-в-середине,
    подменяя open так, что после записи ACL, но до пароля, читаем оба файла."""
    written_order = []

    import builtins
    real_builtins_open = builtins.open

    def tracking_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if "a" in mode:
            base = os.path.basename(path)
            written_order.append(base)
        return real_builtins_open(path, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(provisioning, "MQTT_CONFIG_DIR", tmp)
        monkeypatch.setattr(builtins, "open", tracking_open)
        provision_device(55)

    # aclfile должен быть открыт на append РАНЬШE passwordfile.
    assert "aclfile" in written_order
    assert "passwordfile" in written_order
    assert written_order.index("aclfile") < written_order.index("passwordfile")


def test_provision_takes_flock(monkeypatch):
    """Fix 4: provisioning берёт fcntl.flock(LOCK_EX) на lock-файл."""
    import fcntl

    flock_calls = []
    real_flock = fcntl.flock

    def tracking_flock(fd, op):
        flock_calls.append(op)
        return real_flock(fd, op)

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(provisioning, "MQTT_CONFIG_DIR", tmp)
        monkeypatch.setattr(fcntl, "flock", tracking_flock)
        provision_device(77)
        # lock-файл создан в config dir
        assert os.path.exists(os.path.join(tmp, ".provision.lock"))

    assert fcntl.LOCK_EX in flock_calls
    assert fcntl.LOCK_UN in flock_calls


def test_provision_concurrent_threads_no_corruption(monkeypatch):
    """Fix 4: параллельные provision_device (разные relay_id) не перемешивают
    строки и не дублируют — flock сериализует операции."""
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(provisioning, "MQTT_CONFIG_DIR", tmp)

        relay_ids = list(range(200, 220))
        threads = [threading.Thread(target=provision_device, args=(rid,))
                   for rid in relay_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(os.path.join(tmp, "passwordfile"), encoding="ascii") as fh:
            pw_lines = [ln for ln in fh.read().splitlines() if ln]
        # По одной непустой строке на устройство, без перемешанных/обрезанных.
        assert len(pw_lines) == len(relay_ids)
        for rid in relay_ids:
            assert any(ln.startswith(f"relay-{rid}:") for ln in pw_lines)
        # Каждая строка — ровно "user:password" (двоеточие не потеряно).
        for ln in pw_lines:
            assert ln.count(":") == 1
