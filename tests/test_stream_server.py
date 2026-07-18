"""HTTP-level tests for stream_server.py: auth guards and a full SSE turn.

Spins up the real stdlib HTTP server on a loopback port (no AWS/Bedrock calls —
bedrock.stream_turn is monkeypatched for the happy-path test).
"""

from __future__ import annotations

import importlib
import json
import threading
from http.client import HTTPConnection


def _start_server(stream_server_mod):
    server = stream_server_mod.socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), stream_server_mod._Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _post(port: int, path: str, body: dict) -> tuple[int, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode("utf-8")
    conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_unauthorized_without_user(dynamodb_env, monkeypatch):
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)

    import handler
    import stream_server

    importlib.reload(handler)
    importlib.reload(stream_server)

    server, thread = _start_server(stream_server)
    try:
        port = server.server_address[1]
        status, _ = _post(port, "/chat/stream", {"message": "hi"})
        assert status == 401
    finally:
        _stop_server(server, thread)


def test_missing_message_returns_400(dynamodb_env, monkeypatch):
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)

    import handler
    import stream_server

    importlib.reload(handler)
    importlib.reload(stream_server)

    server, thread = _start_server(stream_server)
    try:
        port = server.server_address[1]
        status, _ = _post(port, "/chat/stream", {"message": "", "userId": "u1"})
        assert status == 400
    finally:
        _stop_server(server, thread)


def test_message_too_long_returns_413(dynamodb_env, monkeypatch):
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("CHAT_MESSAGE_MAX_CHARS", "10")

    import handler
    import stream_server

    importlib.reload(handler)
    importlib.reload(stream_server)

    server, thread = _start_server(stream_server)
    try:
        port = server.server_address[1]
        status, data = _post(port, "/chat/stream", {"message": "x" * 50, "userId": "u1"})
        assert status == 413
        assert json.loads(data)["code"] == "MESSAGE_TOO_LONG"
    finally:
        _stop_server(server, thread)


def test_full_turn_streams_sse_events(dynamodb_env, monkeypatch):
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)

    import bedrock
    import handler
    import stream_server

    importlib.reload(handler)
    importlib.reload(bedrock)
    importlib.reload(stream_server)

    def fake_stream_turn(**_kwargs):
        from bedrock import StreamEvent, TurnResult

        yield StreamEvent("status", {"tool": "list_coffees", "label": "Checking your journal…"})
        yield StreamEvent("delta", "Hello ")
        yield StreamEvent("delta", "world.")
        yield StreamEvent(
            "done",
            TurnResult(
                text="Hello world.",
                tool_calls=[],
                iterations=1,
                hit_iteration_cap=False,
                attachments={},
                usage={},
            ),
        )

    monkeypatch.setattr(stream_server.bedrock, "stream_turn", fake_stream_turn)

    server, thread = _start_server(stream_server)
    try:
        port = server.server_address[1]
        status, data = _post(port, "/chat/stream", {"message": "hi", "userId": "u1"})
        assert status == 200
        text = data.decode("utf-8")
        assert "event: status" in text
        assert "event: delta" in text
        assert "event: done" in text
        assert "Hello world." in text

        # "done" carries the reply + updated history, same contract as buffered /chat.
        done_frame = [f for f in text.split("\n\n") if f.startswith("event: done")][0]
        done_data = json.loads(done_frame.split("data: ", 1)[1])
        assert done_data["reply"] == "Hello world."
        assert done_data["history"][-1] == {"role": "BOT", "text": "Hello world."}
        assert done_data["history"][-2] == {"role": "USER", "text": "hi"}
    finally:
        _stop_server(server, thread)
