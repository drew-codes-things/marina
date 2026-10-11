"""Which LLM backend to use.

Backends are tried in this order in auto mode:

  server  llm.base_url, the main machine
  ...     anything under llm.extra_backends, in the order listed
  local   llm.fallback_base_url, usually Ollama on this Mac

  auto    the first of those that answers
  <name>  that backend only
"""
import threading

from process.config import load_config

_config = load_config()
_llm = _config.get("llm") or {}


def _clean(url):
    return (url or "").strip() or None


def _build():
    out = [{
        "name": "server",
        "label": _llm.get("label") or "GPU server",
        "base_url": _clean(_llm.get("base_url")),
        "api_key": _llm.get("api_key") or _config.get("OPENAI_API_KEY") or "not-needed",
        "model": _llm.get("model") or _config.get("model"),
        "timeout": float(_llm.get("timeout_seconds", 30.0)),
    }]
    for extra in _llm.get("extra_backends") or []:
        name = str(extra.get("name") or "").strip().lower()
        url = _clean(extra.get("base_url"))
        if not name or not url or name in ("auto", "server", "local"):
            print(f"[llm] skipping extra backend {extra!r}: needs a unique "
                  "name and a base_url", flush=True)
            continue
        out.append({
            "name": name,
            "label": extra.get("label") or name,
            "base_url": url,
            "api_key": extra.get("api_key") or "not-needed",
            "model": extra.get("model") or out[0]["model"],
            "timeout": float(extra.get("timeout_seconds", 60.0)),
        })
    local = _clean(_llm.get("fallback_base_url"))
    if local:
        out.append({
            "name": "local",
            "label": _llm.get("fallback_label") or "This Mac",
            "base_url": local,
            "api_key": _llm.get("fallback_api_key") or "not-needed",
            "model": _llm.get("fallback_model") or out[0]["model"],
            "timeout": 60.0,
        })
    return out


BACKENDS = _build()
NAMES = tuple(b["name"] for b in BACKENDS)
VALID = ("auto",) + NAMES

_lock = threading.Lock()
_mode = (_llm.get("mode") or "auto").strip().lower()
if _mode not in VALID:
    _mode = "auto"

_last_used = NAMES[0] if _mode == "auto" else _mode

_models = {b["name"]: b["model"] for b in BACKENDS}


def get(name):
    for b in BACKENDS:
        if b["name"] == name:
            return b
    return None


def label(name=None):
    b = get(name or current())
    return b["label"] if b else (name or "")


def labels():
    return {b["name"]: b["label"] for b in BACKENDS}


def mode():
    return _mode


def set_mode(new):
    global _mode, _last_used
    new = (new or "").strip().lower()
    if new not in VALID:
        raise ValueError(f"mode must be one of {', '.join(VALID)}")
    with _lock:
        _mode = new
        if new != "auto":
            _last_used = new
    return _mode


def note_used(which):
    global _last_used
    if which in NAMES:
        _last_used = which


def model_for(which=None):
    return _models.get(which or current())


def set_model(which, name):
    which = (which or "").strip().lower()
    if which not in NAMES:
        raise ValueError(f"backend must be one of {', '.join(NAMES)}")
    if not name or not str(name).strip():
        raise ValueError("model name required")
    with _lock:
        _models[which] = str(name).strip()
    return _models[which]


def models():
    return dict(_models)


def current():
    return _last_used if _mode == "auto" else _mode


def describe():
    cur = current()
    return cur + (" (auto)" if _mode == "auto" else "")
