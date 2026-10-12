"""One memory and one conversation for every bridge.

The bridge that is always on (mine is lab3, which also does Telegram) keeps
her memory and the personal conversation, and serves them at /shared/ to
the others. On that one, set `shared.serve_token` and bind the bridge to the
network; requests from anywhere but localhost then need that token and can
reach /shared/ and nothing else. On the others, set `shared.url` and
`shared.token`, and they read and write there instead of their own files.

If the home bridge can't be reached, a bridge falls back to its local copy,
which it refreshes every time it does reach it, and tries again shortly.
"""
import threading
import time

import requests

from process.config import load_config

_cfg = load_config().get("shared") or {}

URL = str(_cfg.get("url") or "").strip().rstrip("/")
TOKEN = str(_cfg.get("token") or "").strip()
SERVE_TOKEN = str(_cfg.get("serve_token") or "").strip()
TIMEOUT = float(_cfg.get("timeout_seconds", 3))
RETRY_AFTER = 30.0

_down_until = 0.0
_lock = threading.Lock()


def enabled():
    return bool(URL and TOKEN)


def call(method, path, body=None):
    """The JSON reply, or None if the home bridge didn't answer."""
    global _down_until
    if not enabled() or time.time() < _down_until:
        return None
    try:
        r = requests.request(method, f"{URL}{path}", json=body, timeout=(2, TIMEOUT),
                             headers={"Authorization": f"Bearer {TOKEN}"})
        r.raise_for_status()
        out = r.json()
    except (requests.RequestException, ValueError) as e:
        with _lock:
            first = _down_until == 0.0 or time.time() >= _down_until
            _down_until = time.time() + RETRY_AFTER
        if first:
            print(f"[shared] {URL} didn't answer ({type(e).__name__}); "
                  "using the local copy for now", flush=True)
        return None
    with _lock:
        if _down_until:
            print(f"[shared] {URL} is back", flush=True)
            _down_until = 0.0
    return out
