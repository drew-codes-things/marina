"""Mouse and keyboard through the RemoteDesktop portal: which keysyms are sent,
in what order, and when the portal is the tool chosen. The desktop itself is
replaced by a list that records the events."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.tools import computer, portal, session

RETURN, TAB, CONTROL, SHIFT = 0xFF0D, 0xFF09, 0xFFE3, 0xFFE1


@pytest.fixture
def events(monkeypatch):
    """What would have been sent to the desktop, as (method, arguments...)."""
    sent = []
    monkeypatch.setattr(portal, "_notify", lambda method, signature, *args: sent.append((method, *args)))
    monkeypatch.setattr(portal, "_session", lambda: (None, "/session"))
    monkeypatch.setitem(portal._open, "stream", 7)
    monkeypatch.setattr(portal.time, "sleep", lambda seconds: None)
    return sent


def keys_sent(events):
    return [(args[1], args[2]) for args in events if args[0] == "NotifyKeyboardKeysym"]


def test_characters_are_their_own_keysym_or_unicode():
    assert portal.keysym_for_character("a") == 0x61
    assert portal.keysym_for_character("A") == 0x41
    assert portal.keysym_for_character("é") == 0xE9
    assert portal.keysym_for_character("€") == 0x01000000 + 0x20AC
    assert portal.keysym_for_character("日") == 0x01000000 + 0x65E5


def test_typing_presses_then_releases_each_character(events):
    portal.type_text("hi")
    assert keys_sent(events) == [(0x68, 1), (0x68, 0), (0x69, 1), (0x69, 0)]


def test_newline_and_tab_are_the_keys_not_characters(events):
    portal.type_text("a\n\tb")
    pressed = [keysym for keysym, state in keys_sent(events) if state == 1]
    assert pressed == [0x61, RETURN, TAB, 0x62]


def test_other_control_characters_are_skipped(events):
    portal.type_text("a\x07b")
    assert [k for k, s in keys_sent(events) if s == 1] == [0x61, 0x62]


def test_a_shortcut_holds_the_modifiers_around_the_key(events):
    portal.press_keys(["ctrl", "shift"], "s")
    assert keys_sent(events) == [(CONTROL, 1), (SHIFT, 1), (0x73, 1), (0x73, 0), (SHIFT, 0), (CONTROL, 0)]


def test_modifiers_are_released_even_if_the_key_fails(events, monkeypatch):
    def fail(keysym):
        raise portal.PortalError("gone")
    monkeypatch.setattr(portal, "_press", fail)
    with pytest.raises(portal.PortalError):
        portal.press_keys(["ctrl"], "s")
    assert keys_sent(events) == [(CONTROL, 1), (CONTROL, 0)]


def test_unknown_keys_and_modifiers_are_refused(events):
    with pytest.raises(portal.PortalError):
        portal.press_keys([], "notakey")
    with pytest.raises(portal.PortalError):
        portal.press_keys(["hyper"], "s")
    assert events == []


def test_named_keys():
    assert portal.keysym_for_key("return") == RETURN
    assert portal.keysym_for_key("f5") == 0xFFBE + 4
    assert portal.keysym_for_key("left") == 0xFF51


def test_click_goes_to_the_point_on_the_stream_then_presses_and_releases(events):
    portal.click(100, 200)
    assert events == [("NotifyPointerMotionAbsolute", 7, 100.0, 200.0),
                      ("NotifyPointerButton", portal.BTN_LEFT, 1),
                      ("NotifyPointerButton", portal.BTN_LEFT, 0)]


def test_right_and_double_clicks(events):
    portal.click(1, 2, right=True, double=True)
    buttons = [args[1:] for args in events if args[0] == "NotifyPointerButton"]
    assert buttons == [(portal.BTN_RIGHT, 1), (portal.BTN_RIGHT, 0)] * 2


@pytest.mark.parametrize("direction, axis, steps", [
    ("down", 0, 3), ("up", 0, -3), ("right", 1, 3), ("left", 1, -3)])
def test_scrolling_is_the_wheel_on_the_right_axis(events, direction, axis, steps):
    portal.scroll(direction, 3)
    assert events == [("NotifyPointerAxisDiscrete", axis, steps)]


def test_not_offered_on_x11(monkeypatch):
    monkeypatch.setattr(session, "kind", lambda: "x11")
    assert portal.available() is False


def test_wayland_uses_the_portal_before_ydotool(monkeypatch):
    monkeypatch.setattr(session, "kind", lambda: "wayland")
    monkeypatch.setattr(portal, "available", lambda: True)
    monkeypatch.setattr(session, "have", lambda tool: True)
    monkeypatch.setattr(computer, "_ydotool_socket", lambda: "/run/ydotool")
    assert computer._input_tool() == "portal"


def test_wayland_falls_back_to_ydotool_without_the_portal(monkeypatch):
    monkeypatch.setattr(session, "kind", lambda: "wayland")
    monkeypatch.setattr(portal, "available", lambda: False)
    monkeypatch.setattr(session, "have", lambda tool: tool == "ydotool")
    monkeypatch.setattr(computer, "_ydotool_socket", lambda: "/run/ydotool")
    assert computer._input_tool() == "ydotool"


def test_x11_still_uses_xdotool(monkeypatch):
    monkeypatch.setattr(session, "kind", lambda: "x11")
    monkeypatch.setattr(session, "have", lambda tool: tool == "xdotool")
    assert computer._input_tool() == "xdotool"


def test_a_refusal_comes_back_as_words_for_the_person(monkeypatch):
    monkeypatch.setattr(computer, "_input_tool", lambda: "portal")
    monkeypatch.setattr(computer, "_guard_front", lambda: None)
    def refuse(text):
        raise portal.PortalError("You didn't allow Marina to use the mouse and keyboard.")
    monkeypatch.setattr(portal, "type_text", refuse)
    assert "didn't allow" in computer.computer_type("hello")


def test_typing_reports_the_characters_the_desktop_may_drop(events):
    assert portal.type_text("plain text") == []
    assert portal.type_text("caf\u00e9 \u20ac caf\u00e9") == ["\u00e9", "\u20ac"]


def test_the_reply_warns_about_characters_that_may_be_missing(monkeypatch):
    monkeypatch.setattr(computer, "_input_tool", lambda: "portal")
    monkeypatch.setattr(computer, "_guard_front", lambda: None)
    monkeypatch.setattr(portal, "type_text", lambda text: ["\u00e9"])
    reply = computer.computer_type("caf\u00e9")
    assert reply.startswith("Typed 4 characters") and "may be missing: \u00e9" in reply
    monkeypatch.setattr(portal, "type_text", lambda text: [])
    assert "missing" not in computer.computer_type("cafe")


def test_clicks_are_scaled_from_screenshot_pixels_to_the_desktops_units(events, monkeypatch):
    monkeypatch.setitem(portal._open, "size", (1829, 1029))
    portal.click(1920, 1080, screen_size=(1920, 1080))
    assert events[0] == ("NotifyPointerMotionAbsolute", 7, 1828.0, 1028.0)
    events.clear()
    portal.click(960, 540, screen_size=(1920, 1080))
    assert events[0][2:] == (pytest.approx(914.5), pytest.approx(514.5))


def test_clicks_outside_the_screen_are_clamped_to_its_edge(events, monkeypatch):
    monkeypatch.setitem(portal._open, "size", (1829, 1029))
    portal.click(-50, 99999)
    assert events[0] == ("NotifyPointerMotionAbsolute", 7, 0, 1028)
