"""A tiny synthetic tamper-snapshot JPEG shipped with the relay emulator.

Real 160x120 baseline JPEG (~1597 B) so that `relay --photos` frames land in the
backend media pipeline as a renderable `image/jpeg`, not opaque bytes. Content is a
dark "SCUD TAMPER / synthetic" placeholder — this is a dev/load double, never a real
camera capture. Kept small on purpose: the outbound media dispatcher chokes on large
photo backlogs, so the demo photo is a couple of KiB, not MBs.

Generated once from a fixed Pillow render; edit by regenerating, not by hand.
"""
from __future__ import annotations

import base64

_TAMPER_JPEG_B64 = (
    "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"
)

TAMPER_JPEG: bytes = base64.b64decode(_TAMPER_JPEG_B64)
"""Decoded JPEG body (bytes) sent as a single-frame relay photo series."""
