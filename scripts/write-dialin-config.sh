#!/usr/bin/env bash
# Bake Terraform API URL into web/dialin-config.js (run from repo root after apply).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="$(terraform -chdir="$ROOT/terraform" output -raw api_endpoint)"
OUT="$ROOT/web/dialin-config.js"
LIMIT="${CHAT_HISTORY_TURN_LIMIT:-24}"

export OUT API LIMIT
python3 <<'PY'
import os
import pathlib
import re

out = os.environ["OUT"]
api = os.environ["API"]
limit = int(os.environ["LIMIT"])
path = pathlib.Path(out)
content = (
    path.read_text(encoding="utf-8")
    if path.exists()
    else "window.DIALIN_CONFIG = window.DIALIN_CONFIG || {};\n"
)

def set_key(key: str, value_repr: str) -> None:
    global content
    pat = rf"(\b{re.escape(key)}\s*:\s*)([^,\n}}]+)"
    if re.search(pat, content):
        content = re.sub(pat, rf"\1{value_repr}", content, count=1)
        return
    marker = "window.DIALIN_CONFIG = window.DIALIN_CONFIG || {"
    if marker in content:
        content = content.replace(
            marker,
            f"{marker}\n  {key}: {value_repr},",
            1,
        )
    else:
        content = f"window.DIALIN_CONFIG = window.DIALIN_CONFIG || {{ {key}: {value_repr} }};\n"

set_key("apiBase", repr(api))
set_key("chatHistoryTurnLimit", str(limit))
path.write_text(content, encoding="utf-8")
print(f"Updated {path} (apiBase, chatHistoryTurnLimit={limit})")
PY
