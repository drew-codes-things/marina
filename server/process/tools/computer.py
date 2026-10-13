"""Their computer: commands, files, the screen, and the mouse and keyboard.

Off unless `computer.enabled` is set, so the desktop app has these and
Telegram and chat front ends (which run on the server) never do.

What keeps it safe:
- Using the mouse and keyboard needs their OK once per reply, and risky
  commands (deleting, sudo, git push, piping a download into a shell, ...)
  need it every time. The app asks, with Allow and Deny.
- Secrets are out of reach: ~/.ssh, keyrings, password managers, and the
  apps that hold them, can't be read, written, opened or typed into.
- Files are read and written inside their home folder only.

What it needs from the desktop:
- Seeing the screen: a screenshot tool (spectacle, gnome-screenshot, grim,
  scrot or maim) and Pillow.
- The mouse and keyboard: xdotool on X11, or ydotool on Wayland (with its
  ydotoold daemon running). Wayland won't tell a program which window is in
  front, so there the check that keeps her out of password managers can only
  work with kdotool on KDE; elsewhere typing is on the OK they give.
"""
import base64
import html
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from process.config import setting
from process.tools import portal
from process.tools import session

HOME = Path.home()
OUTPUT_CHARS = 6000
READ_LINES = 400
TIMEOUT = 120
MAX_EDGE = 1600

_SECRET_PATHS = [HOME / ".ssh", HOME / ".gnupg", HOME / ".aws", HOME / ".config" / "gh",
                 HOME / ".password-store",
                 HOME / ".local" / "share" / "keyrings", HOME / ".local" / "share" / "kwalletd",
                 HOME / ".config" / "1Password", HOME / ".config" / "Bitwarden",
                 HOME / ".config" / "keepassxc",
                 HOME / ".mozilla", HOME / ".config" / "google-chrome",
                 HOME / ".config" / "chromium", HOME / ".config" / "BraveSoftware"]
_SECRET_APPS = re.compile(
    r"keychain|keyring|kwallet|seahorse|1password|bitwarden|lastpass|dashlane|keepass"
    r"|enpass|\bpasswords\b|keeper"
    r"|polkit|pinentry|authentication required|gcr-prompter", re.I)
_SECRET_COMMANDS = re.compile(
    r"\bsecret-tool\b|\bgpg2?\b[^|;&]*--(?:export|decrypt)|\.ssh\b|\.gnupg\b|\.password-store"
    r"|\bkeyrings?\b|kwallet|keepassxc|1password|bitwarden|\bbw\s+(?:get|list|unlock)\b"
    r"|\bop\s+(?:item|read)\b", re.I)

_DRIVES_INPUT = re.compile(
    r"(?:^|[;&|(`]|\bsudo|\bexec|\btimeout\s+\d+)\s*(?:\S*/)?"
    r"(?:xdotool|ydotool|wtype|dotool|xte|evemu-\w+)\b"
    r"|\bRemoteDesktop\b|\bNotify(?:Keyboard|Pointer)\w*\b", re.I)

_RISKY = re.compile(
    r"\b(?:rm|rmdir|shred|sudo|su|doas|chmod|chown|chgrp|kill|killall|pkill|shutdown|reboot"
    r"|halt|poweroff|dd|mkfs(?:\.\w+)?|fdisk|parted|wipefs|mount|umount|crontab|iptables"
    r"|nft|ufw|mv|truncate|scp|rsync|ssh|sftp|ftp)\b"
    r"|\bsystemctl\s+(?:stop|restart|disable|mask|kill|poweroff|reboot|isolate)\b"
    r"|\b(?:gsettings|dconf)\s+(?:set|reset|write)\b|\bkwriteconfig\d*\b"
    r"|\bfind\b.*\s-delete\b|\bxargs\s+rm\b"
    r"|\bgit\s+(?:push|reset\s+--hard|clean|branch\s+-D|rebase|checkout\s+--|restore|stash\s+drop)"
    r"|\b(?:apt|apt-get|dnf|yum|zypper|pacman|snap|flatpak)\s+(?:-\S+\s+)*"
    r"(?:remove|purge|autoremove|erase|uninstall|-R\S*|-r)\b"
    r"|\bdpkg\s+(?:-\S+\s+)*(?:-r|-P|--remove|--purge)\b"
    r"|\bnpm\s+(?:publish|unpublish)\b|\bpip\d?\s+uninstall\b"
    r"|\b(?:curl|wget)\b[^|]*\|\s*(?:ba|z|)sh\b|>\s*/(?:etc|usr|boot|bin|sbin|lib|var)\b", re.I)

GUI_TOOLS = {"computer_click", "computer_type", "computer_key", "computer_scroll"}


def available():
    return bool(setting("computer", "enabled", False))


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



def computer_run_command(command, cwd="", timeout_seconds=None):
    command = (command or "").strip()
    if not command:
        return "No command given."
    if _SECRET_COMMANDS.search(command):
        return "Not running that: it reaches into their passwords or keys, which are off limits."
    if _DRIVES_INPUT.search(command):
        return ("Not running that: it works their mouse or keyboard behind the checks. Use "
                "computer_type, computer_key and computer_click, which look at what's in "
                "front first, and if those aren't working, tell them.")
    try:
        where = _path(cwd or "~")
        limit = min(float(timeout_seconds or TIMEOUT), 1800.0)
    except (ValueError, TypeError) as e:
        return str(e)
    print(f"[computer] $ {command}", flush=True)
    proc = subprocess.Popen(["/bin/bash", "-lc", command], cwd=where, stdin=subprocess.DEVNULL,
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


def computer_read_file(path, start_line=1, end_line=None):
    try:
        p = _path(path, must_exist=True)
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else start + READ_LINES - 1
        lines = p.read_text(errors="replace").splitlines()
    except (ValueError, TypeError, OSError) as e:
        return str(e)
    body = "\n".join(f"{n:5d}  {line}" for n, line in enumerate(lines[start - 1:end], start))
    return _clip(f"({len(lines)} lines)\n{body}")


def computer_write_file(path, content):
    try:
        p = _path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else str(content or ""))
    except (ValueError, OSError) as e:
        return f"Could not write {path}: {e}"
    print(f"[computer] wrote {p}", flush=True)
    return f"Wrote {p} ({len(p.read_text().splitlines())} lines)."


def computer_edit_file(path, old_text, new_text):
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
    print(f"[computer] edited {p}", flush=True)
    return f"Edited {p}."


def computer_list_files(path="", depth=2):
    try:
        root = _path(path or "~", must_exist=True)
        depth = max(1, min(int(depth or 2), 4))
    except (ValueError, TypeError) as e:
        return str(e)
    skip = {".git", "node_modules", ".venv", "snap", "__pycache__"}
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



_SHOT_TOOLS = [
    ("grim", ("wayland",), lambda out: ["grim", out]),
    ("spectacle", ("wayland", "x11"), lambda out: ["spectacle", "-b", "-n", "-f", "-o", out]),
    ("gnome-screenshot", ("wayland", "x11"), lambda out: ["gnome-screenshot", "-f", out]),
    ("scrot", ("x11",), lambda out: ["scrot", "-o", out]),
    ("maim", ("x11",), lambda out: ["maim", "-u", out]),
]


def _shot_commands(out):
    desktop = session.kind()
    return [build(out) for tool, kinds, build in _SHOT_TOOLS
            if desktop in kinds and session.have(tool)]


def _has_pillow():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def can_see():
    return _has_pillow() and bool(_shot_commands("-"))


def _ydotool_socket():
    """Where ydotoold is listening, if it is running."""
    candidates = [os.environ.get("YDOTOOL_SOCKET"),
                  f"/run/user/{os.getuid()}/.ydotool_socket", "/tmp/.ydotool_socket"]
    return next((c for c in candidates if c and os.path.exists(c)), None)


def _input_tool():
    """"xdotool", "portal", "ydotool", or None if nothing here can move the mouse.
    On Wayland the desktop's own portal comes first: it reaches every window and
    needs no root, where ydotool needs a daemon and access to /dev/uinput."""
    if session.kind() == "x11" and session.have("xdotool"):
        return "xdotool"
    if portal.available():
        return "portal"
    if session.kind() and session.have("ydotool") and _ydotool_socket():
        return "ydotool"
    return None


def can_control():
    return _input_tool() is not None


def why_no_screen():
    if not session.kind():
        return "there is no desktop session here"
    if not _has_pillow():
        return "Pillow isn't installed (pip install pillow)"
    return ("no screenshot tool is installed: spectacle (KDE), gnome-screenshot (GNOME), "
            "grim (Sway, Hyprland) or scrot (X11)")


def _why_no_control():
    desktop = session.kind()
    if not desktop:
        return "there is no desktop session here"
    if desktop == "x11":
        return "xdotool isn't installed"
    if not portal.available() and not session.have("ydotool"):
        return f"{portal.why_not()}, and ydotool isn't installed"
    if session.have("ydotool"):
        return "ydotoold isn't running, so ydotool can't send anything"
    return "ydotool isn't installed (it needs its ydotoold daemon running)"


def status():
    """What the app shows next to the switch for this feature."""
    missing = []
    if not can_see():
        missing.append(f"To see the screen: {why_no_screen()}.")
    if not can_control():
        missing.append(f"To use the mouse and keyboard: {_why_no_control()}.")
    return {"enabled": available(), "screen": can_see(), "control": can_control(),
            "session": session.kind(), "missing": missing}


def _no_screen():
    return f"You can't see their screen: {why_no_screen()}. Tell them what is missing."


def _no_control():
    return (f"You can't use their mouse and keyboard: {_why_no_control()}. "
            "Tell them what is missing.")


def _capture(out):
    """Take a screenshot into the file `out`, or raise."""
    commands = _shot_commands(out)
    if not commands:
        raise RuntimeError(why_no_screen())
    for command in commands:
        try:
            subprocess.run(command, capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return
    raise RuntimeError(f"{commands[-1][0]} and the other screenshot tools here couldn't "
                       "capture the screen")


_last_screen = {"size": None}


def _screen_pixels():
    """(width, height) of the screen in the pixels of her screenshots."""
    if _last_screen["size"] is None:
        screenshot()
    return _last_screen["size"]


def screenshot():
    """(base64 JPEG scaled to MAX_EDGE, the screen's real width, height in pixels)."""
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="marina-shot-") as folder:
        raw = os.path.join(folder, "screen.png")
        _capture(raw)
        with Image.open(raw) as shot:
            width, height = shot.size
            image = shot.convert("RGB")
        _last_screen["size"] = (width, height)
        if max(width, height) > MAX_EDGE:
            scale = MAX_EDGE / max(width, height)
            image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
        scaled = os.path.join(folder, "screen.jpg")
        image.save(scaled, format="JPEG", quality=75)
        return base64.b64encode(Path(scaled).read_bytes()).decode(), width, height


def _vision(prompt, image, max_tokens=300):
    from process.llm_funcs import llm_scr

    out = llm_scr.chat_completion(
        [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]}],
        temperature=0, max_tokens=max_tokens)
    return (out.choices[0].message.content or "").strip()


LOOK_PROMPT = """This is a screenshot of their computer's screen (your own avatar may be in
a corner; ignore it). Describe what is on it plainly and accurately, for
your own use: which app is in front, what windows are open and what is in
them, and any text that matters to what they asked. Don't read out
passwords, codes or private messages in detail. Plain sentences, no
markdown, under 150 words.{question}"""


def computer_look(question=""):
    if not can_see():
        return _no_screen()
    try:
        image, _w, _h = screenshot()
        ask = f"\n\nIn particular: {question.strip()}" if (question or "").strip() else ""
        reply = _vision(LOOK_PROMPT.format(question=ask), image)
    except Exception as e:
        return f"Looking failed: {type(e).__name__}: {str(e)[:200]}"
    print("[computer] looked at the screen", flush=True)
    return reply or "You couldn't make anything out."


def _locate(target):
    """Screen point (x, y) in pixels of something described in words, or raise."""
    image, width, height = screenshot()
    ask = (f"Find {target} on this screenshot. Reply with only JSON "
           '{"x": X, "y": Y} for its centre, as thousandths of the image width and '
           'height (0-1000), or {"x": null, "y": null} if it is not on the screen.')
    raw = _vision(ask, image, max_tokens=40)
    m = re.search(r"\{.*\}", raw, re.S)
    spot = json.loads(m.group(0)) if m else {}
    if spot.get("x") is None or spot.get("y") is None:
        raise LookupError(f"couldn't find {target} on the screen")
    return float(spot["x"]) / 1000 * width, float(spot["y"]) / 1000 * height



_NAMED_KEYS = {
    "return": ("Return", 28), "enter": ("Return", 28), "tab": ("Tab", 15),
    "space": ("space", 57), "delete": ("BackSpace", 14), "backspace": ("BackSpace", 14),
    "forwarddelete": ("Delete", 111), "escape": ("Escape", 1), "esc": ("Escape", 1),
    "home": ("Home", 102), "end": ("End", 107), "pageup": ("Prior", 104),
    "pagedown": ("Next", 109), "left": ("Left", 105), "right": ("Right", 106),
    "up": ("Up", 103), "down": ("Down", 108),
    "=": ("equal", 13), "-": ("minus", 12), "]": ("bracketright", 27),
    "[": ("bracketleft", 26), "'": ("apostrophe", 40), ";": ("semicolon", 39),
    "\\": ("backslash", 43), ",": ("comma", 51), "/": ("slash", 53), ".": ("period", 52),
    "`": ("grave", 41),
}
_LETTER_CODES = dict(zip("qwertyuiop", range(16, 26)))
_LETTER_CODES.update(zip("asdfghjkl", range(30, 39)))
_LETTER_CODES.update(zip("zxcvbnm", range(44, 51)))
_DIGIT_CODES = dict(zip("1234567890", range(2, 12)))
_FUNCTION_CODES = {f"f{i}": code for i, code in
                   enumerate([59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88], 1)}
_MODIFIERS = {"shift": ("shift", 42), "ctrl": ("ctrl", 29), "control": ("ctrl", 29),
              "alt": ("alt", 56), "option": ("alt", 56), "opt": ("alt", 56),
              "super": ("super", 125), "win": ("super", 125), "meta": ("super", 125)}


def _key_name_and_code(key):
    """(xdotool name, Linux key code) for a key, or None if unknown."""
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if key in _FUNCTION_CODES:
        return key.upper(), _FUNCTION_CODES[key]
    if key in _LETTER_CODES:
        return key, _LETTER_CODES[key]
    if key in _DIGIT_CODES:
        return key, _DIGIT_CODES[key]
    return None


def _send(command):
    """Run a mouse or keyboard command. Raises with what the tool said."""
    env = dict(os.environ)
    if command[0] == "ydotool":
        env["YDOTOOL_SOCKET"] = _ydotool_socket() or env.get("YDOTOOL_SOCKET", "")
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{command[0]} didn't answer")
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:200] or f"{command[0]} failed")


def _front_text():
    front = session.front_window()
    return " — ".join(part for part in front if part) if front else ""


def _guard_front():
    front = session.front_window()
    if front and _SECRET_APPS.search(" ".join(front)):
        raise PermissionError(f"{front[0]} is in front, and that's where their passwords live")


def computer_click(target="", x=None, y=None, button="left", double=False):
    if not can_control():
        return _no_control()
    try:
        _guard_front()
        if x is not None and y is not None:
            px, py = float(x), float(y)
            what = f"({int(px)}, {int(py)})"
        elif (target or "").strip():
            if not can_see():
                return _no_screen()
            px, py = _locate(target.strip())
            what = f"{target.strip()} at ({int(px)}, {int(py)})"
        else:
            return "Say what to click (target), or give x and y in screen pixels."
        right = button == "right"
        if _input_tool() == "xdotool":
            _send(["xdotool", "mousemove", str(int(px)), str(int(py)), "click"]
                  + (["--repeat", "2", "--delay", "50"] if double else [])
                  + ["3" if right else "1"])
        elif _input_tool() == "portal":
            portal.click(px, py, screen_size=_screen_pixels(), right=right, double=double)
        else:
            _send(["ydotool", "mousemove", "--absolute", "-x", str(int(px)), "-y", str(int(py))])
            _send(["ydotool", "click"] + (["--repeat", "2", "--next-delay", "50"] if double else [])
                  + ["0xC1" if right else "0xC0"])
    except (PermissionError, LookupError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Clicking failed: {type(e).__name__}: {e}"
    print(f"[computer] clicked {what}", flush=True)
    return f"Clicked {what}. Look at the screen to check it did what you meant."


def _as_typed(text):
    """Undo the escaping models sometimes put in tool arguments: &amp; for &,
    and a written-out \\n for a new line when there are no real ones."""
    text = html.unescape(text)
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text


def computer_type(text, app=""):
    if not can_control():
        return _no_control()
    text = _as_typed(text if isinstance(text, str) else str(text or ""))
    if not text:
        return "Nothing to type."
    if app and _bring_forward(app, wait=3) is False:
        return (f"Didn't type: {_front_text() or 'another app'} is in front, not {app}. "
                f"Open {app} first.")
    unsure = []
    try:
        _guard_front()
        if _input_tool() == "xdotool":
            delay = "12" if text.isascii() else "60"
            _send(["xdotool", "type", "--delay", delay, "--clearmodifiers", "--", text])
        elif _input_tool() == "portal":
            unsure = portal.type_text(text)
        else:
            _send(["ydotool", "type", "--", text])
    except (PermissionError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Typing failed: {type(e).__name__}: {e}"
    print(f"[computer] typed {len(text)} characters", flush=True)
    typed = f"Typed {len(text)} characters into {_front_text() or 'the app in front'}."
    if unsure:
        typed += (f" The desktop only types characters its keyboard layout has, so these may "
                  f"be missing: {' '.join(unsure)}. Look at the screen to check.")
    return typed


def computer_key(keys):
    """A key or a combination like ctrl+s, shift+tab, return."""
    if not can_control():
        return _no_control()
    parts = [p.strip().lower() for p in (keys or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        return "No keys given."
    *mods, key = parts
    named = _key_name_and_code(key)
    if named is None or any(m not in _MODIFIERS for m in mods):
        return (f"Don't know {keys!r}. Use modifiers {', '.join(sorted(set(_MODIFIERS)))} "
                "with a letter, digit, or return, tab, space, escape, delete, arrows, f1-f12.")
    try:
        _guard_front()
        if _input_tool() == "xdotool":
            _send(["xdotool", "key", "+".join([_MODIFIERS[m][0] for m in mods] + [named[0]])])
        elif _input_tool() == "portal":
            portal.press_keys(mods, key)
        else:
            codes = [_MODIFIERS[m][1] for m in mods]
            _send(["ydotool", "key"] + [f"{c}:1" for c in codes] + [f"{named[1]}:1", f"{named[1]}:0"]
                  + [f"{c}:0" for c in reversed(codes)])
    except (PermissionError, RuntimeError) as e:
        return str(e)
    except Exception as e:
        return f"Pressing keys failed: {type(e).__name__}: {e}"
    print(f"[computer] pressed {keys}", flush=True)
    return f"Pressed {keys}."


def computer_scroll(direction="down", amount=5):
    if not can_control():
        return _no_control()
    try:
        amount = max(1, min(int(amount or 5), 50))
    except (TypeError, ValueError):
        amount = 5
    if direction not in ("up", "down", "left", "right"):
        return "direction must be up, down, left or right."
    try:
        if _input_tool() == "xdotool":
            button = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
            _send(["xdotool", "click", "--repeat", str(amount), "--delay", "20", button])
            return f"Scrolled {direction}."
        if _input_tool() == "portal":
            portal.scroll(direction, amount)
            return f"Scrolled {direction}."
        code = {"up": 103, "down": 108, "left": 105, "right": 106}[direction]
        _send(["ydotool", "key"] + [f"{code}:{state}" for _ in range(amount) for state in (1, 0)])
    except RuntimeError as e:
        return str(e)
    return f"Scrolled {direction} with the arrow keys, in whatever window has focus."



def computer_apps():
    front = _front_text()
    lines = [f"In front: {front}" if front else "In front: (this desktop doesn't say)"]
    titles = session.window_titles()
    if not titles:
        lines.append("Open windows: (this desktop doesn't list them; use computer_look)")
    for app, names in titles.items():
        if not _SECRET_APPS.search(app):
            lines.append(f"Windows of {app}: " + "; ".join(names[:6]))
    return "\n".join(lines)


def _bring_forward(app, wait=10.0, settle=0.0):
    """True once `app` is in front, False if something else still is after
    `wait` seconds, None if this desktop won't say what is in front (after
    waiting `settle` seconds for the window to appear). Typing before the app
    is in front would land in whatever they had open."""
    want = app.lower().removesuffix(".desktop")
    deadline = time.time() + wait
    while True:
        front = session.front_window()
        if front is None:
            time.sleep(settle)
            return None
        text = " ".join(front).lower()
        if want in text or (front[0] and front[0].lower() in want):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _app_dirs():
    data_home = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local" / "share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    extra = [str(Path(data_home) / "flatpak" / "exports" / "share"),
             "/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop"]
    return [Path(d) / "applications" for d in [data_home, *data_dirs, *extra] if d]


def _read_entry(path):
    """The launcher in a .desktop file, or None if it isn't one to show."""
    fields, in_entry = {}, False
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
        elif in_entry and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    if (fields.get("Type") != "Application" or fields.get("NoDisplay") == "true"
            or fields.get("Hidden") == "true" or not fields.get("Name") or not fields.get("Exec")):
        return None
    return {"id": path.stem, "path": path, "name": fields["Name"], "exec": fields["Exec"]}


def _find_app(name):
    """The installed app a name means ("Firefox", "dolphin", "kate"), or None."""
    want = name.strip().casefold().removesuffix(".desktop")
    entries = {}
    for directory in _app_dirs():
        for path in sorted(directory.glob("*.desktop")) if directory.is_dir() else []:
            if path.stem not in entries:
                try:
                    entry = _read_entry(path)
                except OSError:
                    continue
                if entry:
                    entries[path.stem] = entry
    for entry in entries.values():
        ident = entry["id"].casefold()
        if want in (entry["name"].casefold(), ident, ident.rsplit(".", 1)[-1]):
            return entry
    partial = [e for e in entries.values()
               if want in e["name"].casefold() or want in e["id"].casefold()]
    return min(partial, key=lambda e: len(e["name"])) if partial else None


def _launch(entry, url=""):
    """Start an app, with a web page for it to open if one is given."""
    uris = [url] if url else []
    for command in (["gio", "launch", str(entry["path"]), *uris], ["gtk-launch", entry["id"], *uris]):
        if session.have(command[0]) and session.spawn(command):
            return True
    argv = [a for a in shlex.split(entry["exec"]) if not re.fullmatch(r"%[A-Za-z]", a)]
    return bool(argv) and session.spawn(argv + uris)


def _open_link(url, app=""):
    """Open a web page in the named browser, or the default one."""
    entry = _find_app(app) if app else None
    if entry and _SECRET_APPS.search(entry["name"]):
        return f"{entry['name']} holds their passwords, so it's off limits."
    if entry and _launch(entry, url):
        print(f"[computer] opened {url} in {entry['name']}", flush=True)
        return f"Opened {url} in {entry['name']}."
    if not session.spawn(["xdg-open", url]):
        return f"Couldn't open {url}: xdg-open isn't installed or failed."
    note = f" ({app} isn't installed, so this is their default browser)" if app else ""
    return f"Opened {url}{note}."


def computer_open(app="", url="", new_document=False):
    app = (app or "").strip()
    if url:
        if not re.match(r"^https?://", url):
            return "Only http and https links."
        if _SECRET_APPS.search(app):
            return f"{app} holds their passwords, so it's off limits."
        return _open_link(url, app)
    if not app:
        return "Say which app."
    if _SECRET_APPS.search(app):
        return f"{app} holds their passwords, so it's off limits."
    entry = _find_app(app)
    if entry and _SECRET_APPS.search(entry["name"]):
        return f"{entry['name']} holds their passwords, so it's off limits."
    if entry:
        launched = _launch(entry)
    else:
        launched = session.have(app) and session.spawn([app])
    if not launched:
        return f"Couldn't open {app}: no app by that name is installed."
    print(f"[computer] opened {app}", flush=True)
    in_front = _bring_forward(app, settle=1.5)
    if in_front is False:
        return (f"Asked the desktop to open {app}, but {_front_text() or 'something else'} is "
                "still in front. Look at the screen before typing anything.")
    note = "" if in_front else " (this desktop doesn't say what is in front, so look to check)"
    if new_document:
        if not can_control():
            return f"{app} is open, but " + _no_control()
        time.sleep(0.6)
        computer_key("ctrl+n")
        time.sleep(0.8)
        return (f"{app} is open with a new document{note}. Type with "
                f"computer_type(text, app=\"{app}\").")
    return f"{app} is open and in front." if in_front else f"{app} is open{note}."



def describe(name, args):
    def p():
        return args.get("path") or "home"
    if name == "computer_run_command":
        line = (args.get("command") or "").strip().splitlines()
        return f"running `{line[0][:70]}`" if line else "running a command"
    if name == "computer_click":
        return f"clicking {args.get('target') or 'on the screen'}"
    if name == "computer_type":
        text = args.get("text") or ""
        return f"typing “{text[:40]}{'…' if len(text) > 40 else ''}”"
    if name == "computer_key":
        return f"pressing {args.get('keys', '')}"
    if name == "computer_open":
        return f"opening {args.get('app') or args.get('url') or 'an app'}"
    return {
        "computer_read_file": f"reading {p()}", "computer_write_file": f"writing {p()}",
        "computer_edit_file": f"editing {p()}", "computer_list_files": f"looking through {p()}",
        "computer_look": "looking at your screen", "computer_apps": "checking what's open",
        "computer_scroll": f"scrolling {args.get('direction', 'down')}",
    }.get(name, name)


def consent_key(name):
    return "gui" if name in GUI_TOOLS else name


def needs_ok(name, args, consented):
    """The question to ask them first, or None."""
    if name in GUI_TOOLS:
        return None if "gui" in consented else "Let her use your mouse and keyboard for this?"
    if name == "computer_run_command":
        command = (args.get("command") or "").strip()
        if _RISKY.search(command):
            return f"Let her run this on your computer?\n{command[:300]}"
    if name in ("computer_write_file", "computer_edit_file"):
        try:
            p = _path(args.get("path", ""))
        except ValueError:
            return None
        if name == "computer_write_file" and p.exists():
            return f"Let her overwrite {p}?"
    return None


SPECS = [
    ("computer_look", computer_look, {
        "description": "Take a screenshot of their computer's screen and look at it. Use it "
                       "before working in one of their apps, and after doing something "
                       "to check it worked. You can't see their screen otherwise.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What you want to check."}}}}),
    ("computer_apps", computer_apps, {
        "description": "List the windows open on their computer, and which is in front, "
                       "where the desktop will say.",
        "parameters": {"type": "object", "properties": {}}}),
    ("computer_open", computer_open, {
        "description": "Open an app on their computer by name (e.g. \"Google Chrome\", "
                       "\"Kate\"). To open a web page in a particular browser, give both app "
                       "and url (\"open YouTube in Chrome\" is app Google Chrome, url "
                       "https://www.youtube.com). Waits until the app is in front, where the "
                       "desktop will say. Set new_document when they want something written "
                       "in a new document (a text editor, a word processor...): it presses "
                       "ctrl+n.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}, "url": {"type": "string"},
            "new_document": {"type": "boolean"}}}}),
    ("computer_click", computer_click, {
        "description": "Click on their computer's screen. Describe what to click in target "
                       "(e.g. \"the Save button in the dialog\") and it is found on a fresh "
                       "screenshot, or give x and y in screen pixels.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
            "button": {"type": "string", "enum": ["left", "right"]},
            "double": {"type": "boolean"}}}}),
    ("computer_type", computer_type, {
        "description": "Type text into whatever has focus on their computer. Give app (the "
                       "app you mean to type into) and nothing is typed unless it is in "
                       "front. In a form or field, click it first.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "app": {"type": "string"}},
                       "required": ["text"]}}),
    ("computer_key", computer_key, {
        "description": "Press a key or shortcut on their computer: e.g. return, tab, escape, "
                       "ctrl+s, ctrl+shift+t, alt+tab, up.",
        "parameters": {"type": "object", "properties": {"keys": {"type": "string"}},
                       "required": ["keys"]}}),
    ("computer_scroll", computer_scroll, {
        "description": "Scroll whatever is under the mouse on their computer.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer"}}}}),
    ("computer_run_command", computer_run_command, {
        "description": "Run a bash command on their computer, in their home folder unless you "
                       "give cwd. Risky commands ask them first.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}, "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number"}}, "required": ["command"]}}),
    ("computer_read_file", computer_read_file, {
        "description": "Read a file on their computer (inside their home folder) with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}}, "required": ["path"]}}),
    ("computer_write_file", computer_write_file, {
        "description": "Create a file on their computer, or overwrite one (which asks them first).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}),
    ("computer_edit_file", computer_edit_file, {
        "description": "Change part of a file on their computer: replace old_text, which must "
                       "appear exactly once, with new_text.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"},
            "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}),
    ("computer_list_files", computer_list_files, {
        "description": "List files under a folder on their computer.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "depth": {"type": "integer"}}}}),
]

PROMPT = """

You can also work on their computer, the one they are using right now:
computer_look to see the screen, computer_apps to see what's open,
computer_open, computer_click, computer_type, computer_key and
computer_scroll to use it, and computer_run_command and the computer_*_file
tools for commands and files. Their computer is theirs, not yours: be
careful with it, do what they asked and nothing more. Whenever they ask
what's on their screen or what they have open, call computer_look or
computer_apps in this reply and answer from what it returns, never from
memory or from earlier in the conversation: their screen changes all the
time, and a made-up answer is worse than none. Look before you act
in an app and look again afterwards to check. The first time in a reply you
use their mouse or keyboard, and before anything risky, the app asks them;
if they say no, don't look for a way round it. Until they've answered,
don't talk as if it's happening: say "I'll need your OK to delete that",
not "Deleting that now", and only say it's done once the tool says so.
Their passwords, keys and
password managers are off limits. Say which computer you mean: "my
machine" is yours, "your computer" is theirs."""
