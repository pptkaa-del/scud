#!/usr/bin/env python3
"""Idempotent rewriter: add a Custom-on-fail=0 handler to every dependent
``scud.metric.*`` JSONPATH step in ``template-scud-services.yaml``.

A labeled Prometheus counter emits no series until its first observation, so on
a quiet stand the JSONPATH selection is empty and ``.value.sum()`` fails the
dependent item (UNSUPPORTED) instead of reading 0. Appending

    error_handler: CUSTOM_VALUE
    error_handler_params: "0"

to each such preprocessing step makes the item read 0 until real data arrives,
which also revives the SLI calculations that depend on those metrics.

Only the 90 scalar ``.value.sum()`` aggregation steps are touched. LLD discovery
filters (``@.type==...``) and item-prototype ``.value.first()`` steps are left
alone — an empty selection there is semantically valid.

Run once: ``python tools/add_onfail.py`` (prints the number of steps modified).
Re-running is a no-op (prints ``0 steps modified``).
"""
import os
import re

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "template-scud-services.yaml")

# A JSONPATH parameter line whose expression aggregates an array via .value.sum().
_ARG_RE = re.compile(r'^(\s*)- ".*\.value\.sum\(\)"\s*$')
_HANDLER = "error_handler: CUSTOM_VALUE"
_HANDLER_PARAMS = 'error_handler_params: "0"'


def add_onfail(text: str) -> tuple[str, int]:
    """Return (rewritten_text, steps_modified). Idempotent."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    modified = 0
    for i, line in enumerate(lines):
        out.append(line)
        match = _ARG_RE.match(line)
        if not match:
            continue
        # The parameter dash is indented two columns past its sibling keys
        # (``type:``/``parameters:``); the handler keys align with those.
        key_indent = " " * (len(match.group(1)) - 2)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() == _HANDLER:
            continue  # already handled — keep idempotent
        eol = "\n"
        out.append(f"{key_indent}{_HANDLER}{eol}")
        out.append(f"{key_indent}{_HANDLER_PARAMS}{eol}")
        modified += 1
    return "".join(out), modified


def main() -> int:
    with open(TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    new_text, count = add_onfail(text)
    if count:
        with open(TEMPLATE, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
    print(f"{count} steps modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
