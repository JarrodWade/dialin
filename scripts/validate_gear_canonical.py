#!/usr/bin/env python3
"""Validate lambda/gear_canonical.json structure and non-empty alias values."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "lambda" / "gear_canonical.json"


def normalize_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def main() -> int:
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"missing {CONFIG}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 1

    aliases = raw.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        print("aliases must be a non-empty object", file=sys.stderr)
        return 1

    errors = 0
    seen_norm: dict[str, str] = {}
    for k, v in aliases.items():
        if not isinstance(k, str) or not isinstance(v, str):
            print(f"alias key/value must be strings: {k!r}", file=sys.stderr)
            errors += 1
            continue
        disp = v.strip()
        if not disp:
            print(f"empty display for key {k!r}", file=sys.stderr)
            errors += 1
            continue
        nk = normalize_key(k)
        if not nk:
            print(f"empty normalized key for {k!r}", file=sys.stderr)
            errors += 1
            continue
        if nk in seen_norm and seen_norm[nk] != disp:
            print(
                f"conflict: normalized key {nk!r} maps to {seen_norm[nk]!r} and {disp!r}",
                file=sys.stderr,
            )
            errors += 1
        else:
            seen_norm[nk] = disp

    if errors:
        print(f"gear_canonical validation failed ({errors} issue(s))", file=sys.stderr)
        return 1
    print(f"ok: {len(aliases)} alias entries, {len(seen_norm)} normalized keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
