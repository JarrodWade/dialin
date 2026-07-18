#!/usr/bin/env bash
# Write web/dialin-config.js from Terraform output (file is gitignored).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="$(terraform -chdir="$ROOT/terraform" output -raw api_endpoint)"
# Optional SSE streaming Function URL (null / empty when enable_chat_streaming is false).
STREAM="$(terraform -chdir="$ROOT/terraform" output -raw chat_stream_url 2>/dev/null || true)"
if [[ "$STREAM" == "null" ]]; then
  STREAM=""
fi
# Function URLs often include a trailing slash — strip it for fetch base URLs.
STREAM="${STREAM%/}"
OUT="$ROOT/web/dialin-config.js"
EXAMPLE="$ROOT/web/dialin-config.example.js"
LIMIT="${CHAT_HISTORY_TURN_LIMIT:-24}"

if [[ ! -f "$OUT" ]]; then
  cp "$EXAMPLE" "$OUT"
  echo "Created $OUT from example — add clerkPublishableKey if needed."
fi

export OUT API STREAM LIMIT
python3 <<'PY'
import os
import pathlib
import re

out = pathlib.Path(os.environ["OUT"])
api = os.environ["API"]
stream = (os.environ.get("STREAM") or "").strip()
limit = int(os.environ["LIMIT"])
text = out.read_text(encoding="utf-8")

def set_key(key: str, value_repr: str) -> None:
    global text
    pat = rf"(\b{re.escape(key)}\s*:\s*)([^,\n}}]+)"
    if re.search(pat, text):
        text = re.sub(pat, rf"\g<1>{value_repr}", text, count=1)
        return
    marker = "window.DIALIN_CONFIG = window.DIALIN_CONFIG || {"
    if marker in text:
        text = text.replace(marker, f"{marker}\n  {key}: {value_repr},", 1)

set_key("apiBase", repr(api))
set_key("chatHistoryTurnLimit", str(limit))
if stream:
    set_key("streamApiBase", repr(stream))
out.write_text(text, encoding="utf-8")
bits = [f"apiBase, chatHistoryTurnLimit={limit}"]
if stream:
    bits.append("streamApiBase")
print(f"Updated {out} ({', '.join(bits)})")
PY
