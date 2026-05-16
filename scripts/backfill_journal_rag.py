#!/usr/bin/env python3
"""Rebuild journal RAG chunks for existing Brew / Coffee / Visit rows.

Scans Dynamo (fine for modest tables). Calls the same journal_rag sync_* helpers as writes.
Requires AWS credentials locally; set TABLE_NAME and BEDROCK_EMBEDDING_MODEL_ID."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)

_LAMBDA = Path(__file__).resolve().parent.parent / "lambda"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
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
        help="Only entities under PK USER#USER_ID",
    )
    p.add_argument(
        "--embedding-model",
        default=os.environ.get("BEDROCK_EMBEDDING_MODEL_ID", "").strip(),
        help="Overrides BEDROCK_EMBEDDING_MODEL_ID env",
    )
    p.add_argument(
        "--bedrock-region",
        default=os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", ""),
        help="Bedrock embedding region (default BEDROCK_REGION or AWS_REGION)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Pause after each embedding write (avoid throttling)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows only; no Bedrock writes",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    table = args.table.strip()
    if not table:
        print("Missing table: pass --table or set TABLE_NAME", file=sys.stderr)
        sys.exit(1)

    os.environ["TABLE_NAME"] = table
    if args.embedding_model:
        os.environ["BEDROCK_EMBEDDING_MODEL_ID"] = args.embedding_model
    if args.bedrock_region:
        os.environ["BEDROCK_REGION"] = args.bedrock_region

    sys.path.insert(0, str(_LAMBDA))

    try:
        import boto3
    except ImportError:
        print("Install boto3 for local runs: pip install boto3", file=sys.stderr)
        sys.exit(1)

    import ddb  # noqa: E402 — after TABLE_NAME env
    import journal_rag  # noqa: E402

    def _ddb():
        r = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if r:
            return boto3.resource("dynamodb", region_name=r)
        return boto3.resource("dynamodb")

    if args.dry_run:
        dynamodb = _ddb()
        tbl = dynamodb.Table(table)

        brewed = coffees = visits = 0
        scan_kw: dict = {}
        only = args.only_user.strip()
        while True:
            resp = tbl.scan(**scan_kw)
            for item in resp.get("Items", []):
                pk = item.get("PK") or ""
                sk = item.get("SK") or ""
                if not pk.startswith("USER#"):
                    continue
                if only and pk != f"USER#{only}":
                    continue
                if sk.startswith("BREW#"):
                    brewed += 1
                elif sk.startswith("COFFEE#"):
                    coffees += 1
                elif sk.startswith("VISIT#"):
                    visits += 1
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            scan_kw["ExclusiveStartKey"] = lek

        print(
            f"dry-run: would process brews={brewed} coffees={coffees} visits={visits} "
            f"(user filter={only or 'ALL'})"
        )
        if not journal_rag.enabled():
            print(
                "hint: embedding disabled until BEDROCK_EMBEDDING_MODEL_ID set; "
                "live run needs it + IAM bedrock:InvokeModel",
                file=sys.stderr,
            )
        return

    if not journal_rag.enabled():
        print(
            "journal_rag is disabled: need TABLE_NAME, BEDROCK_EMBEDDING_MODEL_ID, Bedrock IAM",
            file=sys.stderr,
        )
        sys.exit(1)

    dynamodb = _ddb()
    tbl = dynamodb.Table(table)

    brewed = coffees = visits = errors = 0
    scan_kw = {}
    only = args.only_user.strip()

    while True:
        resp = tbl.scan(**scan_kw)
        for item in resp.get("Items", []):
            pk = item.get("PK") or ""
            sk = item.get("SK") or ""
            if not pk.startswith("USER#"):
                continue
            if only and pk != f"USER#{only}":
                continue
            user_id = pk.removeprefix("USER#")
            try:
                if sk.startswith("BREW#"):
                    brew = ddb._strip_keys(item)
                    cof = None
                    cid = brew.get("coffeeId")
                    if cid:
                        cof = ddb.get_coffee(user_id, str(cid))
                    journal_rag.sync_brew(user_id, brew, cof)
                    brewed += 1
                elif sk.startswith("COFFEE#"):
                    journal_rag.sync_coffee(user_id, ddb._strip_keys(item))
                    coffees += 1
                elif sk.startswith("VISIT#"):
                    journal_rag.sync_visit(user_id, ddb._strip_keys(item))
                    visits += 1
                else:
                    continue
                if args.sleep > 0:
                    time.sleep(args.sleep)
            except Exception:
                errors += 1
                _LOG.exception(
                    "backfill failed pk=%s sk=%s",
                    pk,
                    sk[:80],
                )

        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        scan_kw["ExclusiveStartKey"] = lek

    print(f"done: brews={brewed} coffees={coffees} visits={visits} errors={errors}")


if __name__ == "__main__":
    main()
