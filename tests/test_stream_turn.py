"""Streaming chat turn: SSE-facing event stream from bedrock.stream_turn.

Exercises the converse_stream event parsing (tool-use + text deltas) and the
live <thinking> tag filter, since these can't be verified against a live
Bedrock streaming response in this test suite.
"""

from __future__ import annotations

import importlib

USER = "stream-user-1"


def _fake_stream(events: list[dict]):
    return {"stream": iter(events)}


def test_stream_turn_emits_status_then_delta_then_done(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    responses = [
        _fake_stream(
            [
                {"messageStart": {"role": "assistant"}},
                {
                    "contentBlockStart": {
                        "contentBlockIndex": 0,
                        "start": {"toolUse": {"toolUseId": "t1", "name": "list_coffees"}},
                    }
                },
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{}"}}}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
                {"messageStop": {"stopReason": "tool_use"}},
                {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10}}},
            ]
        ),
        _fake_stream(
            [
                {"messageStart": {"role": "assistant"}},
                {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
                {
                    "contentBlockDelta": {
                        "contentBlockIndex": 0,
                        "delta": {"text": "<thinking>secret plan</thinking>Great, you have "},
                    }
                },
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "2 coffees on hand."}}},
                {"contentBlockStop": {"contentBlockIndex": 0}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 20}}},
            ]
        ),
    ]

    calls = {"n": 0}

    def fake_converse_stream(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(bedrock, "_client", type("C", (), {"converse_stream": staticmethod(fake_converse_stream)})())

    events = list(bedrock.stream_turn(USER, [], "how many coffees do I have?"))

    statuses = [e for e in events if e.type == "status"]
    deltas = [e for e in events if e.type == "delta"]
    dones = [e for e in events if e.type == "done"]

    assert len(statuses) == 1
    assert statuses[0].data == {"tool": "list_coffees", "label": "Checking your journal…"}

    joined = "".join(e.data for e in deltas)
    assert "thinking" not in joined.lower()
    assert "secret plan" not in joined
    assert joined == "Great, you have 2 coffees on hand."

    assert len(dones) == 1
    result = dones[0].data
    assert result.text == "Great, you have 2 coffees on hand."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "list_coffees"
    assert result.usage.get("inputTokens") == 150
    assert result.usage.get("outputTokens") == 30
    assert calls["n"] == 2


def test_thinking_stream_filter_handles_split_tags():
    from bedrock import _ThinkingStreamFilter

    f = _ThinkingStreamFilter()
    out = []
    out.append(f.feed("hello <thin"))
    out.append(f.feed("king>hidden reasoning</th"))
    out.append(f.feed("inking> world"))
    out.append(f.flush())

    joined = "".join(out)
    assert joined == "hello  world"
    assert "hidden" not in joined


def test_thinking_stream_filter_passthrough_when_no_tag():
    from bedrock import _ThinkingStreamFilter

    f = _ThinkingStreamFilter()
    out = "".join([f.feed("just a "), f.feed("normal reply."), f.flush()])
    assert out == "just a normal reply."


def test_stream_converse_text_done_joins_tokens_without_newlines(dynamodb_env, monkeypatch):
    """Regression: done used to '\\n'.join each Bedrock token → jumbled markdown at finish."""
    import bedrock

    importlib.reload(bedrock)

    def fake_converse_stream(**kwargs):
        return _fake_stream(
            [
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "**Caf"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "és"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " in Tokyo**"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "\n"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "**Glitch"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " Coffee** — praise."}}},
                {"messageStop": {"stopReason": "end_turn"}},
            ]
        )

    monkeypatch.setattr(
        bedrock, "_client", type("C", (), {"converse_stream": staticmethod(fake_converse_stream)})()
    )

    events = list(bedrock._stream_converse_text("sys", "user"))
    deltas = [e.data for e in events if e.type == "delta"]
    dones = [e.data for e in events if e.type == "done"]

    joined = "".join(deltas)
    assert joined == "**Cafés in Tokyo**\n**Glitch Coffee** — praise."
    assert len(dones) == 1
    assert dones[0] == joined
    assert "\n**és" not in dones[0]
    assert dones[0].count("\n") == 1


def test_tool_status_label_search_web_includes_query():
    from bedrock import _tool_status_label

    label = _tool_status_label("search_web", {"query": "best espresso Lisbon"})
    assert "best espresso Lisbon" in label

    assert _tool_status_label("search_web", {}) == "Searching the web…"
    assert _tool_status_label("totally_unknown_tool", {}) == "Using totally_unknown_tool…"
