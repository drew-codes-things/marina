"""One codebase, two desktops: on a Mac she gets the Mac tools and the Mac's own
commands exactly as before, anywhere else the Linux ones. The platform is
faked here, since only one of the two is ever the machine running the tests."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.tools import context, mac, registry


@pytest.fixture
def platform(monkeypatch):
    """Reload the registry as if on the named platform; put it back afterwards."""
    def switch(name):
        monkeypatch.setattr(sys, "platform", name)
        return importlib.reload(registry)
    yield switch
    monkeypatch.undo()
    importlib.reload(registry)


def tool_names(module):
    return {name for name, *_ in module.SPECS}


def test_a_mac_gets_the_mac_tools(platform):
    reg = platform("darwin")
    assert reg._DESKTOP is mac and reg._DESKTOP_KEY == "mac"
    listed = {spec["function"]["name"] for spec in reg.definitions() or []}
    assert all(name.startswith("mac_") for name in reg._DESKTOP_TOOLS)
    assert not any(name.startswith("computer_") for name in listed)


def test_linux_gets_the_computer_tools(platform):
    reg = platform("linux")
    assert reg._DESKTOP.__name__.endswith("computer") and reg._DESKTOP_KEY == "computer"
    assert all(name.startswith("computer_") for name in reg._DESKTOP_TOOLS)
    assert not any(name in reg._DESKTOP_TOOLS for name in tool_names(mac))


def test_a_macs_tool_calls_are_labelled_as_the_macs(platform):
    reg = platform("darwin")
    name = sorted(reg._DESKTOP_TOOLS)[0]
    assert reg.describe(name, "{}")[1] == "mac"


def test_the_mac_reads_its_clipboard_with_pbpaste_and_opens_links_with_open(platform, monkeypatch):
    reg = platform("darwin")
    ran = []

    class Done:
        stdout = "copied text\n"
        returncode = 0

    monkeypatch.setattr(reg.subprocess, "run", lambda command, **kw: ran.append(command) or Done())
    assert "copied text" in reg.read_clipboard()
    assert "example.com" in reg.open_url("https://example.com/page")
    assert ran == [["pbpaste"], ["open", "https://example.com/page"]]


def test_the_macs_tools_need_only_the_setting_not_linux_programs(platform, monkeypatch):
    reg = platform("darwin")
    monkeypatch.setattr(reg.session, "have", lambda tool: False)
    monkeypatch.setattr(reg.session, "kind", lambda: "")
    assert reg._clipboard_available() and reg._open_url_available()


def test_linux_needs_a_desktop_session_and_a_tool(platform, monkeypatch):
    reg = platform("linux")
    monkeypatch.setattr(reg.session, "kind", lambda: "")
    assert not reg._clipboard_available() and not reg._open_url_available()
    monkeypatch.setattr(reg.session, "kind", lambda: "wayland")
    monkeypatch.setattr(reg.session, "have", lambda tool: tool in ("wl-paste", "xdg-open"))
    assert reg._clipboard_available() and reg._open_url_available()


def test_open_url_tells_linux_to_use_computer_open_for_a_named_browser(platform):
    spec = next(s for n, _f, _o, s in platform("linux").SPECS if n == "open_url")
    assert "computer_open" in spec["description"]
    spec = next(s for n, _f, _o, s in platform("darwin").SPECS if n == "open_url")
    assert "computer_open" not in spec["description"]


def test_a_mac_asks_the_mac_what_is_in_front_and_linux_asks_the_desktop(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    answers = {("lsappinfo", "front"): "ASN:0x0-1", }
    monkeypatch.setattr(context, "_run", lambda args: answers.get(tuple(args), 'x="Safari"'))
    assert context.frontmost_app() == "Safari"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(context.session, "front_window", lambda: ("kate", "notes"))
    assert context.frontmost_app() == "kate"


def test_a_mac_reads_its_battery_with_pmset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    asked = []
    monkeypatch.setattr(context, "_run", lambda args: asked.append(args) or
                        "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t12%; discharging")
    assert "down to 12%" in context.battery()
    assert asked == [["pmset", "-g", "batt"]]


@pytest.mark.parametrize("percent, status, expected", [
    (10, "Discharging", "on battery, down to 10% and not charging"),
    (30, "Discharging", "on battery, 30% left"),
    (80, "Discharging", ""), (50, "Charging", "on charge"), (99, "Charging", ""), (50, "Full", ""),
])
def test_linux_battery_wording(percent, status, expected):
    assert context.battery_state(percent, status) == expected
