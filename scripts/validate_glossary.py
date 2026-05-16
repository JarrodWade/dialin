#!/usr/bin/env python3
"""Validate lambda/coffee_glossary.json: syntax, entries, no duplicate normalized aliases."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "lambda" / "coffee_glossary.json"


def normalize_alias(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("&", " and ")
    for ch in "‐–—":
        s = s.replace(ch, "-")
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def main() -> int:
    try:
        raw = json.loads(GLOSSARY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"missing {GLOSSARY}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 1

    entries = raw.get("entries")
    if not isinstance(entries, list) or not entries:
        print("entries must be a non-empty list", file=sys.stderr)
        return 1

    seen: dict[str, str] = {}
    errors = 0
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            print(f"entry {i}: not an object", file=sys.stderr)
            errors += 1
            continue
        title = (ent.get("title") or "").strip()
        body = (ent.get("body") or "").strip()
        aliases = ent.get("aliases") or []
        if not title or not body:
            print(f"entry {i} ({title!r}): missing title or body", file=sys.stderr)
            errors += 1
        if not isinstance(aliases, list) or not aliases:
            print(f"entry {i} ({title!r}): aliases must be a non-empty list", file=sys.stderr)
            errors += 1
            continue
        for al in aliases:
            k = normalize_alias(str(al))
            if not k:
                print(f"entry {i} ({title!r}): empty alias", file=sys.stderr)
                errors += 1
                continue
            if k in seen and seen[k] != title:
                print(
                    f"duplicate normalized alias {k!r}: {seen[k]!r} vs {title!r}",
                    file=sys.stderr,
                )
                errors += 1
            else:
                seen[k] = title

    if errors:
        print(f"glossary validation failed ({errors} issue(s))", file=sys.stderr)
        return 1
    print(f"ok: {len(entries)} entries, {len(seen)} alias keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
