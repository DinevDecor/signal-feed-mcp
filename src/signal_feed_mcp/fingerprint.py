"""Canonical JSON fingerprint: the SHA-256 that pins each Regime Feed issue in the public ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(data: Any) -> bytes:
    """Serialize ``data`` to a canonical, deterministic JSON byte string.

    Keys are sorted recursively and separators are fixed, so semantically
    identical structures produce identical bytes regardless of input key
    order or whitespace.
    """
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


def fingerprint(data: Any) -> str:
    """Return the SHA-256 hex digest of the canonical JSON form of ``data``."""
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()
