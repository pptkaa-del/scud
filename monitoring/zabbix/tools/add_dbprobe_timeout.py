#!/usr/bin/env python3
"""Idempotent rewriter: add ``timeout: 30s`` to every ``scud.dbprobe[*]`` item in
``template-scud-infra.yaml``.

Each dbprobe item opens a fresh (cold) psql connection, which takes ~10s — well
over the server's default 3-4s poll timeout for passive Zabbix-agent checks, so
the items were reported UNSUPPORTED ("Timeout occurred while gathering data").
Zabbix 7.0 supports a per-item ``timeout`` (max 30s, matching the agent2 Timeout
cap); set it on every dbprobe item.

Run once: ``python tools/add_dbprobe_timeout.py`` (prints the count). Re-running
is a no-op.
"""
import os

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "template-scud-infra.yaml")
_KEY_PREFIX = 'key: "scud.dbprobe['
_TIMEOUT = "timeout: 30s"


def add_timeout(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    modified = 0
    for i, line in enumerate(lines):
        out.append(line)
        stripped = line.strip()
        if not stripped.startswith(_KEY_PREFIX):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() == _TIMEOUT:
            continue  # already present — keep idempotent
        out.append(f"{indent}{_TIMEOUT}\n")
        modified += 1
    return "".join(out), modified


def main() -> int:
    with open(TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    new_text, count = add_timeout(text)
    if count:
        with open(TEMPLATE, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
    print(f"{count} items modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
