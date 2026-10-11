"""Chat completions client with on-disk history.

Works with anything OpenAI-compatible (OpenAI, Ollama, llama.cpp, LM Studio,
vLLM). Which endpoints exist and in what order is up to process.backend.
"""
import json
import os
import random
import threading
import time

from openai import OpenAI, Timeout

from process import backend

from process.backend import current as backend_current
from process.backend import mode as backend_mode
from process.backend import model_for
from process.backend import note_used
from process import config
from process.config import load_config, resolve_data
from process.memory import extract as memory_extract
from process.memory import persona
from process.memory import store as memory
from process.tools import context as ambient
from process.tools import registry as tools

char_config = load_config()

_llm = char_config.get("llm") or {}

MAX_TOKENS = _llm.get("max_tokens", 2048)

HISTORY_TURNS = int(_llm.get("history_turns", 12))

_HISTORY_BASE = resolve_data(char_config["history_file"])


def _history_file():
    """Each preset keeps its own conversation, so switching to a
    presentation personality doesn't bring the personal one along."""
    name = config.preset()
    if name == "default":
        return str(_HISTORY_BASE)
    return str(_HISTORY_BASE.with_name(f"{_HISTORY_BASE.stem}.{name}{_HISTORY_BASE.suffix}"))


def _temperature():
    return config.setting("llm", "temperature", 1.0)


def _remember():
    return bool(config.setting("llm", "remember", True))


def _recall():
    """Off keeps what she remembers out of the prompt, e.g. for a demo."""
    return bool(config.setting("llm", "recall", True))


def _stable_context():
    from datetime import datetime
    now = datetime.now()
    h = now.hour
    part = ("the middle of the night" if h < 5 else "early morning" if h < 8 else
            "morning" if h < 12 else "afternoon" if h < 17 else
            "evening" if h < 22 else "late evening")
    return f"\n\nIt is {now:%A} {part}. Use this only if it is actually relevant."


def system_message():
    return {
        "role": "system",
        "content": (config.system_prompt()
                    + (memory.as_prompt_block() if _recall() else "")
                    + tools.prompt_addition()
                    + _stable_context()),
    }


def _tool_rounds():
    """How many rounds of tool calls one reply may take before she has to
    answer. Work on her machine is a chain of them: run, read, fix, rerun."""
    if not tools.wants_long_loop():
        return 1
    return max(1, int(config.setting("llm", "max_tool_rounds", 12)))


STICKY = float(_llm.get("fallback_sticky_seconds", 30))

THINKING = bool(_llm.get("thinking", False))

SEED = _llm.get("seed")

REPEAT_PENALTY = float(_llm.get("repeat_penalty", 1.12))
FREQUENCY_PENALTY = float(_llm.get("frequency_penalty", 0.35))



def _client(b):
    # A box that is off should fail fast so the next one gets a turn, but a
    # model that is still loading needs the full read timeout.
    return OpenAI(api_key=b["api_key"], base_url=b["base_url"],
                  timeout=Timeout(b["timeout"], connect=5.0), max_retries=0)


_clients = {b["name"]: _client(b) for b in backend.BACKENDS}
client = _clients["server"]

_down_until = {}


def describe_endpoint():
    b = backend.get(backend_current())
    return (b and b["base_url"]) or "https://api.openai.com/v1"


def active_model():
    return model_for()


def active_endpoint():
    return backend_current()


def _order():
    """Backends to try for this call, best first."""
    m = backend_mode()
    if m != "auto":
        return [m]
    now = time.time()
    up = [n for n in backend.NAMES if _down_until.get(n, 0) <= now]
    # If everything is marked down, try them all again rather than giving up.
    return up or list(backend.NAMES)


def _pick():
    which = _order()[0]
    return _clients[which], model_for(which), which


def _request_extras(kw):
    kw = dict(kw)
    if not THINKING:
        extra = dict(kw.get("extra_body") or {})
        tmpl = dict(extra.get("chat_template_kwargs") or {})
        tmpl.setdefault("enable_thinking", False)
        extra["chat_template_kwargs"] = tmpl
        kw["extra_body"] = extra
    if "seed" not in kw:
        kw["seed"] = SEED if SEED is not None else random.randrange(2**31)
    if FREQUENCY_PENALTY and "frequency_penalty" not in kw:
        kw["frequency_penalty"] = FREQUENCY_PENALTY
    if REPEAT_PENALTY and REPEAT_PENALTY != 1.0:
        extra = dict(kw.get("extra_body") or {})
        extra.setdefault("repeat_penalty", REPEAT_PENALTY)
        kw["extra_body"] = extra
    return kw


def chat_completion(messages, **kw):
    """Call the selected backend. In auto mode, fall through to the next one
    whenever a backend can't be reached."""
    from openai import APIConnectionError, APITimeoutError

    kw = _request_extras(kw)
    order = _order()
    for i, which in enumerate(order):
        try:
            out = _clients[which].chat.completions.create(
                model=model_for(which), messages=messages, **kw)
        except (APIConnectionError, APITimeoutError):
            if i == len(order) - 1:
                raise
            _down_until[which] = time.time() + STICKY
            nxt = order[i + 1]
            print(f"[llm] {backend.label(which)} unreachable, trying "
                  f"{backend.label(nxt)} ({model_for(nxt)})", flush=True)
            continue
        if _down_until.pop(which, None):
            print(f"[llm] {backend.label(which)} is back", flush=True)
        note_used(which)
        return out


def _flatten(message):
    content = message.get("content")
    if isinstance(content, str):
        return {"role": message["role"], "content": content}

    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )
        return {"role": message["role"], "content": text}

    return {"role": message["role"], "content": str(content)}


def load_history():
    path = _history_file()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
            except json.JSONDecodeError:
                return []
        history = [_flatten(m) for m in raw if isinstance(m, dict) and "role" in m]
        return [m for m in history if m["role"] != "system"]
    return []


_history_lock = threading.RLock()


SEND_WINDOW = HISTORY_TURNS * 2
RETAIN = HISTORY_TURNS * 6


def _window(history):
    msgs = [m for m in history if m.get("role") != "system"]
    if len(msgs) <= SEND_WINDOW:
        return msgs
    start = ((len(msgs) - SEND_WINDOW) // SEND_WINDOW) * SEND_WINDOW
    return msgs[start:]


def save_history(history):
    msgs = [m for m in history if m["role"] != "system"]
    if len(msgs) > RETAIN:
        msgs = msgs[-(RETAIN - SEND_WINDOW):]
    with _history_lock:
        path = _history_file()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msgs, f, indent=2)
        os.replace(tmp, path)


def _extract_backend():
    c, model, _which = _pick()
    return c, model


def save_turn(user_text, assistant_text):
    assistant_text = (assistant_text or "").strip()
    if not assistant_text:
        return
    with _history_lock:
        history = _window(load_history())
        if user_text is not None:
            history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        save_history(history)
    if _remember() and user_text is not None:
        memory_extract.remember_async(*_extract_backend(), user_text, assistant_text)


def reset_history():
    path = _history_file()
    if os.path.exists(path):
        os.remove(path)


def get_reply(messages):
    return chat_completion(
        messages,
        temperature=_temperature(),
        max_tokens=MAX_TOKENS,
        stream=False,
    )


def note_exchange(user_text, assistant_text):
    """Save a turn that didn't come from the chat model, e.g. a screen look."""
    history = _window(load_history())
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    save_history(history)


def llm_response(user_input):
    history = _window(load_history())
    history.append({"role": "user", "content": user_input})

    completion = get_reply([system_message()] + history)
    reply = (completion.choices[0].message.content or "").strip()

    history.append({"role": "assistant", "content": reply})
    save_history(history)

    if _remember():
        memory_extract.remember_async(*_extract_backend(), user_input, reply)

    return reply


def get_reply_stream(messages, **kw):
    return chat_completion(
        messages,
        temperature=_temperature(),
        max_tokens=MAX_TOKENS,
        stream=True,
        **kw,
    )


def _collect_tool_calls(delta, calls):
    for call in getattr(delta, "tool_calls", None) or []:
        slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
        if call.id:
            slot["id"] = call.id
        fn = getattr(call, "function", None)
        if fn is not None:
            if fn.name:
                slot["name"] = fn.name
            if fn.arguments:
                slot["arguments"] += fn.arguments


_tools_supported = True


def _rejects_tools(e):
    """True if the endpoint doesn't support tools (as opposed to being down)."""
    from openai import APIStatusError

    if not isinstance(e, APIStatusError):
        return False
    if e.status_code not in (400, 404, 422, 501):
        return False
    body = (str(getattr(e, "message", "")) or str(e)).lower()
    return "tool" in body or "function" in body or e.status_code in (404, 501)


def llm_stream(user_input=None, extra_system=None, use_tools=True):
    """Yield reply text as it arrives. The caller saves the turn with
    `save_turn`, since only it knows how much was actually spoken.

    `extra_system` is added to the system prompt for this call only.
    """
    history = _window(load_history())
    if user_input is not None:
        history.append({"role": "user", "content": user_input})

    system = system_message()
    if extra_system:
        system = {"role": "system", "content": system["content"] + extra_system}

    global _tools_supported

    messages = [system] + history
    offer = tools.definitions() if (use_tools and _tools_supported) else None

    rounds = _tool_rounds()
    done_rounds = 0
    while True:
        calls = {}
        said = []
        try:
            stream = (get_reply_stream(messages, tools=offer, tool_choice="auto")
                      if offer else get_reply_stream(messages))
            for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                _collect_tool_calls(delta, calls)
                piece = getattr(delta, "content", None)
                if not piece:
                    continue
                said.append(piece)
                yield piece
        except Exception as e:
            if offer is None or not _rejects_tools(e):
                raise
            print(f"[llm] endpoint rejected tool definitions "
                  f"({type(e).__name__}); continuing without them", flush=True)
            _tools_supported = False
            offer = None
            continue

        if not calls or offer is None:
            return

        messages = messages + [{
            "role": "assistant",
            "content": "".join(said) or None,
            "tool_calls": [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in sorted(calls.items())
            ],
        }]
        for i, call in sorted(calls.items()):
            result = tools.run(call["name"], call["arguments"])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"] or f"call_{i}",
                "content": result,
            })
        done_rounds += 1
        if done_rounds >= rounds:
            # Out of rounds: no more tools, so the next pass has to answer.
            offer = None
