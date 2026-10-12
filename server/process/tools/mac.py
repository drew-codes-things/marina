"""Their Mac: commands, files, the screen, and the mouse and keyboard.

Off unless `mac.enabled` is set, and only on a bridge running on macOS, so
the desktop app has these and Telegram and chat front ends (which run on
the server) never do.

What keeps it safe:
- Using the mouse and keyboard needs their OK once per reply, and risky
  commands (deleting, sudo, git push, piping a download into a shell, ...)
  need it every time. The app asks, with Allow and Deny.
- Secrets are out of reach: ~/.ssh, keychains, password managers, and the
  apps that hold them, can't be read, written, opened or typed into.
- Files are read and written inside their home folder only.

macOS has to allow it too: Screen Recording to see the screen and
Accessibility to use the mouse and keyboard, both for Marina, in System
Settings > Privacy & Security.
"""
import base64
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from process.config import setting

HOME = Path.home()
OUTPUT_CHARS = 6000
READ_LINES = 400
TIMEOUT = 120

# Never read, written, opened or typed into, whatever she is asked.
_SECRET_PATHS = [HOME / ".ssh", HOME / ".gnupg", HOME / "Library" / "Keychains",
                 HOME / "Library" / "Group Containers" / "2BUA8C4S2C.com.1password",
                 HOME / "Library" / "Application Support" / "1Password",
                 HOME / "Library" / "Application Support" / "Bitwarden",
                 HOME / "Library" / "Cookies", HOME / ".aws", HOME / ".config" / "gh"]
_SECRET_APPS = re.compile(r"keychain|1password|bitwarden|lastpass|dashlane|\bpasswords\b|keeper", re.I)
_SECRET_COMMANDS = re.compile(
    r"\bsecurity\s+(?:find-|dump-|export|unlock)|\.ssh/|Keychains|1password|bitwarden"
    r"|\bop\s+(?:item|read)\b", re.I)

# Asked about every time.
_RISKY = re.compile(
    r"\b(?:rm|rmdir|srm|sudo|su|chmod|chown|kill|killall|pkill|shutdown|reboot|halt"
    r"|diskutil|dd|mkfs|newfs|launchctl|osascript|csrutil|spctl|tmutil|mv|truncate"
    r"|scp|rsync|ssh|sftp|ftp)\b"
    r"|\bdefaults\s+(?:write|delete)\b|\bfind\b.*\s-delete\b|\bxargs\s+rm\b"
    r"|\bgit\s+(?:push|reset\s+--hard|clean|branch\s+-D|rebase|checkout\s+--|restore|stash\s+drop)"
    r"|\bbrew\s+(?:uninstall|remove|untap)\b|\bnpm\s+(?:publish|unpublish)\b|\bpip\d?\s+uninstall\b"
    r"|\b(?:curl|wget)\b[^|]*\|\s*(?:ba|z|)sh\b|>\s*/(?:etc|usr|System|Library)\b", re.I)

GUI_TOOLS = {"mac_click", "mac_type", "mac_key", "mac_scroll"}


def available():
    return sys.platform == "darwin" and bool(setting("mac", "enabled", False))


def _clip(text):
    text = (text or "").rstrip()
    if len(text) <= OUTPUT_CHARS:
        return text
    half = OUTPUT_CHARS // 2
    return f"{text[:half]}\n… ({len(text) - OUTPUT_CHARS} characters cut) …\n{text[-half:]}"


def _path(path, must_exist=False):
    """A path inside their home folder, or raise."""
    p = Path(os.path.expanduser((path or "").strip() or "~"))
    if not p.is_absolute():
        p = HOME / p
    p = p.resolve()
    if p != HOME and HOME not in p.parents:
        raise ValueError(f"{p} is outside their home folder")
    for secret in _SECRET_PATHS:
        if p == secret or secret in p.parents:
            raise ValueError("that's where their passwords and keys live; it's off limits")
    if must_exist and not p.exists():
        raise ValueError(f"no such file: {p}")
    return p


# --- commands and files ------------------------------------------------------

def mac_run_command(command, cwd="", timeout_seconds=None):
    command = (command or "").strip()
    if not command:
        return "No command given."
    if _SECRET_COMMANDS.search(command):
        return "Not running that: it reaches into their passwords or keys, which are off limits."
    try:
        where = _path(cwd or "~")
        limit = min(float(timeout_seconds or TIMEOUT), 1800.0)
    except (ValueError, TypeError) as e:
        return str(e)
    print(f"[mac] $ {command}", flush=True)
    proc = subprocess.Popen(["/bin/zsh", "-lc", command], cwd=where, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=limit)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        code = 124
        out = (out or "") + f"\n(stopped after {int(limit)} seconds)"
    return f"exit code {code}\n{_clip(out) or '(no output)'}"


def mac_read_file(path, start_line=1, end_line=None):
    try:
        p = _path(path, must_exist=True)
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else start + READ_LINES - 1
        lines = p.read_text(errors="replace").splitlines()
    except (ValueError, TypeError, OSError) as e:
        return str(e)
    body = "\n".join(f"{n:5d}  {line}" for n, line in enumerate(lines[start - 1:end], start))
    return _clip(f"({len(lines)} lines)\n{body}")


def mac_write_file(path, content):
    try:
        p = _path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else str(content or ""))
    except (ValueError, OSError) as e:
        return f"Could not write {path}: {e}"
    print(f"[mac] wrote {p}", flush=True)
    return f"Wrote {p} ({len(p.read_text().splitlines())} lines)."


def mac_edit_file(path, old_text, new_text):
    try:
        p = _path(path, must_exist=True)
        current = p.read_text()
    except (ValueError, OSError) as e:
        return str(e)
    count = current.count(old_text or "")
    if not old_text or count == 0:
        return "old_text was not found in the file. Read it again and copy the text exactly."
    if count > 1:
        return f"old_text appears {count} times. Include more surrounding lines so it is unique."
    p.write_text(current.replace(old_text, new_text or "", 1))
    print(f"[mac] edited {p}", flush=True)
    return f"Edited {p}."


def mac_list_files(path="", depth=2):
    try:
        root = _path(path or "~", must_exist=True)
        depth = max(1, min(int(depth or 2), 4))
    except (ValueError, TypeError) as e:
        return str(e)
    skip = {".git", "node_modules", ".venv", "Library", ".Trash", "__pycache__"}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= depth:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for name in sorted(dirnames) + sorted(f for f in filenames if not f.startswith(".")):
            out.append(str(rel / name))
        if len(out) > 300:
            break
    return _clip("\n".join(out[:300])) or "(empty)"


# --- the screen ----------------------------------------------------------------

def _cg():
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    return cg


def can_see():
    try:
        return bool(_cg().CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def can_control():
    try:
        ax = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return False


def ask_for_permissions():
    """Make macOS show its permission prompts, where it still will."""
    try:
        _cg().CGRequestScreenCaptureAccess()
    except Exception:
        pass
    try:
        ax = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                          ctypes.c_void_p, ctypes.c_void_p]
        prompt_key = ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt")
        true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(prompt_key.value)
        values = (ctypes.c_void_p * 1)(true.value)
        options = cf.CFDictionaryCreate(None, keys, values, 1, None, None)
        ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        ax.AXIsProcessTrustedWithOptions(options)
    except Exception:
        pass
    return {"screen": can_see(), "control": can_control()}


_NO_SCREEN = ("You can't see their screen: macOS hasn't given Marina Screen Recording. "
              "Ask them to allow it in System Settings > Privacy & Security > Screen "
              "Recording, then restart Marina.")
_NO_CONTROL = ("You can't use their mouse and keyboard: macOS hasn't given Marina "
               "Accessibility. Ask them to allow it in System Settings > Privacy & "
               "Security > Accessibility.")


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


def _screen_points():
    cg = _cg()
    cg.CGDisplayBounds.restype = _CGRect
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    r = cg.CGDisplayBounds(cg.CGMainDisplayID())
    return r.w, r.h


def _screenshot():
    """(base64 JPEG, image width, image height) of the main display."""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", "-m", "-t", "jpg", path], check=True, timeout=15)
        subprocess.run(["sips", "-Z", "1600", "-s", "formatOptions", "75", path],
                       capture_output=True, timeout=15)
        size = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                              capture_output=True, text=True, timeout=15).stdout
        w = int(re.search(r"pixelWidth: (\d+)", size).group(1))
        h = int(re.search(r"pixelHeight: (\d+)", size).group(1))
        return base64.b64encode(Path(path).read_bytes()).decode(), w, h
    finally:
        Path(path).unlink(missing_ok=True)


def _vision(prompt, image, max_tokens=300):
    from process.llm_funcs import llm_scr  # it imports this module

    out = llm_scr.chat_completion(
        [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]}],
        temperature=0, max_tokens=max_tokens)
    return (out.choices[0].message.content or "").strip()


LOOK_PROMPT = """This is a screenshot of their Mac's screen (your own avatar may be in
a corner; ignore it). Describe what is on it plainly and accurately, for
your own use: which app is in front, what windows are open and what is in
them, and any text that matters to what they asked. Don't read out
passwords, codes or private messages in detail. Plain sentences, no
markdown, under 150 words.{question}"""


def mac_look(question=""):
    if not can_see():
        return _NO_SCREEN
    try:
        image, _w, _h = _screenshot()
        ask = f"\n\nIn particular: {question.strip()}" if (question or "").strip() else ""
        reply = _vision(LOOK_PROMPT.format(question=ask), image)
    except Exception as e:
        return f"Looking failed: {type(e).__name__}: {str(e)[:200]}"
    print("[mac] looked at the screen", flush=True)
    return reply or "You couldn't make anything out."


def _locate(target):
    """Screen point (x, y) of something described in words, or raise."""
    image, w, h = _screenshot()
    ask = (f"Find {target} on this screenshot. Reply with only JSON "
           '{"x": X, "y": Y} for its centre, as thousandths of the image width and '
           'height (0-1000), or {"x": null, "y": null} if it is not on the screen.')
    raw = _vision(ask, image, max_tokens=40)
    m = re.search(r"\{.*\}", raw, re.S)
    spot = json.loads(m.group(0)) if m else {}
    if spot.get("x") is None or spot.get("y") is None:
        raise LookupError(f"couldn't find {target} on the screen")
    sw, sh = _screen_points()
    return float(spot["x"]) / 1000 * sw, float(spot["y"]) / 1000 * sh


# --- the mouse and keyboard --------------------------------------------------

class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _events():
    cg = _cg()
    cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                   ctypes.POINTER(ctypes.c_uint16)]
    cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
    cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p
    cg.CGEventCreateScrollWheelEvent2.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                  ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    def post(event):
        cg.CGEventPost(0, event)  # kCGHIDEventTap
        cf.CFRelease(event)

    return cg, post


_KEYS = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39,
    "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48,
    "space": 49, "`": 50, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    **{f"f{i}": c for i, c in enumerate([122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111], 1)},
}
_MODIFIERS = {"shift": 0x20000, "ctrl": 0x40000, "control": 0x40000, "alt": 0x80000,
              "option": 0x80000, "opt": 0x80000, "cmd": 0x100000, "command": 0x100000}


def _frontmost():
    return subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to get name of first '
                            'application process whose frontmost is true'],
        capture_output=True, text=True, timeout=10).stdout.strip()


def _guard_front():
    front = _frontmost()
    if front and _SECRET_APPS.search(front):
        raise PermissionError(f"{front} is in front, and that's where their passwords live")


def mac_click(target="", x=None, y=None, button="left", double=False):
    if not can_control():
        return _NO_CONTROL
    try:
        _guard_front()
        if x is not None and y is not None:
            px, py = float(x), float(y)
            what = f"({int(px)}, {int(py)})"
        elif (target or "").strip():
            if not can_see():
                return _NO_SCREEN
            px, py = _locate(target.strip())
            what = f"{target.strip()} at ({int(px)}, {int(py)})"
        else:
            return "Say what to click (target), or give x and y in screen points."
        cg, post = _events()
        down, up = (3, 4) if button == "right" else (1, 2)
        btn = 1 if button == "right" else 0
        point = _CGPoint(px, py)
        post(cg.CGEventCreateMouseEvent(None, 5, point, 0))  # move there first
        for click in range(2 if double else 1):
            for kind in (down, up):
                event = cg.CGEventCreateMouseEvent(None, kind, point, btn)
                cg.CGEventSetIntegerValueField(event, 1, click + 1)  # click state
                post(event)
            time.sleep(0.05)
    except (PermissionError, LookupError) as e:
        return str(e)
    except Exception as e:
        return f"Clicking failed: {type(e).__name__}: {e}"
    print(f"[mac] clicked {what}", flush=True)
    return f"Clicked {what}. Look at the screen to check it did what you meant."


def mac_type(text):
    if not can_control():
        return _NO_CONTROL
    text = text if isinstance(text, str) else str(text or "")
    if not text:
        return "Nothing to type."
    try:
        _guard_front()
        cg, post = _events()
        for i in range(0, len(text), 20):
            chunk = text[i:i + 20]
            units = chunk.encode("utf-16-le")
            buf = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
            for pressed in (True, False):
                event = cg.CGEventCreateKeyboardEvent(None, 0, pressed)
                cg.CGEventKeyboardSetUnicodeString(event, len(buf), buf)
                post(event)
            time.sleep(0.01)
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return f"Typing failed: {type(e).__name__}: {e}"
    print(f"[mac] typed {len(text)} characters", flush=True)
    return f"Typed {len(text)} characters into {_frontmost() or 'the app in front'}."


def mac_key(keys):
    """A key or a combination like cmd+s, shift+tab, return."""
    if not can_control():
        return _NO_CONTROL
    parts = [p.strip().lower() for p in (keys or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        return "No keys given."
    *mods, key = parts
    if key not in _KEYS or any(m not in _MODIFIERS for m in mods):
        return (f"Don't know {keys!r}. Use modifiers {', '.join(sorted(set(_MODIFIERS)))} "
                "with a letter, digit, or return, tab, space, escape, delete, arrows, f1-f12.")
    try:
        _guard_front()
        cg, post = _events()
        flags = 0
        for m in mods:
            flags |= _MODIFIERS[m]
        for pressed in (True, False):
            event = cg.CGEventCreateKeyboardEvent(None, _KEYS[key], pressed)
            cg.CGEventSetFlags(event, flags)
            post(event)
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return f"Pressing keys failed: {type(e).__name__}: {e}"
    print(f"[mac] pressed {keys}", flush=True)
    return f"Pressed {keys}."


def mac_scroll(direction="down", amount=5):
    if not can_control():
        return _NO_CONTROL
    try:
        amount = max(1, min(int(amount or 5), 50))
    except (TypeError, ValueError):
        amount = 5
    dy = {"up": amount, "down": -amount}.get(direction, 0)
    dx = {"left": amount, "right": -amount}.get(direction, 0)
    if not dx and not dy:
        return "direction must be up, down, left or right."
    try:
        cg, post = _events()
        post(cg.CGEventCreateScrollWheelEvent2(None, 0, 2, dy, dx, 0))  # line units
    except Exception as e:
        return f"Scrolling failed: {type(e).__name__}: {e}"
    return f"Scrolled {direction}."


# --- apps ----------------------------------------------------------------------

_APPS_SCRIPT = """
on joined(itemList)
    set AppleScript's text item delimiters to ", "
    return itemList as text
end joined
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set openApps to name of every application process whose background only is false
    set frontWindows to name of every window of (first application process whose frontmost is true)
end tell
return frontApp & linefeed & my joined(openApps) & linefeed & my joined(frontWindows)
"""


def mac_apps():
    out = subprocess.run(["osascript", "-e", _APPS_SCRIPT], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        return ("Couldn't list their apps: " + out.stderr.strip()[:200]
                + " (macOS may be asking them to let Marina control System Events.)")
    front, apps, wins = (out.stdout.strip().split("\n") + ["", "", ""])[:3]
    return f"In front: {front}\nWindows of {front}: {wins or '(none)'}\nOpen apps: {apps}"


def mac_open(app="", url=""):
    if url:
        if not re.match(r"^https?://", url):
            return "Only http and https links."
        subprocess.run(["open", url], timeout=10)
        return f"Opened {url}."
    app = (app or "").strip()
    if not app:
        return "Say which app."
    if _SECRET_APPS.search(app):
        return f"{app} holds their passwords, so it's off limits."
    out = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        return f"Couldn't open {app}: {out.stderr.strip()[:200]}"
    print(f"[mac] opened {app}", flush=True)
    return f"{app} is open and in front."


# --- what the app shows and asks ----------------------------------------------

def describe(name, args):
    def p():
        return args.get("path") or "home"
    if name == "mac_run_command":
        line = (args.get("command") or "").strip().splitlines()
        return f"running `{line[0][:70]}`" if line else "running a command"
    if name == "mac_click":
        return f"clicking {args.get('target') or 'on the screen'}"
    if name == "mac_type":
        text = args.get("text") or ""
        return f"typing “{text[:40]}{'…' if len(text) > 40 else ''}”"
    if name == "mac_key":
        return f"pressing {args.get('keys', '')}"
    if name == "mac_open":
        return f"opening {args.get('app') or args.get('url') or 'an app'}"
    return {
        "mac_read_file": f"reading {p()}", "mac_write_file": f"writing {p()}",
        "mac_edit_file": f"editing {p()}", "mac_list_files": f"looking through {p()}",
        "mac_look": "looking at your screen", "mac_apps": "checking what's open",
        "mac_scroll": f"scrolling {args.get('direction', 'down')}",
    }.get(name, name)


def consent_key(name):
    return "gui" if name in GUI_TOOLS else name


def needs_ok(name, args, consented):
    """The question to ask them first, or None."""
    if name in GUI_TOOLS:
        return None if "gui" in consented else "Let her use your mouse and keyboard for this?"
    if name == "mac_run_command":
        command = (args.get("command") or "").strip()
        if _RISKY.search(command):
            return f"Let her run this on your Mac?\n{command[:300]}"
    if name in ("mac_write_file", "mac_edit_file"):
        try:
            p = _path(args.get("path", ""))
        except ValueError:
            return None  # the tool will refuse it anyway
        if name == "mac_write_file" and p.exists():
            return f"Let her overwrite {p}?"
    return None


SPECS = [
    ("mac_look", mac_look, {
        "description": "Take a screenshot of their Mac's screen and look at it. Use it "
                       "before working in one of their apps, and after doing something "
                       "to check it worked. You can't see their screen otherwise.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What you want to check."}}}}),
    ("mac_apps", mac_apps, {
        "description": "List the apps open on their Mac, which is in front, and its windows.",
        "parameters": {"type": "object", "properties": {}}}),
    ("mac_open", mac_open, {
        "description": "Open or bring to the front an app on their Mac (by name, e.g. "
                       "\"Safari\", \"Visual Studio Code\"), or open a web link.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}, "url": {"type": "string"}}}}),
    ("mac_click", mac_click, {
        "description": "Click on their Mac's screen. Describe what to click in target "
                       "(e.g. \"the Save button in the dialog\") and it is found on a fresh "
                       "screenshot, or give x and y in screen points.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
            "button": {"type": "string", "enum": ["left", "right"]},
            "double": {"type": "boolean"}}}}),
    ("mac_type", mac_type, {
        "description": "Type text into whatever has focus on their Mac. Click the field first.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}}),
    ("mac_key", mac_key, {
        "description": "Press a key or shortcut on their Mac: e.g. return, tab, escape, "
                       "cmd+s, cmd+shift+t, cmd+tab, up.",
        "parameters": {"type": "object", "properties": {"keys": {"type": "string"}},
                       "required": ["keys"]}}),
    ("mac_scroll", mac_scroll, {
        "description": "Scroll whatever is under the mouse on their Mac.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer"}}}}),
    ("mac_run_command", mac_run_command, {
        "description": "Run a zsh command on their Mac, in their home folder unless you "
                       "give cwd. Risky commands ask them first.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"}}, "required": ["command"]}}),
    ("mac_read_file", mac_read_file, {
        "description": "Read a file on their Mac (inside their home folder) with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}}, "required": ["path"]}}),
    ("mac_write_file", mac_write_file, {
        "description": "Create a file on their Mac, or overwrite one (which asks them first).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}),
    ("mac_edit_file", mac_edit_file, {
        "description": "Change part of a file on their Mac: replace old_text, which must "
                       "appear exactly once, with new_text.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"},
            "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}),
    ("mac_list_files", mac_list_files, {
        "description": "List files under a folder on their Mac.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "depth": {"type": "integer"}}}}),
]

PROMPT = """

You can also work on their Mac, the computer they are using right now:
mac_look to see the screen, mac_apps to see what's open, mac_open, mac_click,
mac_type, mac_key and mac_scroll to use it, and mac_run_command and the
mac_*_file tools for commands and files. Their Mac is theirs, not yours: be
careful with it, do what they asked and nothing more. Look before you act
in an app and look again afterwards to check. The first time in a reply you
use their mouse or keyboard, and before anything risky, the app asks them;
if they say no, don't look for a way round it. Until they've answered,
don't talk as if it's happening: say "I'll need your OK to delete that",
not "Deleting that now", and only say it's done once the tool says so.
Their passwords, keys and
password managers are off limits. Say which computer you mean: "my
machine" is yours, "your Mac" is theirs."""
