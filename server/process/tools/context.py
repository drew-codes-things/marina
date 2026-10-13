"""Time, frontmost app name and battery, for the prompt."""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from process.config import load_config
from process.tools import session

_config = load_config()
_ctx = _config.get("context") or {}

ENABLED = bool(_ctx.get("enabled", True))
SHOW_TIME = bool(_ctx.get("time", True))
SHOW_APP = bool(_ctx.get("frontmost_app", True))
SHOW_BATTERY = bool(_ctx.get("battery", True))

TIMEOUT = 0.6
POWER_SUPPLY = Path("/sys/class/power_supply")


def _run(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=TIMEOUT)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def time_of_day(now=None):
    now = now or datetime.now()
    hour = now.hour
    part = ("the middle of the night" if hour < 5 else
            "early morning" if hour < 8 else
            "morning" if hour < 12 else
            "afternoon" if hour < 17 else
            "evening" if hour < 22 else
            "late evening")
    return f"{now.strftime('%A')} {part}, {now.strftime('%H:%M')}"


def frontmost_app():
    if sys.platform != "darwin":
        front = session.front_window()
        return front[0] if front else ""
    asn = _run(["lsappinfo", "front"])
    if not asn:
        return ""
    raw = _run(["lsappinfo", "info", "-only", "name", asn])
    if "=" not in raw:
        return ""
    return raw.rsplit("=", 1)[-1].strip().strip('"')


def battery_state(percent, status):
    """What is worth saying about a Linux battery, or "" if nothing."""
    if status == "Charging":
        return "on charge" if percent < 95 else ""
    if status != "Discharging":
        return ""
    if percent <= 15:
        return f"on battery, down to {percent}% and not charging"
    if percent <= 35:
        return f"on battery, {percent}% left"
    return ""


def _linux_battery():
    for supply in sorted(POWER_SUPPLY.glob("BAT*")):
        try:
            percent = int((supply / "capacity").read_text())
            status = (supply / "status").read_text().strip()
        except (OSError, ValueError):
            continue
        return battery_state(percent, status)
    return ""


def battery():
    if sys.platform != "darwin":
        return _linux_battery()
    raw = _run(["pmset", "-g", "batt"])
    if not raw:
        return ""
    charging = "AC Power" in raw
    percent = ""
    for token in raw.replace(";", " ").split():
        if token.endswith("%"):
            percent = token.rstrip("%;")
            break
    if not percent:
        return ""
    level = int(percent)
    if charging:
        return "on charge" if level < 95 else ""
    if level <= 15:
        return f"on battery, down to {level}% and not charging"
    if level <= 35:
        return f"on battery, {level}% left"
    return ""


def as_note():
    """The same facts, exact, to ride along on the message being answered,
    where they can change every minute without upsetting the cached prompt.
    NOTE_EXPLAINED goes in the system prompt so she knows what it is."""
    if not ENABLED:
        return ""
    now = datetime.now()
    bits = [f"{now:%A} {now.day} {now:%B}, {now:%H:%M}"] if SHOW_TIME else []
    if SHOW_APP:
        app = frontmost_app()
        if app:
            bits.append(f"they are in {app}")
    if SHOW_BATTERY:
        state = battery()
        if state:
            bits.append(f"the laptop is {state}")
    return f"\n\n<context>{'; '.join(bits)}</context>" if bits else ""


NOTE_EXPLAINED = (
    "\n\nTheir latest message may end with a <context> tag. The app adds it, "
    "not them: it is the real date and time right now, and which app they "
    "have in front. Trust it over anything else, so when they ask the time, "
    "tell them exactly. It says nothing about what is in that app, so never "
    "guess at that. Never mention the tag or quote it, and don't bring up "
    "the day, date or time yourself unless they ask or it really matters to "
    "what you're saying: people don't keep saying what day it is."
)


def as_prompt_block():
    if not ENABLED:
        return ""

    bits = []
    if SHOW_TIME:
        bits.append(f"it is {time_of_day()}")
    if SHOW_APP:
        app = frontmost_app()
        if app:
            bits.append(f"they are in {app}")
    if SHOW_BATTERY:
        state = battery()
        if state:
            bits.append(f"the laptop is {state}")

    if not bits:
        return ""
    return (
        "\n\nAround you right now: " + "; ".join(bits) + ". "
        "Use this only if it is actually relevant. Do not read it back to "
        "them or comment on it for its own sake."
    )
