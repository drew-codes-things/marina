"""Tools the model can call: timers, clipboard, opening links, remembering.

open_url only accepts http and https.
"""
import json
import subprocess
import threading
import time
from urllib.parse import urlparse

from process.config import load_config, setting
from process.memory import store as memory

_config = load_config()
_cfg = _config.get("tools") or {}



# Read on every call: the presentation preset turns some of these off.
def _allowed(key):
    return bool(setting("tools", key, True))

def _may_remember():
    return bool(setting("llm", "remember", True))

CLIPBOARD_LIMIT = int(_cfg.get("clipboard_chars", 2000))

_announcements = []
_lock = threading.Lock()


def pending_announcement():
    with _lock:
        return _announcements.pop(0) if _announcements else None


def _announce(text):
    with _lock:
        _announcements.append(text)


def set_timer(minutes, label=""):
    if not _allowed("timers"):
        return "Timers are switched off."
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        return "That is not a number of minutes."
    if not 0 < minutes <= 24 * 60:
        return "Timers have to be between a moment and a day."

    what = (label or "").strip()

    def fire():
        time.sleep(minutes * 60)
        _announce(f"The timer you set{f' for {what}' if what else ''} just "
                  f"went off. Tell them, in your own words, in one sentence.")

    threading.Thread(target=fire, daemon=True).start()
    pretty = f"{int(minutes)} minutes" if minutes >= 1 else f"{int(minutes * 60)} seconds"
    print(f"[tool] timer set for {pretty}{f' ({what})' if what else ''}", flush=True)
    return f"Timer set for {pretty}{f' ({what})' if what else ''}."


def read_clipboard():
    if not _allowed("clipboard"):
        return "The clipboard is off limits."
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2)
    except Exception as e:
        return f"Could not read the clipboard: {e}"
    text = (out.stdout or "").strip()
    if not text:
        return "The clipboard is empty."
    if len(text) > CLIPBOARD_LIMIT:
        text = text[:CLIPBOARD_LIMIT] + "… (truncated)"
    print(f"[tool] read clipboard ({len(text)} chars)", flush=True)
    return f"The clipboard contains:\n{text}"


def open_url(url):
    if not _allowed("open_url"):
        return "Opening links is switched off."
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Only http and https links can be opened."
    try:
        subprocess.run(["open", url], timeout=3, check=False)
    except Exception as e:
        return f"Could not open it: {e}"
    print(f"[tool] opened {url}", flush=True)
    return f"Opened {parsed.netloc}."


def remember(fact):
    fact = (fact or "").strip()
    if not fact:
        return "Nothing to remember."
    added = memory.add(fact, source="tool")
    print(f"[tool] remembered: {fact}", flush=True)
    return "Noted." if added else "Already knew that."


SPECS = [
    ("set_timer", set_timer, lambda: _allowed("timers"), {
        "description": "Set a timer that will go off after a number of minutes. "
                       "Use when they ask to be reminded of something soon.",
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "How many minutes from now."},
                "label": {"type": "string", "description": "What the timer is for."},
            },
            "required": ["minutes"],
        }}),
    ("read_clipboard", read_clipboard, lambda: _allowed("clipboard"), {
        "description": "Read what is on the clipboard. You cannot see it any "
                       "other way, so call this whenever they mention "
                       "something they copied or pasted, or refer to 'this' "
                       "error, link, message or snippet. Never guess at what "
                       "they copied.",
        "parameters": {"type": "object", "properties": {}},
        }),
    ("open_url", open_url, lambda: _allowed("open_url"), {
        "description": "Open a web link in their browser. Only http and https.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }}),
    ("remember", remember, _may_remember, {
        "description": "Write down a lasting fact about the person you are talking to, so it "
                       "survives this conversation. Only things that stay true: their name, "
                       "what they do, what they like, people and pets in their "
                       "life. Not instructions to yourself, not what is happening "
                       "right now, not your own feelings or actions.",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
        }}),
]

_HANDLERS = {name: (fn, on) for name, fn, on, _ in SPECS}

def definitions():
    if not _allowed("enabled"):
        return None
    tools = [
        {"type": "function",
         "function": {"name": name, **spec}}
        for name, _fn, on, spec in SPECS if on()
    ]
    return tools or None


def run(name, arguments):
    """Run one tool call. Errors are returned as text for the model."""
    fn, on = _HANDLERS.get(name, (None, None))
    if fn is None or not on():
        return f"There is no tool called {name}."
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return "Those arguments were not valid JSON."
    if not isinstance(args, dict):
        return "Those arguments were not an object."
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"Wrong arguments for {name}: {e}"
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}"
