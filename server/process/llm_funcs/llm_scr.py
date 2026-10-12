"""Chat completions client with on-disk history.

Works with anything OpenAI-compatible (OpenAI, Ollama, llama.cpp, LM Studio,
vLLM). Which endpoints exist and in what order is up to process.backend.
"""
import json
import os
import random
import re
import threading
import time

from openai import OpenAI, Timeout

from process import backend

from process.backend import current as backend_current
from process.backend import mode as backend_mode
from process.backend import model_for
from process.backend import note_used
from process import config
from process import shared
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
                    + _stable_context()
                    + (ambient.NOTE_EXPLAINED if ambient.ENABLED else "")),
    }


def _tool_rounds():
    """How many rounds of tool calls one reply may take before she has to
    answer. Work on her machine is a chain of them: run, read, fix, rerun.
    0 means no limit."""
    if not tools.wants_long_loop():
        return 1
    rounds = int(config.setting("llm", "max_tool_rounds", 0) or 0)
    return float("inf") if rounds <= 0 else rounds


# The same call this many times running means she is stuck, not working.
STUCK_REPEATS = 3

# A reply that ends by promising to do something, when she could have.
_PROMISE = re.compile(
    r"\b(i'?ll|i will|let me|let'?s|i'?m (?:going|gonna) to|gonna|about to)\b",
    re.I)


def _promised(text):
    """True if the last sentence of a reply says she is about to do something."""
    tail = re.split(r"(?<=[.!?])\s+", (text or "").strip())[-1]
    return bool(_PROMISE.search(tail.replace("\u2019", "'")))


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


def _shared_history():
    """Only the personal conversation is shared. A presentation keeps its
    own, so guests never see what you texted her."""
    return shared.enabled() and config.preset() == "default"


def load_history():
    """The conversation, from the shared home if there is one."""
    if _shared_history():
        msgs = shared.call("GET", "/shared/history")
        if isinstance(msgs, list):
            history = [_flatten(m) for m in msgs if isinstance(m, dict) and "role" in m]
            save_history(history)  # the copy used if the home is down
            return [m for m in history if m["role"] != "system"]
    return load_local_history()


def load_local_history():
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


def append_local(messages):
    """Add messages to this bridge's own conversation file."""
    with _history_lock:
        history = _window(load_local_history())
        history.extend(messages)
        save_history(history)


def append_history(messages):
    """Add messages to the conversation. Appending, rather than saving the
    whole thing, is what lets the desktop and Telegram both write to the
    shared one without either losing what the other just said."""
    if _shared_history():
        if shared.call("POST", "/shared/history", {"messages": messages}) is not None:
            return
    append_local(messages)


def save_turn(user_text, assistant_text):
    assistant_text = (assistant_text or "").strip()
    if not assistant_text:
        return
    turn = [] if user_text is None else [{"role": "user", "content": user_text}]
    append_history(turn + [{"role": "assistant", "content": assistant_text}])
    if _remember() and user_text is not None:
        memory_extract.remember_async(*_extract_backend(), user_text, assistant_text)


def reset_history():
    if _shared_history():
        shared.call("DELETE", "/shared/history")
    reset_local_history()


def reset_local_history():
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
    append_history([{"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text}])


def llm_response(user_input):
    history = _window(load_history())
    history.append({"role": "user", "content": user_input})

    completion = get_reply([system_message()] + history)
    reply = (completion.choices[0].message.content or "").strip()

    append_history([{"role": "user", "content": user_input},
                    {"role": "assistant", "content": reply}])

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


def llm_stream(user_input=None, extra_system=None, use_tools=True, history=None):
    """Yield reply text as it arrives. The caller saves the turn with
    `save_turn`, since only it knows how much was actually spoken.

    `extra_system` is added to the system prompt for this call only.
    `history` replaces her own conversation, for a chat that keeps its own
    threads, like Open WebUI.
    """
    history = _window(load_history()) if history is None else list(history)
    if user_input is not None:
        # The clock goes on this message only, never into the saved history
        # or the system prompt, so the cached prefix stays the same.
        note = ambient.as_note()
        if isinstance(user_input, list):  # text and images, from a chat UI
            content = user_input + ([{"type": "text", "text": note.strip()}] if note else [])
        else:
            content = user_input + note
        history.append({"role": "user", "content": content})

    system = system_message()
    if extra_system:
        system = {"role": "system", "content": system["content"] + extra_system}

    global _tools_supported

    messages = [system] + history
    offer = tools.definitions() if (use_tools and _tools_supported) else None

    rounds = _tool_rounds()
    done_rounds = 0
    last_calls = []
    nudged = False
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

        if not calls and offer is not None and not nudged and tools.wants_long_loop() \
                and _promised("".join(said)):
            # She said what she was about to do and stopped. Hold her to it
            # once, rather than leaving them with a plan and no work.
            nudged = True
            print("[llm] she described a plan without doing it; nudging", flush=True)
            yield "\n\n"
            messages = messages + [
                {"role": "assistant", "content": "".join(said)},
                {"role": "user", "content": "(go ahead and do it now, with your tools)"},
            ]
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
        # Nothing to say, but a chance for the caller to notice it was hung
        # up on; without it a long run of tool calls never yields, and she
        # would carry on working for nobody.
        yield ""
        signature = json.dumps([(c["name"], c["arguments"]) for _, c in sorted(calls.items())])
        last_calls = (last_calls + [signature])[-STUCK_REPEATS:]
        stuck = len(last_calls) == STUCK_REPEATS and len(set(last_calls)) == 1
        if stuck:
            print(f"[llm] same tool call {STUCK_REPEATS} times running; "
                  "making her answer", flush=True)
        if done_rounds >= rounds or stuck:
            # No more tools, so the next pass has to answer.
            offer = None
