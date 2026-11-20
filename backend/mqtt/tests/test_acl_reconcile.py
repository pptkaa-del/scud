"""Сверка ACL с дефолтами образа при каждом старте брокера.

Том `mqtt_config` сеется из образа только при первом запуске, поэтому правка
`aclfile` в образе иначе никогда не доезжает до существующего развёртывания:
брокер молча работает по устаревшим правам. Пароль backend-пользователя уже
сверяется на каждом старте (INF-06) — ACL должен вести себя так же, сохраняя
per-device блоки, которые дописывает провижининг.
"""
import pathlib
import subprocess

import pytest

SCRIPT = "/acl-reconcile.sh"

DEFAULTS = """\
user scud_backend
topic readwrite stat
topic readwrite stat/#

pattern write stat
"""

RELAY_BLOCK = """\
user relay-7
topic write stat
topic write stat/7
topic read com/7
"""


def _run(tmp_path: pathlib.Path, live: str | None) -> str:
    conf = tmp_path / "config"
    defaults = tmp_path / "defaults"
    conf.mkdir()
    defaults.mkdir()
    (defaults / "aclfile").write_text(DEFAULTS)
    if live is not None:
        (conf / "aclfile").write_text(live)
    res = subprocess.run(["sh", SCRIPT, str(conf), str(defaults)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return (conf / "aclfile").read_text()


def test_seeds_when_absent(tmp_path):
    assert _run(tmp_path, None) == DEFAULTS


def test_refreshes_stale_backend_block(tmp_path):
    stale = "user scud_backend\ntopic readwrite stat\n\npattern write stat\n"
    assert "topic readwrite stat/#" in _run(tmp_path, stale)


def test_preserves_per_device_blocks(tmp_path):
    out = _run(tmp_path, DEFAULTS + "\n" + RELAY_BLOCK)
    assert "user relay-7" in out
    assert "topic read com/7" in out
    assert "topic write stat/7" in out


def test_refresh_keeps_devices_when_defaults_changed(tmp_path):
    stale = "user scud_backend\ntopic readwrite stat\n\npattern write stat\n\n" + RELAY_BLOCK
    out = _run(tmp_path, stale)
    assert "topic readwrite stat/#" in out
    assert "user relay-7" in out


def test_is_idempotent(tmp_path):
    first = _run(tmp_path, DEFAULTS + "\n" + RELAY_BLOCK)
    conf = tmp_path / "config"
    subprocess.run(["sh", SCRIPT, str(conf), str(tmp_path / "defaults")], check=True)
    assert (conf / "aclfile").read_text() == first


def test_does_not_duplicate_device_blocks(tmp_path):
    out = _run(tmp_path, DEFAULTS + "\n" + RELAY_BLOCK)
    assert out.count("user relay-7") == 1
