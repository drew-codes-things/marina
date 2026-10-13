"""Which kind of Linux desktop this is, and the few command-line tools the
other tools use to ask it things (xprop, kdotool, ...).

Under Wayland a program can't ask which window is in front or move the mouse
the way it can under X11, so everything here answers None, "" or {} when the
desktop won't say, and callers carry on without.
"""
import os
import re
import shutil
import subprocess


def kind():
    """"x11", "wayland", or "" when there is no desktop session (a headless
    server running only Telegram, for one)."""
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("x11", "wayland"):
        return declared
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else ""


def have(command):
    return shutil.which(command) is not None


def run(args, timeout=2.0):
    """Stdout of a command, or None if it isn't installed, failed or timed out."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def spawn(command):
    """Start a program and leave it running. True if it started."""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    try:
        return proc.wait(timeout=3) == 0
    except subprocess.TimeoutExpired:
        return True


_WINDOW_ID = re.compile(r"0x[0-9a-fA-F]+")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_xprop(text):
    """(app, title) from the output of `xprop WM_CLASS _NET_WM_NAME WM_NAME`."""
    app = title = ""
    for line in text.splitlines():
        name, _, value = line.partition("=")
        quoted = _QUOTED.findall(value)
        if not quoted:
            continue
        if name.startswith("WM_CLASS"):
            app = quoted[-1]
        elif name.startswith("_NET_WM_NAME"):
            title = quoted[0]
        elif name.startswith("WM_NAME") and not title:
            title = quoted[0]
    return app, title


def _x11_window(window_id):
    info = run(["xprop", "-id", window_id, "WM_CLASS", "_NET_WM_NAME", "WM_NAME"])
    return parse_xprop(info) if info else None


def front_window():
    """(app, title) of the window in front, or None if this desktop won't say.

    X11 answers through xprop. Wayland has no way to ask, except KDE with
    kdotool installed. (xprop under Wayland only sees X11 windows, and would
    name the wrong one whenever a Wayland app has focus.)"""
    if kind() == "x11":
        root = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        found = _WINDOW_ID.search(root or "")
        if not found or int(found.group(), 16) == 0:
            return None
        return _x11_window(found.group())
    if kind() == "wayland" and have("kdotool"):
        app = run(["kdotool", "getactivewindow", "getwindowclassname"])
        title = run(["kdotool", "getactivewindow", "getwindowname"])
        return (app, title or "") if app else None
    return None


def window_titles():
    """Titles of the open windows by app, {} where the desktop won't say."""
    if kind() != "x11":
        return {}
    listing = run(["xprop", "-root", "_NET_CLIENT_LIST"])
    out = {}
    for window_id in _WINDOW_ID.findall(listing or ""):
        window = _x11_window(window_id)
        if window and window[1]:
            out.setdefault(window[0], []).append(window[1])
    return out
