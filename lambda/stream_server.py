"""Local HTTP/SSE server for streaming chat + For You recommendations.

Runs behind AWS Lambda Web Adapter in response-streaming mode, fronted by a
dedicated Lambda Function URL — see terraform/lambda_stream.tf. This is a
separate deployment artifact from handler.py's buffered API Gateway Lambda;
the two share the same lambda/ code bundle but run as different Lambda
functions (see the shared archive_file in terraform/lambda.tf).

Routes:
  GET  / /healthz
  POST /chat/stream
  POST /recommendations/beans/stream
  POST /recommendations/cafes/stream

CORS and OPTIONS preflight are handled by the Lambda Function URL's own `cors`
config (see terraform/lambda_stream.tf), not by this process.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
from http.server import BaseHTTPRequestHandler
from typing import Any

import auth
import bedrock
import ddb

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dialin.stream")


def _emit_sse(wfile, event: str, data: Any) -> None:
    frame = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
    wfile.write(frame)
    wfile.flush()


class _Handler(BaseHTTPRequestHandler):
    """HTTP/1.0-style handler: no Content-Length/chunked framing on the SSE
    response, so the connection closes at the end of each request and EOF marks
    the end of the stream."""

    server_version = "dialin-stream/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _resolve_user(self, body: dict[str, Any]) -> str:
        bearer = auth.extract_bearer(
            self.headers.get("Authorization") or self.headers.get("authorization")
        )
        logger.info(
            "%s auth_header=%s body_userId=%s",
            self.path,
            "yes" if bearer else "no",
            "yes" if body.get("userId") else "no",
        )
        return auth.resolve_user_id(bearer_token=bearer, body_user_id=body.get("userId"))

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/healthz"):
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/chat/stream":
            self._handle_chat_stream()
        elif self.path == "/recommendations/beans/stream":
            self._handle_beans_stream()
        elif self.path == "/recommendations/cafes/stream":
            self._handle_cafes_stream()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_chat_stream(self) -> None:  # noqa: C901
        import handler as _handler

        body = self._read_body()
        message = (body.get("message") or "").strip()
        raw_tz = body.get("clientTimezone")
        client_tz = raw_tz.strip() if isinstance(raw_tz, str) else None
        history = body.get("history") or []
        if not isinstance(history, list):
            history = []

        user_id = self._resolve_user(body)
        if not user_id:
            self._send_json(401, {"error": "Unauthorized"})
            return
        if not message:
            self._send_json(400, {"error": "missing required field: message"})
            return

        max_chars = _handler._CHAT_MESSAGE_MAX_CHARS
        if max_chars > 0 and len(message) > max_chars:
            self._send_json(
                413,
                {
                    "error": (
                        f"Message too long ({len(message)} chars; max {max_chars}). "
                        "Trim it and try again."
                    ),
                    "code": "MESSAGE_TOO_LONG",
                },
            )
            return

        try:
            chat_daily_limit = int(os.environ.get("CHAT_DAILY_LIMIT_PER_USER", "0"))
        except ValueError:
            chat_daily_limit = 0
        if chat_daily_limit > 0:
            allowed, used = ddb.consume_chat_quota(user_id, chat_daily_limit)
            if not allowed:
                self._send_json(
                    429,
                    {
                        "error": (
                            f"Daily chat limit reached ({used}/{chat_daily_limit} turns UTC). "
                            "Try again tomorrow or raise CHAT_DAILY_LIMIT_PER_USER."
                        ),
                        "code": "CHAT_QUOTA_EXCEEDED",
                        "used": used,
                        "limit": chat_daily_limit,
                    },
                )
                return

        trimmed = history[-_handler._HISTORY_TURN_LIMIT :]
        model_history = _handler._trim_history_by_chars(trimmed, _handler._CHAT_HISTORY_MAX_CHARS)
        max_web_searches_env = int(os.environ.get("CHAT_MAX_WEB_SEARCHES", "4"))
        max_web_searches = max_web_searches_env if max_web_searches_env > 0 else None

        self._begin_sse()
        _emit_sse(self.wfile, "status", {"tool": "_start", "label": "dialing in…"})
        try:
            for evt in bedrock.stream_turn(
                user_id=user_id,
                history=model_history,
                user_text=message,
                client_timezone=client_tz,
                max_web_searches=max_web_searches,
            ):
                if evt.type == "status":
                    _emit_sse(self.wfile, "status", evt.data)
                elif evt.type == "delta":
                    _emit_sse(self.wfile, "delta", {"text": evt.data})
                elif evt.type == "done":
                    result = evt.data
                    new_history = trimmed + [
                        {"role": "USER", "text": message},
                        {"role": "BOT", "text": result.text},
                    ]
                    _emit_sse(self.wfile, "done", {"reply": result.text, "history": new_history})
        except (BrokenPipeError, ConnectionResetError):
            logger.info("client disconnected mid-stream")
        except Exception:
            logger.exception("stream_turn failed")
            if chat_daily_limit > 0:
                ddb.refund_chat_quota(user_id, chat_daily_limit)
            try:
                _emit_sse(self.wfile, "error", {"error": "model invocation failed"})
            except Exception:  # noqa: BLE001
                pass

    def _handle_beans_stream(self) -> None:
        body = self._read_body()
        user_id = self._resolve_user(body)
        if not user_id:
            self._send_json(401, {"error": "Unauthorized"})
            return

        self._begin_sse()
        try:
            for evt in bedrock.stream_recommend_beans(user_id):
                if evt.type == "status":
                    _emit_sse(self.wfile, "status", evt.data)
                elif evt.type == "delta":
                    _emit_sse(self.wfile, "delta", {"text": evt.data})
                elif evt.type == "done":
                    _emit_sse(self.wfile, "done", {"recommendations": evt.data})
        except (BrokenPipeError, ConnectionResetError):
            logger.info("client disconnected mid-stream (beans)")
        except Exception:
            logger.exception("stream_recommend_beans failed")
            try:
                _emit_sse(self.wfile, "error", {"error": "model invocation failed"})
            except Exception:  # noqa: BLE001
                pass

    def _handle_cafes_stream(self) -> None:
        body = self._read_body()
        user_id = self._resolve_user(body)
        if not user_id:
            self._send_json(401, {"error": "Unauthorized"})
            return
        city = (body.get("city") or "").strip()
        if not city:
            self._send_json(400, {"error": "city is required"})
            return

        self._begin_sse()
        try:
            for evt in bedrock.stream_recommend_cafes(user_id, city):
                if evt.type == "status":
                    _emit_sse(self.wfile, "status", evt.data)
                elif evt.type == "delta":
                    _emit_sse(self.wfile, "delta", {"text": evt.data})
                elif evt.type == "done":
                    _emit_sse(self.wfile, "done", {"recommendations": evt.data})
        except ValueError as e:
            # Destination parse errors — emit as SSE error so the open stream can close cleanly.
            try:
                _emit_sse(self.wfile, "error", {"error": str(e)})
            except Exception:  # noqa: BLE001
                pass
        except (BrokenPipeError, ConnectionResetError):
            logger.info("client disconnected mid-stream (cafes)")
        except Exception:
            logger.exception("stream_recommend_cafes failed")
            try:
                _emit_sse(self.wfile, "error", {"error": "model invocation failed"})
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler) as httpd:
        httpd.daemon_threads = True
        logger.info("dialin stream server listening on :%d", port)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
