"""Mouse and keyboard through the desktop's own RemoteDesktop portal.

On Wayland an app can't press keys or move the pointer in other windows by
itself, but the desktop (KDE, GNOME) offers a standard way to ask: the
org.freedesktop.portal.RemoteDesktop service. The first time she uses it the
desktop shows a prompt, and the answer is remembered through a restore token.

The portal session closes when the program that opened it disconnects from the
bus, so one connection is kept for the life of the bridge. Nothing here runs
until she is first asked to use the mouse or keyboard: `available()` only looks.
"""
import os
import threading
import time
import uuid
from pathlib import Path

from process.config import DATA_ROOT
from process.tools import session

try:
    from jeepney import DBusAddress, MatchRule, Properties, message_bus, new_method_call
    from jeepney.io.blocking import Proxy, open_dbus_connection
except ImportError:
    open_dbus_connection = None

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
SCREEN_CAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"

KEYBOARD, POINTER = 1, 2
SOURCE_MONITOR = 1
PERSIST_UNTIL_REVOKED = 2

APPROVAL_SECONDS = 90
STEP_SECONDS = 10

TOKEN_FILE = DATA_ROOT / "portal_token"

BTN_LEFT, BTN_RIGHT = 0x110, 0x111
KEY_PRESS_SECONDS = 0.004
DOUBLE_CLICK_SECONDS = 0.05
VERTICAL_AXIS, HORIZONTAL_AXIS = 0, 1

UNICODE_KEYSYM_BASE = 0x01000000
KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "space": 0x20,
    "delete": 0xFF08, "backspace": 0xFF08, "forwarddelete": 0xFFFF,
    "escape": 0xFF1B, "esc": 0xFF1B, "home": 0xFF50, "left": 0xFF51, "up": 0xFF52,
    "right": 0xFF53, "down": 0xFF54, "pageup": 0xFF55, "pagedown": 0xFF56, "end": 0xFF57,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}
MODIFIER_KEYSYMS = {
    "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9, "option": 0xFFE9,
    "opt": 0xFFE9, "super": 0xFFEB, "win": 0xFFEB, "meta": 0xFFEB,
}


class PortalError(RuntimeError):
    """Something the person should hear: they said no, or the desktop can't."""


_lock = threading.RLock()
_supported = None
_open = {"connection": None, "session": None, "stream": None, "size": None}


def keysym_for_character(character):
    code = ord(character)
    return code if code <= 0xFF else UNICODE_KEYSYM_BASE + code


def keysym_for_key(name):
    """Keysym for a key given by name ("return", "f5", "s"), or None."""
    if name in KEYSYMS:
        return KEYSYMS[name]
    if len(name) == 1:
        return keysym_for_character(name)
    return None


def available():
    """Whether this desktop can take mouse and keyboard from us this way. Never
    shows a prompt: that waits for the first real use."""
    global _supported
    if session.kind() != "wayland" or open_dbus_connection is None:
        return False
    if _supported is None:
        _supported = _portal_offers_input()
    return _supported


def why_not():
    if session.kind() != "wayland":
        return "this isn't a Wayland session"
    if open_dbus_connection is None:
        return "the jeepney package isn't installed (pip install jeepney)"
    return ("the desktop has no RemoteDesktop portal "
            "(xdg-desktop-portal with the KDE or GNOME backend)")


def _portal_offers_input():
    try:
        connection = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        address = DBusAddress(PORTAL_PATH, PORTAL_BUS_NAME, REMOTE_DESKTOP)
        reply = connection.send_and_get_reply(Properties(address).get("AvailableDeviceTypes"),
                                              timeout=STEP_SECONDS)
        devices = reply.body[0][1]
        return bool(devices & KEYBOARD) and bool(devices & POINTER)
    except Exception:
        return False
    finally:
        connection.close()


def _address(interface):
    return DBusAddress(PORTAL_PATH, PORTAL_BUS_NAME, interface)


def _request(connection, interface, method, signature, arguments, options, timeout):
    """Call a portal method that answers later through a Response signal, and
    return that answer's results. Raises PortalError if it was refused."""
    token = "marina" + uuid.uuid4().hex[:12]
    sender = connection.unique_name[1:].replace(".", "_")
    rule = MatchRule(type="signal", interface=REQUEST, member="Response",
                     path=f"{PORTAL_PATH}/request/{sender}/{token}")
    Proxy(message_bus, connection).AddMatch(rule)
    with connection.filter(rule) as answers:
        message = new_method_call(_address(interface), method, signature,
                                  (*arguments, {**options, "handle_token": ("s", token)}))
        connection.send_and_get_reply(message, timeout=STEP_SECONDS)
        try:
            answer = connection.recv_until_filtered(answers, timeout=timeout)
        except TimeoutError:
            raise PortalError("The desktop's permission prompt wasn't answered in time.")
    code, results = answer.body
    if code == 1:
        raise PortalError("You didn't allow Marina to use the mouse and keyboard "
                          "(the desktop's prompt was dismissed).")
    if code != 0:
        raise PortalError(f"The desktop refused {method} (code {code}).")
    return results


def _saved_token():
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def _save_token(token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def _forget_token():
    try:
        TOKEN_FILE.unlink()
    except OSError:
        pass


def _start_session(connection):
    """Create a remote desktop session on the connection, asking the person if
    the desktop needs to. Returns (session handle, stream id, stream size)."""
    created = _request(connection, REMOTE_DESKTOP, "CreateSession", "a{sv}", (), {
        "session_handle_token": ("s", "marina" + uuid.uuid4().hex[:12])}, STEP_SECONDS)
    handle = created["session_handle"][1]

    select = {"types": ("u", KEYBOARD | POINTER), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
    if _saved_token():
        select["restore_token"] = ("s", _saved_token())
    _request(connection, REMOTE_DESKTOP, "SelectDevices", "oa{sv}", (handle,), select, STEP_SECONDS)
    _request(connection, SCREEN_CAST, "SelectSources", "oa{sv}", (handle,), {
        "types": ("u", SOURCE_MONITOR), "multiple": ("b", False)}, STEP_SECONDS)

    started = _request(connection, REMOTE_DESKTOP, "Start", "osa{sv}", (handle, ""), {},
                       APPROVAL_SECONDS)
    if "restore_token" in started:
        _save_token(started["restore_token"][1])
    streams = started.get("streams", ("", []))[1]
    if not streams:
        raise PortalError("The desktop gave no screen to point at.")
    stream_id, properties = streams[0]
    size = properties.get("size", ("", (None, None)))[1]
    return handle, stream_id, size


def _session():
    """The open session, made on first use. Call with `_lock` held."""
    if _open["session"]:
        return _open["connection"], _open["session"]
    connection = open_dbus_connection(bus="SESSION")
    try:
        handle, stream, size = _start_session(connection)
    except PortalError:
        _forget_token()
        connection.close()
        raise
    except Exception:
        connection.close()
        raise
    _open.update(connection=connection, session=handle, stream=stream, size=size)
    return connection, handle


def _drop_session():
    connection = _open["connection"]
    _open.update(connection=None, session=None, stream=None, size=None)
    if connection:
        try:
            connection.close()
        except Exception:
            pass


def _notify(method, signature, *arguments):
    """Send one input event. If the session has gone (the desktop restarted,
    or the permission was revoked), start a new one once and try again."""
    with _lock:
        for attempt in (1, 2):
            connection, handle = _session()
            message = new_method_call(_address(REMOTE_DESKTOP), method, f"oa{{sv}}{signature}",
                                      (handle, {}, *arguments))
            try:
                connection.send_and_get_reply(message, timeout=STEP_SECONDS)
                return
            except Exception as e:
                _drop_session()
                if attempt == 2:
                    raise PortalError(f"The desktop wouldn't take input: {e}")


def _press(keysym):
    _notify("NotifyKeyboardKeysym", "iu", keysym, 1)
    time.sleep(KEY_PRESS_SECONDS)
    _notify("NotifyKeyboardKeysym", "iu", keysym, 0)


def type_text(text):
    """Type text into the window in front. Returns the characters beyond ASCII
    that were sent: KDE only types the ones its keyboard layout has and drops
    the rest without saying, so the caller should warn about these."""
    beyond_ascii = []
    for character in text:
        if character == "\n":
            _press(KEYSYMS["return"])
        elif character == "\t":
            _press(KEYSYMS["tab"])
        elif character.isprintable():
            _press(keysym_for_character(character))
            if not character.isascii() and character not in beyond_ascii:
                beyond_ascii.append(character)
    return beyond_ascii


def press_keys(modifiers, key):
    """Hold the modifiers (names), tap the key (a name), release."""
    keysym = keysym_for_key(key)
    held = [MODIFIER_KEYSYMS.get(m) for m in modifiers]
    if keysym is None or None in held:
        raise PortalError(f"Don't know the key {'+'.join([*modifiers, key])!r}.")
    for modifier in held:
        _notify("NotifyKeyboardKeysym", "iu", modifier, 1)
    try:
        _press(keysym)
    finally:
        for modifier in reversed(held):
            _notify("NotifyKeyboardKeysym", "iu", modifier, 0)


def click(x, y, screen_size=None, right=False, double=False):
    """Click at a point in screen pixels, given the (width, height) of the
    screen those pixels are on. The desktop measures in scaled units, so at 105%
    its screen is smaller than the screenshot she looked at."""
    with _lock:
        _session()
        stream, size = _open["stream"], _open["size"]
    if screen_size and size and None not in size:
        x, y = x * size[0] / screen_size[0], y * size[1] / screen_size[1]
    if size and None not in size:
        x, y = min(max(x, 0), size[0] - 1), min(max(y, 0), size[1] - 1)
    _notify("NotifyPointerMotionAbsolute", "udd", stream, float(x), float(y))
    button = BTN_RIGHT if right else BTN_LEFT
    for _ in range(2 if double else 1):
        _notify("NotifyPointerButton", "iu", button, 1)
        _notify("NotifyPointerButton", "iu", button, 0)
        if double:
            time.sleep(DOUBLE_CLICK_SECONDS)


def scroll(direction, steps):
    """Turn the wheel under the pointer: up and left are negative."""
    axis = VERTICAL_AXIS if direction in ("up", "down") else HORIZONTAL_AXIS
    sign = -1 if direction in ("up", "left") else 1
    _notify("NotifyPointerAxisDiscrete", "ui", axis, sign * steps)


def close():
    with _lock:
        _drop_session()
