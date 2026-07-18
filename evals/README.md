# dialin prompt eval harness

A test harness for the chat assistant's **behavior**, not just its plumbing. The
model is the variable — we can't assert on exact reply wording, but we *can*
deterministically assert on **tool-call behavior** (which tools, what args, what
order) and apply light checks to the reply text. Every assertion maps back to a
specific system-prompt rule (e.g. `CORE-2e`, `CORE-P1`), so when you slim the
prompt you can see exactly which rule's pass-rate moves.

Rule taxonomy: `CORE-*` labels a rule in the always-on system prompt
(`lambda/prompts/core.md`); `TRIP-*` labels a step inside the trip-place-
discovery appendix (`lambda/prompts/trip_appendix.md`), which is only attached
when the router fires (see `bedrock.want_trip_place_discovery_appendix`).

---

## The three layers of testing (don't conflate them)

| Layer | What it tests | Model | Where | Cost |
|---|---|---|---|---|
| **Router unit tests** | Pure functions like `want_trip_place_discovery_appendix`, `_wants_youtube` | none | `tests/` | free, every PR |
| **Harness plumbing** | The tool loop, trace capture, dispatch wiring, the scenarios themselves | *fake* (scripted) | `tests/test_eval_harness.py`, `tests/test_eval_scenarios.py` | free, every PR |
| **Prompt quality** | Does the live model follow the prompt rules? | **live Bedrock** | `make eval` | cents, on demand |

The first two run in CI and block PRs. The third is the actual prompt-quality
measurement and runs on demand against a real model.

---

## Execution model (one live run)

```
seeded scratch DynamoDB  +  live Bedrock converse  +  local tool dispatch
```

- **DynamoDB** — a real *scratch* table (default `dialin-eval`, auto-created),
  seeded fresh per scenario+rep under a unique user id. Not prod data.
- **Bedrock** — the **real** model (the thing under test).
- **`search_web` / `get_youtube_transcript` / `retrieve_journal`** — swapped for
  canned stubs (`fixtures.py`) so runs are reproducible, free, and need no
  Tavily key or Titan access. Every other (DynamoDB-backed) tool runs for real.

> We can't wrap a live run in `moto` like the CI tests, because `moto` would
> intercept the Bedrock call too. Hence the real scratch table.

---

## What exists today

**Phase 1 (done):** instrumentation + harness core.
- `bedrock._run_turn(...) -> TurnResult` captures the full trace
  (`text`, `tool_calls[(name,input,output)]`, `iterations`, `hit_iteration_cap`,
  `attachments`, `usage`). `generate_reply` is a thin wrapper returning `.text`.
- `harness.py` — `Scenario`/`ScenarioResult`/`CheckResult` dataclasses, the
  assertion DSL, and `run_scenario`.
- `fakes.FakeBedrockClient` — scripted converse responses for CI plumbing.

**Phase 2 (done):** scenarios + live runner.
- `fixtures.py` — canned external-IO stubs + `install()`/restore.
- `scenarios/` — 19 scenarios across rule families, tagged by rule.
- `run_evals.py` — live CLI: scratch table, N reps, scoring, md+json report,
  baseline diff.
- `make eval` / `make eval-list`; CI smoke test over all scenarios.

**Phase 3 (not started):** see [Roadmap](#roadmap).

---

## Layout

```
evals/
  harness.py          # Scenario model, assertion DSL, run_scenario
  fakes.py            # scripted fake Bedrock client (CI plumbing only)
  fixtures.py         # canned search_web / youtube / retrieve_journal stubs
  run_evals.py        # live CLI runner (reps, scoring, report, baseline diff)
  scenarios/
    coffees.py        # CORE-P1 snapshot, CORE-2a roaster resolution, add_coffee parsing
    equipment.py      # CORE-2b-add cataloging, CORE-2d edits, CORE-2b brew gear resolution
    cafes_visits.py   # CORE-2e badge / no re-add / one-log / visit correction
    corrections.py    # CORE-2d edit-not-recreate, confirm-before-delete
    trips.py          # TRIP routing, CORE-5c, city anchoring, CORE-0 voice, CORE-P0
    recall.py         # CORE-2f RAG, CORE-3 dial-in advice, CORE-4 summarize
  baselines/          # committed pass-rate snapshots (regression gate)
reports/              # generated md+json reports (gitignored)
```

---

## Prerequisites (one-time)

1. **AWS credentials** in your shell for the account with Bedrock access.
2. **Bedrock model access** enabled (us-east-1) for
   `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the prod default).
3. **IAM** on those creds: `bedrock:InvokeModel`, `bedrock:Converse`, and
   DynamoDB CRUD on a `dialin-eval*` table.

No Docker, no Tavily key.

---

## Running

```bash
make eval-list                         # list scenarios (no AWS, no model)
make eval                              # all suites, 3 reps, live
make eval ARGS='--suite trips --reps 5'
make eval ARGS='--suite coffees --suite equipment'
make eval ARGS='--save-baseline'       # snapshot pass-rates as the baseline
```

Useful flags (`python -m evals.run_evals --help`):

| flag | default | meaning |
|---|---|---|
| `--suite` | all | suite name, repeatable |
| `--reps` | `3` | runs per scenario (stochasticity → pass-rate) |
| `--model` | Haiku 4.5 profile | model under test |
| `--table` | `dialin-eval` | scratch DynamoDB table |
| `--region` | `us-east-1` | AWS region |
| `--save-baseline` | off | write this run's pass-rates to `baselines/` |
| `--no-baseline` | off | skip the baseline diff |
| `--list` | off | print scenarios and exit |

Cost is a rough estimate; tune `EVAL_PRICE_INPUT_PER_M` / `EVAL_PRICE_OUTPUT_PER_M`
to real pricing.

---

## Reports

Each run writes `reports/<timestamp>.md` and `.json`:

- header: model, scenario×rep count, **aggregate pass-rate**, tokens,
  **cache-read ratio**, estimated cost, iteration-cap hits
- per-scenario table: `pass-rate (passes/reps)`, `Δ baseline`, caps
- a "Failing checks" section: which check labels didn't always pass, plus the
  last rep's tool trace and reply for debugging

Baselines live in `evals/baselines/<suite>.json` (`all.json` for a full run) and
are committed, so a prompt edit produces a visible regression diff.

---

## Adding a scenario

Scenarios are plain Python (callable predicates > YAML). A scenario is:
**seeded state + one message + a list of checks**, tagged with the rule it guards.

```python
# evals/scenarios/cafes_visits.py
from typing import Any
from evals import harness as H

def _cafe_roaster_badge() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_cafe(user_id, name="Anchorhead", city="Seattle", state="WA")

    return H.Scenario(
        id="cafe_roaster_badge",
        rule="CORE-2e",
        seed=seed,
        message="Anchorhead actually roasts their own beans now",
        checks=[
            H.called("update_cafe", where=lambda a: a.get("isRoaster") is True),
            H.not_called("add_cafe"),
            H.reply_excludes(["cafeid", "isroaster", "duplicate_place"]),
        ],
    )

SCENARIOS = [_cafe_roaster_badge(), ...]
```

To reference an id created by the seed inside a check, share a `state` dict:

```python
def _build():
    state: dict[str, Any] = {}
    def seed(ddb, u):
        c = ddb.create_coffee(u, roaster="Onyx", name="Guji", origin="Ethiopia")
        state["coffeeId"] = c["coffeeId"]
    return H.Scenario(
        id="...", rule="CORE-3", seed=seed, message="dial in my Guji on espresso",
        checks=[H.called("get_dialin_advice",
                         where=lambda a: a.get("coffeeId") == state.get("coffeeId"))],
    )
```

`Scenario` fields: `id`, `message`, `rule`, `seed`, `history`, `checks`,
`client_timezone`, `user_id`. Register a new *suite module* in
`scenarios/__init__.py::_MODULES`.

---

## Assertion DSL (`harness.py`)

All operate on a `TurnResult`. `where` predicates receive the tool's **input**
dict; exceptions inside them are swallowed (treated as no-match).

| check | passes when |
|---|---|
| `called(name, where=None)` | a matching tool call exists |
| `not_called(name, where=None)` | no matching tool call exists |
| `called_before(first, second)` | both called and `first` precedes `second` |
| `call_count(name, min=, max=)` | call count within bounds |
| `attachment(**flags)` | `trip_appendix` / `youtube` attachment matches |
| `no_iteration_cap()` | didn't hit `MAX_TOOL_ITERATIONS` |
| `reply_excludes([...])` | none of the substrings appear (no plumbing leak) |
| `reply_matches(pattern)` | regex matches the reply (case-insensitive) |
| `reply_max_sentences(n)` | ≤ n sentences (rough) |

Each returns a `CheckResult(label, passed, detail)`. A scenario passes a rep only
if **all** checks pass; the report aggregates pass-rate over reps.

---

## Roadmap (Phase 3)

- **LLM-as-judge** for the fuzzy text rules we can't check structurally
  (e.g. "did it invent a street address?"), graded by a *stronger* separate
  model to avoid self-grading bias.
- **Pass-rate gate**: fail when aggregate drops > X% below baseline.
- **Nightly CI workflow** (`workflow_dispatch` + AWS creds secret) that runs the
  live eval and uploads the report artifact.
- Then use the harness to drive the **prompt slim**: trim the system prompt and
  watch which rule pass-rates move.
