#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SETUP="./setup-$([ "$(uname)" = Darwin ] && echo mac || echo linux).sh"
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv. Run $SETUP" >&2; exit 1; }

HOST="${1:-}"
MODEL="${2:-}"

HOST="$HOST" MODEL="$MODEL" "$PY" - <<'PY'
import json, os, sys, time, urllib.error, urllib.request, yaml
from pathlib import Path

cfg = yaml.safe_load(Path("character_config.yaml").read_text())
base = os.environ.get("HOST") or (cfg["llm"].get("base_url") or "").rstrip("/")
model = os.environ.get("MODEL") or cfg["llm"]["model"]
if not base:
    sys.exit("No endpoint. Pass one, or set llm.base_url in character_config.yaml")
base = base.removesuffix("/v1")

def _post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)

def call(prompt, n_predict=120):
    """Try Ollama's /api/generate, then llama.cpp's /completion."""
    t0 = time.time()
    try:
        d = _post(f"{base}/api/generate",
                  {"model": model, "prompt": prompt, "stream": False,
                   "options": {"num_predict": n_predict}})
        return d, time.time() - t0
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    d = _post(f"{base}/completion",
              {"prompt": prompt, "n_predict": n_predict, "stream": False})
    t = d.get("timings", {})
    return {
        "prompt_eval_count": t.get("prompt_n", 0),
        "prompt_eval_duration": t.get("prompt_ms", 0) * 1e6,
        "eval_count": t.get("predicted_n", 0),
        "eval_duration": t.get("predicted_ms", 0) * 1e6,
    }, time.time() - t0

print(f"endpoint : {base}")
print(f"model    : {model}\n")

long_prompt = ("You are a helpful assistant. " + "Context sentence. " * 120
               + "\n\nQuestion: name three colours.")

for label, prompt, n in [("short prompt", "Say hello.", 60),
                         ("long prompt (~900 tok)", long_prompt, 60)]:
    try:
        d, wall = call(prompt, n)
    except Exception as e:
        print(f"{label:<24} FAILED: {e}")
        continue
    pe, ped = d.get("prompt_eval_count", 0), d.get("prompt_eval_duration", 0) / 1e9
    ec, ed = d.get("eval_count", 0), d.get("eval_duration", 0) / 1e9
    print(f"{label}")
    print(f"  prefill  : {pe:>5} tok in {ped:6.2f}s  = {pe/max(ped,1e-6):7.1f} tok/s")
    print(f"  generate : {ec:>5} tok in {ed:6.2f}s  = {ec/max(ed,1e-6):7.1f} tok/s")
    print(f"  wall     : {wall:.2f}s\n")

print("Under ~2s to first word feels conversational. Prefill is most of that.")
PY
