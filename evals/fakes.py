"""Deterministic stand-ins so the harness/loop can be exercised without a live model.

``FakeBedrockClient`` replays a scripted sequence of converse responses (one per
``converse`` call), letting tests assert that the tool loop, trace capture, and
dispatch wiring behave correctly. Real prompt-quality evaluation uses the live
client instead — this is only for plumbing.
"""

from __future__ import annotations

from typing import Any


class FakeBedrockClient:
    """Replays scripted ``converse`` responses.

    Each ``script`` entry is consumed by one ``converse`` call:
      * ``{"tools": [(name, input_dict), ...]}`` -> a ``tool_use`` round
      * ``{"text": "final reply"}``              -> an ``end_turn`` round

    Captured ``converse`` kwargs are kept on ``.calls`` for assertions (e.g.
    verifying cache points or system blocks were attached).
    """

    def __init__(self, script: list[dict[str, Any]]):
        self._script = list(script)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        step = self._script[self._i] if self._i < len(self._script) else {"text": "(fake: script exhausted)"}
        self._i += 1

        if "tools" in step:
            content = [
                {
                    "toolUse": {
                        "toolUseId": f"tu-{self._i}-{j}",
                        "name": name,
                        "input": inp,
                    }
                }
                for j, (name, inp) in enumerate(step["tools"])
            ]
            return {
                "stopReason": "tool_use",
                "output": {"message": {"role": "assistant", "content": content}},
                "usage": {"inputTokens": 100, "outputTokens": 20},
            }

        text = step.get("text", "")
        return {
            "stopReason": "end_turn",
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"inputTokens": 80, "outputTokens": 10},
        }
