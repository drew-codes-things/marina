"""Decides when she says something without being asked. See `idle:` in
character_config.yaml for the limits."""
import random
import threading
import time
from datetime import datetime

from process.config import load_config, setting

_config = load_config()
_idle = _config.get("idle") or {}

MIN_GAP = float(_idle.get("min_gap_minutes", 25)) * 60
MAX_PER_HOUR = int(_idle.get("max_per_hour", 2))
PROBABILITY = float(_idle.get("probability", 0.5))
QUIET_FROM = int(_idle.get("quiet_from_hour", 23))
QUIET_UNTIL = int(_idle.get("quiet_until_hour", 9))

OPENER_INSTRUCTION = (
    "\n\nNothing has been said for a while and the silence is yours to break. "
    "Say one thing, unprompted: something on your mind, something you were "
    "reminded of, or a passing thought about them. One or two sentences. "
    "Do not offer help, do not ask what they are working on, and do not "
    "greet them as if they just arrived. It must not repeat anything already "
    "said above; the conversation is in front of you, so pick something else."
)

_lock = threading.Lock()
_last_interaction = time.time()
_spoken_at = []
_muted = False


def enabled():
    """Read on every call: the presentation preset turns openers off."""
    return bool(setting("idle", "enabled", True))


def note_interaction():
    global _last_interaction
    with _lock:
        _last_interaction = time.time()


def muted():
    with _lock:
        return _muted


def set_muted(value):
    global _muted
    with _lock:
        _muted = bool(value)
    return _muted


def in_quiet_hours(now=None):
    hour = (now or datetime.now()).hour
    if QUIET_FROM == QUIET_UNTIL:
        return False
    if QUIET_FROM < QUIET_UNTIL:
        return QUIET_FROM <= hour < QUIET_UNTIL
    return hour >= QUIET_FROM or hour < QUIET_UNTIL


def note_spoken():
    now = time.time()
    with _lock:
        _spoken_at.append(now)
        _spoken_at[:] = [t for t in _spoken_at if now - t < 3600]
    note_interaction()


def _recent_count(now):
    return sum(1 for t in _spoken_at if now - t < 3600)


def status():
    now = time.time()
    with _lock:
        quiet_for = now - _last_interaction
        recent = _recent_count(now)
        is_muted = _muted
    return {
        "enabled": enabled(),
        "muted": is_muted,
        "quiet_for_seconds": round(quiet_for),
        "spoken_this_hour": recent,
        "max_per_hour": MAX_PER_HOUR,
        "in_quiet_hours": in_quiet_hours(),
    }


def due(now=None, roll=None):
    if not enabled():
        return False
    now = now or time.time()
    with _lock:
        if _muted:
            return False
        if now - _last_interaction < MIN_GAP:
            return False
        if MAX_PER_HOUR and _recent_count(now) >= MAX_PER_HOUR:
            return False
    if in_quiet_hours():
        return False
    return (roll if roll is not None else random.random()) < PROBABILITY
