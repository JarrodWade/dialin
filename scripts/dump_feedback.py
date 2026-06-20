#!/usr/bin/env python3
"""Dump "that wasn't quite right" chat feedback so you can spot patterns.

Reads ChatFeedback rows from DynamoDB and prints them newest-first, with the
user message, the bot reply, and any comment. Use --user to scope to one user
(an efficient query); omit it to scan the whole table across all users.

Requires AWS credentials locally; set TABLE_NAME or pass --table.

Examples:
  TABLE_NAME=dialin-data python scripts/dump_feedback.py
  python scripts/dump_feedback.py --table dialin-data --user user_123
  python scripts/dump_feedback.py --table dialin-data --json > feedback.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

_LAMBDA = Path(__file__).resolve().parent.parent / "lambda"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--table",
        default=os.environ.get("TABLE_NAME", ""),
        help="DynamoDB table name (default: TABLE_NAME env)",
    )
    p.add_argument(
        "--user",
        metavar="USER_ID",
        dest="only_user",
        default="",
        help="Only feedback under PK USER#USER_ID (query instead of scan)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max rows to show (default: 200)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit raw JSON instead of the human-readable report",
    )
    return p.parse_args()


def _fetch(table: Any, only_user: str, limit: int) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Attr, Key

    items: list[dict[str, Any]] = []
    if only_user:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(f"USER#{only_user}")
            & Key("SK").begins_with("FEEDBACK#"),
            "ScanIndexForward": False,
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = lek
    else:
        kwargs = {"FilterExpression": Attr("itemType").eq("ChatFeedback")}
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        # Newest first by the timestamp embedded in createdAt.
        items.sort(key=lambda i: str(i.get("createdAt") or ""), reverse=True)
    return items[:limit]


def _print_report(items: list[dict[str, Any]]) -> None:
    if not items:
        print("No feedback found.")
        return

    with_comment = sum(1 for i in items if (i.get("comment") or "").strip())
    print(f"{len(items)} feedback item(s) — {with_comment} with a comment\n")
    print("=" * 72)
    for i in items:
        created = i.get("createdAt", "?")
        user = i.get("userId", "?")
        print(f"\n[{created}]  user={user}")
        comment = (i.get("comment") or "").strip()
        if comment:
            print(f"  comment : {comment}")
        um = textwrap.shorten((i.get("userMessage") or "").strip(), width=300, placeholder=" …")
        bm = textwrap.shorten((i.get("botMessage") or "").strip(), width=400, placeholder=" …")
        print(f"  asked   : {um or '(none)'}")
        print(f"  replied : {bm or '(none)'}")
    print("\n" + "=" * 72)


def main() -> None:
    args = _parse_args()
    table_name = args.table.strip()
    if not table_name:
        print("Missing table: pass --table or set TABLE_NAME", file=sys.stderr)
        sys.exit(1)

    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    items = _fetch(table, args.only_user.strip(), max(1, args.limit))

    if args.as_json:
        print(json.dumps(items, default=str, indent=2))
    else:
        _print_report(items)


if __name__ == "__main__":
    main()
