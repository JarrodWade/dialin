"""Scenario model, assertion DSL, and runner for the dialin prompt eval harness.

A *scenario* is: seeded DynamoDB state + a conversation + a list of *checks*.
Each check maps a system-prompt rule to a structural assertion over the turn's
tool trace (``bedrock.TurnResult``) or a fuzzy assertion over the reply text.

The runner is model-agnostic: pass a ``FakeBedrockClient`` for deterministic CI
plumbing tests, or leave ``model_client=None`` to hit the live client configured
in ``bedrock`` for real prompt-quality evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# A check receives a bedrock.TurnResult and returns a CheckResult.
Check = Callable[[Any], "CheckResult"]


@dataclass
class CheckResult:
    label: str
    passed: bool
    detail: str = ""


def _safe(pred: Callable[[dict[str, Any]], bool], value: dict[str, Any]) -> bool:
    """Run a user predicate without letting a KeyError/TypeError crash the run."""
    try:
        return bool(pred(value))
    except Exception:  # noqa: BLE001
        return False


def truthy(value: Any) -> bool:
    """Booleans from the model often arrive as the string ``"true"`` (the backend
    ``coerce_bool``s them). Use this in ``where`` predicates instead of ``is True``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# Assertion DSL — each factory returns a Check. Tag with the prompt rule it guards.
# ---------------------------------------------------------------------------


def called(name: str, where: Callable[[dict[str, Any]], bool] | None = None, *, label: str | None = None) -> Check:
    lbl = label or f"called({name})"

    def _check(tr: Any) -> CheckResult:
        for c in tr.tool_calls:
            if c.name == name and (where is None or _safe(where, c.input)):
                return CheckResult(lbl, True)
        return CheckResult(lbl, False, f"no matching {name} call; calls={tr.names()}")

    return _check


def not_called(name: str, where: Callable[[dict[str, Any]], bool] | None = None, *, label: str | None = None) -> Check:
    lbl = label or f"not_called({name})"

    def _check(tr: Any) -> CheckResult:
        for c in tr.tool_calls:
            if c.name == name and (where is None or _safe(where, c.input)):
                return CheckResult(lbl, False, f"unexpected {name} call input={c.input}")
        return CheckResult(lbl, True)

    return _check


def called_any(names: list[str], *, label: str | None = None) -> Check:
    """Pass if at least one tool in ``names`` was called."""
    lbl = label or f"called_any({','.join(names)})"

    def _check(tr: Any) -> CheckResult:
        for c in tr.tool_calls:
            if c.name in names:
                return CheckResult(lbl, True, f"saw {c.name}")
        return CheckResult(lbl, False, f"none of {names} called; calls={tr.names()}")

    return _check


def called_before(first: str, second: str, *, label: str | None = None) -> Check:
    """Both tools must be called and ``first`` must precede ``second``."""
    lbl = label or f"called_before({first},{second})"

    def _check(tr: Any) -> CheckResult:
        names = tr.names()
        if first not in names:
            return CheckResult(lbl, False, f"{first} never called; calls={names}")
        if second not in names:
            return CheckResult(lbl, False, f"{second} never called; calls={names}")
        ok = names.index(first) < names.index(second)
        return CheckResult(lbl, ok, "" if ok else f"order wrong: {names}")

    return _check


def call_count(name: str, *, min: int | None = None, max: int | None = None, label: str | None = None) -> Check:
    lbl = label or f"call_count({name},min={min},max={max})"

    def _check(tr: Any) -> CheckResult:
        n = sum(1 for c in tr.tool_calls if c.name == name)
        ok = (min is None or n >= min) and (max is None or n <= max)
        return CheckResult(lbl, ok, f"{name} called {n}x")

    return _check


def attachment(*, label: str | None = None, **flags: bool) -> Check:
    """Assert the turn attached (or didn't) the trip appendix / youtube tools."""
    lbl = label or f"attachment({flags})"

    def _check(tr: Any) -> CheckResult:
        bad = {k: (tr.attachments.get(k), v) for k, v in flags.items() if tr.attachments.get(k) != v}
        return CheckResult(lbl, not bad, "" if not bad else f"mismatch (got,want)={bad}")

    return _check


def no_iteration_cap(*, label: str | None = None) -> Check:
    lbl = label or "no_iteration_cap"

    def _check(tr: Any) -> CheckResult:
        return CheckResult(lbl, not tr.hit_iteration_cap, "hit MAX_TOOL_ITERATIONS" if tr.hit_iteration_cap else "")

    return _check


def reply_excludes(substrings: list[str], *, label: str | None = None) -> Check:
    """CORE-0: no implementation plumbing (field names, codes) leaks into the reply."""
    lbl = label or "reply_excludes"

    def _check(tr: Any) -> CheckResult:
        low = (tr.text or "").lower()
        hits = [s for s in substrings if s.lower() in low]
        return CheckResult(lbl, not hits, "" if not hits else f"leaked {hits}")

    return _check


def reply_matches(pattern: str, *, label: str | None = None) -> Check:
    lbl = label or f"reply_matches({pattern})"
    rx = re.compile(pattern, re.IGNORECASE)

    def _check(tr: Any) -> CheckResult:
        ok = bool(rx.search(tr.text or ""))
        return CheckResult(lbl, ok, "" if ok else "pattern not found")

    return _check


def reply_max_sentences(n: int, *, label: str | None = None) -> Check:
    """CORE-7: keep replies short. Rough sentence count via terminal punctuation."""
    lbl = label or f"reply_max_sentences({n})"

    def _check(tr: Any) -> CheckResult:
        sentences = [s for s in re.split(r"[.!?]+", tr.text or "") if s.strip()]
        ok = len(sentences) <= n
        return CheckResult(lbl, ok, f"{len(sentences)} sentences")

    return _check


# ---------------------------------------------------------------------------
# Scenario + runner
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    id: str
    message: str = ""
    rule: str = ""
    seed: Callable[[Any, str], None] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    client_timezone: str | None = None
    user_id: str = "eval-user"
    # Resolve the message at run time from the (already-imported) bedrock module —
    # lets a scenario target a prompt constant (e.g. the For You instruction)
    # without importing lambda deps at scenario-build time.
    message_factory: Callable[[Any], str] | None = None
    # Overrides for non-chat entrypoints (e.g. recommend_beans). None = use the
    # turn's normal heuristic / no cap, matching the default chat path.
    force_trip_appendix: bool | None = None
    max_web_searches: int | None = None


@dataclass
class ScenarioResult:
    scenario_id: str
    rule: str
    results: list[CheckResult]
    reply: str
    tool_calls: list[str]
    usage: dict[str, int]
    iterations: int
    hit_cap: bool
    calls_detail: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)


def run_scenario(scenario: Scenario, *, model_client: Any | None = None) -> ScenarioResult:
    """Seed state, run one turn, and evaluate every check.

    ``model_client`` overrides ``bedrock._client`` for the duration (use a
    FakeBedrockClient in CI). Leave None to use the live client already set.
    """
    import bedrock
    import ddb

    if scenario.seed is not None:
        scenario.seed(ddb, scenario.user_id)

    message = scenario.message_factory(bedrock) if scenario.message_factory else scenario.message

    prev_client = bedrock._client
    if model_client is not None:
        bedrock._client = model_client
    try:
        tr = bedrock._run_turn(
            scenario.user_id,
            scenario.history,
            message,
            client_timezone=scenario.client_timezone,
            force_trip_appendix=scenario.force_trip_appendix,
            max_web_searches=scenario.max_web_searches,
        )
    finally:
        if model_client is not None:
            bedrock._client = prev_client

    results = [chk(tr) for chk in scenario.checks]
    return ScenarioResult(
        scenario_id=scenario.id,
        rule=scenario.rule,
        results=results,
        reply=tr.text,
        tool_calls=tr.names(),
        usage=tr.usage,
        iterations=tr.iterations,
        hit_cap=tr.hit_iteration_cap,
        calls_detail=[{"name": c.name, "input": c.input} for c in tr.tool_calls],
    )
