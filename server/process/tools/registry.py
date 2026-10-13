"""Tools the model can call: timers, clipboard, opening links, remembering,
and her workspace machine (see process.tools.workspace).

open_url only accepts http and https. The clipboard and open_url need a desktop
session (a Mac, or Linux with wl-clipboard, xclip or xsel and xdg-open), so a
headless bridge leaves them out.
"""
import json
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from process.config import load_config, setting
from process.memory import store as memory
from process.tools import computer
from process.tools import mac
from process.tools import session
from process.tools import workspace

_config = load_config()
_cfg = _config.get("tools") or {}



# Read on every call: the presentation preset turns some of these off.
def _allowed(key):
    return bool(setting("tools", key, True))


_ON_MAC = sys.platform == "darwin"


def _linux_clipboard_command():
    """The command that prints the clipboard on this desktop, or None."""
    readers = [(["wl-paste", "--no-newline"], "wl-paste"),
               (["xclip", "-selection", "clipboard", "-o"], "xclip"),
               (["xsel", "--clipboard", "--output"], "xsel")]
    if not session.kind():
        return None
    if session.kind() == "x11":
        readers.append(readers.pop(0))
    return next((command for command, tool in readers if session.have(tool)), None)


def _clipboard_available():
    if _ON_MAC:
        return _allowed("clipboard")
    return _allowed("clipboard") and _linux_clipboard_command() is not None


def _open_url_available():
    if _ON_MAC:
        return _allowed("open_url")
    return _allowed("open_url") and bool(session.kind()) and session.have("xdg-open")


def _workspace_allowed():
    return workspace.available() and _allowed("workspace")


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
    command = ["pbpaste"] if _ON_MAC else _linux_clipboard_command()
    if command is None:
        return "No clipboard tool is installed (wl-clipboard on Wayland, xclip or xsel on X11)."
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=2)
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
    if _ON_MAC:
        try:
            subprocess.run(["open", url], timeout=3, check=False)
        except Exception as e:
            return f"Could not open it: {e}"
    elif not session.spawn(["xdg-open", url]):
        return "Could not open it: xdg-open failed."
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
    ("read_clipboard", read_clipboard, _clipboard_available, {
        "description": "Read what is on the clipboard. You cannot see it any "
                       "other way, so call this whenever they mention "
                       "something they copied or pasted, or refer to 'this' "
                       "error, link, message or snippet. Never guess at what "
                       "they copied.",
        "parameters": {"type": "object", "properties": {}},
        }),
    ("open_url", open_url, _open_url_available, {
        "description": "Open a web link in their browser. Only http and https."
                       + ("" if _ON_MAC else " If they name a browser, use computer_open with that "
                          "app and the url instead."),
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

SPECS += [(name, fn, _workspace_allowed, spec) for name, fn, spec in workspace.SPECS]


_DESKTOP = mac if _ON_MAC else computer
_DESKTOP_KEY = "mac" if _ON_MAC else "computer"


def _desktop_allowed():
    return _DESKTOP.available() and _allowed(_DESKTOP_KEY)


SPECS += [(name, fn, _desktop_allowed, spec) for name, fn, spec in _DESKTOP.SPECS]
_DESKTOP_TOOLS = {name for name, *_ in _DESKTOP.SPECS}

_HANDLERS = {name: (fn, on) for name, fn, on, _ in SPECS}


def _args(arguments):
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


_WORKSPACE_TOOLS = {name for name, *_ in workspace.SPECS}


def describe(name, arguments):
    """(label, where) for a tool call the app should show, else (None, None).
    where is "vm" for her machine, "mac" or "computer" for theirs."""
    args = _args(arguments)
    if name in _WORKSPACE_TOOLS:
        return workspace.describe(name, args), "vm"
    if name in _DESKTOP_TOOLS:
        return _DESKTOP.describe(name, args), _DESKTOP_KEY
    return None, None


def needs_ok(name, arguments, consented):
    """A question to put to them before running this call, or None."""
    if name in _DESKTOP_TOOLS:
        return _DESKTOP.needs_ok(name, _args(arguments), consented)
    return None


def consent_key(name):
    return _DESKTOP.consent_key(name) if name in _DESKTOP_TOOLS else name


def prompt_addition():
    """What she needs to know about tools that the definitions don't say."""
    if not _allowed("enabled"):
        return ""
    machines = ((workspace.PROMPT if _workspace_allowed() else "")
                + (_DESKTOP.PROMPT if _desktop_allowed() else ""))
    return machines + (WORKING if machines else "")


WORKING = """

While you're working on a machine, between one tool call and the next, you
don't need to perform: say nothing, or a few plain words about what you're
doing. Save your voice and personality for when you're done and talk to
them about how it went, which should sound like you."""


def wants_long_loop():
    """Work on a machine takes many calls in a row; the rest take one."""
    return _allowed("enabled") and (_workspace_allowed() or _desktop_allowed())


def definitions():
    if not _allowed("enabled"):
        return None
    tools = [
        {"type": "function",
         "function": {"name": name, **spec}}
        for name, _fn, on, spec in SPECS if on()
    ]
    return tools or None


_GARBLED = re.compile(r"<tool_call>|</?function[=>]|</?parameter[=>]")


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
    if any(isinstance(v, str) and _GARBLED.search(v) for v in args.values()):
        # The model wrote its next call (or several) inside this one's
        # arguments. Running it would open "LibreOffice <tool_call>..." or
        # type the markup into their document.
        return (f"Didn't run {name}: its arguments had another tool call written inside "
                "them. Make one tool call at a time, with only the plain value in each "
                "argument.")
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"Wrong arguments for {name}: {e}"
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}"
