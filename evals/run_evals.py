"""Live prompt-eval runner.

Runs each scenario N times against the **real** Bedrock model, scores the
structural checks, and writes a markdown + JSON report with per-scenario
pass-rates, token cost, cache-hit ratio, and a diff vs. a saved baseline.

Execution model (see evals/scenarios/__init__.py for the why):
  * DynamoDB  -> a real *scratch* table (default ``dialin-eval``), auto-created,
                 seeded fresh per scenario+rep under a unique user id.
  * Bedrock   -> the real model (the thing under test).
  * search_web / get_youtube_transcript / retrieve_journal -> canned stubs
                 (see evals/fixtures.py) so runs are reproducible and free.

Usage:
  python -m evals.run_evals --list
  python -m evals.run_evals                       # all suites, 3 reps
  python -m evals.run_evals --suite trips --reps 5
  python -m evals.run_evals --save-baseline       # snapshot pass-rates
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "lambda"
BASELINE_DIR = ROOT / "evals" / "baselines"
DEFAULT_REPORT_DIR = ROOT / "reports"

# Prod default (terraform variables.tf): Claude Haiku 4.5 US inference profile.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_TABLE = "dialin-eval"

# Rough USD per 1M tokens — override via env. Cache reads are billed at a
# fraction of input; we surface the cache ratio separately so the headline
# cost stays simple and clearly an estimate.
PRICE_INPUT_PER_M = float(os.environ.get("EVAL_PRICE_INPUT_PER_M", "1.0"))
PRICE_OUTPUT_PER_M = float(os.environ.get("EVAL_PRICE_OUTPUT_PER_M", "5.0"))


def _setup_import_path() -> None:
    if str(LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDA_DIR))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _ensure_scratch_table(table_name: str, region: str) -> None:
    """Create the eval scratch table if it does not already exist (idempotent)."""
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb", region_name=region)
    try:
        client.describe_table(TableName=table_name)
        return
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table_name)


def _select_scenarios(suite_args: list[str]) -> list[Any]:
    from evals import scenarios as S

    suites = S.suites()
    if not suite_args:
        out: list[Any] = []
        for sc in suites.values():
            out.extend(sc)
        return out

    wanted: list[Any] = []
    for name in suite_args:
        if name not in suites:
            raise SystemExit(f"unknown suite {name!r}; choices: {', '.join(sorted(suites))}")
        wanted.extend(suites[name])
    return wanted


def _baseline_path(suite_args: list[str]) -> Path:
    name = "all" if not suite_args else "_".join(sorted(suite_args))
    return BASELINE_DIR / f"{name}.json"


def _aggregate(results: list[Any]) -> dict[str, Any]:
    """Fold reps for one scenario into a summary dict."""
    reps = len(results)
    passes = sum(1 for r in results if r.passed)
    pass_rate = passes / reps if reps else 0.0

    # Per-check pass counts across reps.
    check_pass: dict[str, int] = {}
    for r in results:
        for c in r.results:
            check_pass[c.label] = check_pass.get(c.label, 0) + (1 if c.passed else 0)

    usage_keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    usage_sum = {k: sum(int(r.usage.get(k, 0)) for r in results) for k in usage_keys}

    errors = sum(1 for r in results if any(c.label == "runner_error" for c in r.results))

    return {
        "scenario_id": results[0].scenario_id,
        "rule": results[0].rule,
        "reps": reps,
        "passes": passes,
        "pass_rate": pass_rate,
        "errors": errors,
        "checks": {lbl: f"{n}/{reps}" for lbl, n in sorted(check_pass.items())},
        "failing_checks": sorted(lbl for lbl, n in check_pass.items() if n < reps),
        "iteration_caps": sum(1 for r in results if r.hit_cap),
        "usage": usage_sum,
        "sample_reply": results[-1].reply,
        "tool_calls_last": results[-1].tool_calls,
        "calls_detail_last": results[-1].calls_detail,
    }


def _est_cost(usage: dict[str, int]) -> float:
    return (
        usage.get("inputTokens", 0) / 1_000_000 * PRICE_INPUT_PER_M
        + usage.get("outputTokens", 0) / 1_000_000 * PRICE_OUTPUT_PER_M
    )


def _render_markdown(
    summaries: list[dict[str, Any]],
    *,
    model_id: str,
    reps: int,
    baseline: dict[str, float] | None,
    totals: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append(f"# dialin eval report — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"- model: `{model_id}`")
    lines.append(f"- scenarios: {len(summaries)} × {reps} reps = {len(summaries) * reps} turns")
    lines.append(f"- aggregate pass-rate: **{totals['mean_pass_rate']:.0%}**")
    lines.append(
        f"- tokens: {totals['usage']['inputTokens']:,} uncached-in / "
        f"{totals['usage']['outputTokens']:,} out  ·  "
        f"prompt-cache hit: {totals['cache_ratio']:.0%} of prompt tokens "
        f"({totals['usage']['cacheReadInputTokens']:,} cached)"
    )
    lines.append(f"- estimated cost: ~${totals['cost']:.4f} (rough; see EVAL_PRICE_* envs)")
    if totals["iteration_caps"]:
        lines.append(f"- ⚠️ iteration-cap hits: {totals['iteration_caps']}")
    if totals.get("runner_errors"):
        lines.append(f"- ⚠️ runner errors (e.g. transient network): {totals['runner_errors']} — pass-rates undercounted")
    lines.append("")

    header = "| scenario | rule | pass | caps |"
    sep = "|---|---|---|---|"
    if baseline is not None:
        header = "| scenario | rule | pass | Δ baseline | caps |"
        sep = "|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    for s in summaries:
        rate = f"{s['pass_rate']:.0%} ({s['passes']}/{s['reps']})"
        caps = str(s["iteration_caps"]) if s["iteration_caps"] else ""
        if baseline is not None:
            base = baseline.get(s["scenario_id"])
            if base is None:
                delta = "new"
            else:
                d = s["pass_rate"] - base
                delta = "—" if abs(d) < 1e-9 else f"{d:+.0%}"
            lines.append(f"| {s['scenario_id']} | {s['rule']} | {rate} | {delta} | {caps} |")
        else:
            lines.append(f"| {s['scenario_id']} | {s['rule']} | {rate} | {caps} |")
    lines.append("")

    failing = [s for s in summaries if s["failing_checks"]]
    if failing:
        lines.append("## Failing checks (not always passing)")
        lines.append("")
        for s in failing:
            lines.append(f"### {s['scenario_id']} ({s['rule']}) — {s['pass_rate']:.0%}")
            for lbl in s["failing_checks"]:
                lines.append(f"- `{lbl}` passed {s['checks'][lbl]}")
            calls = s.get("calls_detail_last") or []
            if calls:
                lines.append("- tool calls (last rep):")
                for c in calls:
                    inp = json.dumps(c.get("input", {}), default=str)
                    if len(inp) > 200:
                        inp = inp[:200] + "…"
                    lines.append(f"    - `{c.get('name')}` {inp}")
            else:
                lines.append("- tool calls (last rep): (none)")
            reply = (s["sample_reply"] or "").strip().replace("\n", " ")
            lines.append(f"- reply (last rep): {reply[:300]}")
            lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run dialin prompt evals against live Bedrock.")
    parser.add_argument("--suite", action="append", default=[], help="Suite name (repeatable). Default: all.")
    parser.add_argument("--reps", type=int, default=3, help="Reps per scenario (default 3).")
    parser.add_argument("--model", default=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID))
    parser.add_argument("--table", default=os.environ.get("EVAL_TABLE_NAME", DEFAULT_TABLE))
    parser.add_argument("--region", default=os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--save-baseline", action="store_true", help="Write this run's pass-rates as the baseline.")
    parser.add_argument("--no-baseline", action="store_true", help="Skip baseline diff in the report.")
    parser.add_argument("--list", action="store_true", help="List selected scenarios and exit (no AWS/model).")
    args = parser.parse_args(argv)

    _setup_import_path()

    # --list works without AWS creds or the lambda model deps.
    if args.list:
        scenarios = _select_scenarios(args.suite)
        for sc in scenarios:
            print(f"{sc.id:32s} {sc.rule:20s} {sc.message[:60]}")
        print(f"\n{len(scenarios)} scenarios.")
        return 0

    # Configure the lambda runtime BEFORE importing ddb/tools/bedrock.
    os.environ["TABLE_NAME"] = args.table
    os.environ.setdefault("AWS_REGION", args.region)
    os.environ.setdefault("BEDROCK_REGION", args.region)
    os.environ["BEDROCK_MODEL_ID"] = args.model
    os.environ.setdefault("BEDROCK_PROMPT_CACHING", "true")
    # Keep Titan embeddings off — retrieve_journal is stubbed.
    os.environ.pop("BEDROCK_EMBEDDING_MODEL_ID", None)

    print(f"Ensuring scratch table {args.table!r} in {args.region} ...")
    _ensure_scratch_table(args.table, args.region)

    import tools
    from evals import fixtures
    from evals import harness as H

    restore = fixtures.install(tools)

    scenarios = _select_scenarios(args.suite)
    run_tag = uuid.uuid4().hex[:6]
    print(f"Running {len(scenarios)} scenarios × {args.reps} reps on {args.model} (run {run_tag})\n")

    summaries: list[dict[str, Any]] = []
    started = time.time()
    try:
        for sc in scenarios:
            rep_results = []
            for rep in range(args.reps):
                sc.user_id = f"eval-{sc.id}-{run_tag}-{rep}"
                try:
                    res = H.run_scenario(sc)  # live model (no model_client)
                except Exception as e:  # noqa: BLE001 — never let one blip nuke the whole run
                    res = H.ScenarioResult(
                        scenario_id=sc.id,
                        rule=sc.rule,
                        results=[H.CheckResult("runner_error", False, f"{type(e).__name__}: {str(e)[:200]}")],
                        reply=f"(runner error: {type(e).__name__})",
                        tool_calls=[],
                        usage={},
                        iterations=0,
                        hit_cap=False,
                    )
                    print(f"  [ERR ] {sc.id} rep {rep + 1}/{args.reps}  {type(e).__name__}: {str(e)[:120]}")
                    rep_results.append(res)
                    continue
                rep_results.append(res)
                mark = "ok " if res.passed else "FAIL"
                print(f"  [{mark}] {sc.id} rep {rep + 1}/{args.reps}  tools={res.tool_calls}")
            summaries.append(_aggregate(rep_results))
    finally:
        restore()

    # Totals.
    usage_keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    total_usage = {k: sum(s["usage"].get(k, 0) for s in summaries) for k in usage_keys}
    # Bedrock reports cached prompt tokens (cacheReadInputTokens) SEPARATELY from
    # the uncached inputTokens. The meaningful hit ratio is cached / all-prompt.
    cache_read = total_usage["cacheReadInputTokens"]
    prompt_total = total_usage["inputTokens"] + cache_read
    cache_ratio = cache_read / prompt_total if prompt_total else 0.0
    totals = {
        "mean_pass_rate": statistics.mean([s["pass_rate"] for s in summaries]) if summaries else 0.0,
        "usage": total_usage,
        "cache_ratio": cache_ratio,
        "cost": _est_cost(total_usage),
        "iteration_caps": sum(s["iteration_caps"] for s in summaries),
        "runner_errors": sum(s.get("errors", 0) for s in summaries),
        "elapsed_s": round(time.time() - started, 1),
    }

    baseline: dict[str, float] | None = None
    if not args.no_baseline:
        bp = _baseline_path(args.suite)
        if bp.exists():
            baseline = {k: float(v) for k, v in json.loads(bp.read_text()).items()}

    # Write reports.
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = _render_markdown(summaries, model_id=args.model, reps=args.reps, baseline=baseline, totals=totals)
    (report_dir / f"{ts}.md").write_text(md)
    (report_dir / f"{ts}.json").write_text(
        json.dumps({"model": args.model, "reps": args.reps, "totals": totals, "scenarios": summaries}, indent=2, default=str)
    )

    if args.save_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        bp = _baseline_path(args.suite)
        bp.write_text(json.dumps({s["scenario_id"]: round(s["pass_rate"], 4) for s in summaries}, indent=2))
        print(f"\nSaved baseline -> {bp}")

    print("\n" + md)
    print(f"\nReport: {report_dir / f'{ts}.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
