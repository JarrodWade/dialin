"""Resolve free-typed gear names to cleaned display strings and known aliases."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ALIASES: dict[str, str] | None = None


def _aliases_path() -> Path:
    return Path(__file__).resolve().parent / "gear_canonical.json"


def _normalize_lookup_key(name: str) -> str:
    s = (name or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def _load_aliases() -> dict[str, str]:
    global _ALIASES
    if _ALIASES is not None:
        return _ALIASES
    path = _aliases_path()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    blob = raw.get("aliases") or {}
    if not isinstance(blob, dict):
        blob = {}
    out: dict[str, str] = {}
    for k, v in blob.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = _normalize_lookup_key(k)
        val = v.strip()
        if key and val:
            out[key] = val
    _ALIASES = out
    return _ALIASES


def resolve_equipment_display_name(raw: str) -> tuple[str, dict[str, Any] | None]:
    """Return (stored_display_name, adjustment_meta_or_None).

    * Trims and collapses internal whitespace.
    * If ``gear_canonical.json`` maps the normalized key, uses that display string.
    * ``meta`` is set when the user-visible result differs from what they submitted
      (trim-only or alias), so the API/UI can acknowledge the change.
    """
    submitted = (raw or "").strip()
    if not submitted:
        return "", None

    tidied_space = re.sub(r"\s+", " ", submitted)
    key = _normalize_lookup_key(tidied_space)
    aliases = _load_aliases()

    if key in aliases:
        resolved = aliases[key]
        if resolved != submitted:
            return resolved, {"kind": "alias", "submitted": submitted, "resolved": resolved}
        return resolved, None

    if tidied_space != submitted:
        return tidied_space, {"kind": "whitespace", "submitted": submitted, "resolved": tidied_space}

    return tidied_space, None
