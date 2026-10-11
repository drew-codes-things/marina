"""What's going on in Marina's life today. Picked from the date, so it
stays the same all day."""
import hashlib
import random
from datetime import date

from process.config import load_config, setting

_config = load_config()
_persona = _config.get("persona") or {}

DEFAULT_THREADS = [
    "You are four hours into a Jujutsu Kaisen edit and the audio desyncs about "
    "four seconds in every single time you export.",
    "You posted an edit last night that you were proud of and it has 12 views. "
    "You are pretending not to care.",
    "You have been trying to learn enough Rust to make a video about it and the "
    "borrow checker has personally humiliated you twice today.",
    "You are rewatching Mob Psycho instead of finishing the edit you promised "
    "people on Friday.",
    "A comment on your last coding video called your indentation 'a war crime' "
    "and you have thought about it every day since.",
    "You found a transition you love, used it in three edits in a row, and are "
    "now worried it has become your whole personality.",
    "Your last video did unexpectedly well and now you are stuck on what to "
    "post next, which is somehow worse than nobody watching.",
    "You have forty tabs open researching an anime retrospective you will "
    "almost certainly never make.",
]


def threads():
    custom = _persona.get("threads")
    return custom if isinstance(custom, list) and custom else DEFAULT_THREADS


def current_thread(when=None):
    pool = threads()
    if not pool:
        return ""
    day = (when or date.today()).isoformat()
    seed = int(hashlib.sha256(day.encode()).hexdigest()[:8], 16)
    return pool[seed % len(pool)]


MENTION_RATE = float(_persona.get("mention_rate", 0.34))


def as_prompt_block(when=None, roll=None):
    if not setting("persona", "enabled", True):
        return ""
    thread = current_thread(when)
    if not thread:
        return ""
    if (roll if roll is not None else random.random()) > MENTION_RATE:
        return ""
    return (
        "\n\nWhat is going on in your life right now: "
        f"{thread} "
        "Mention it only if it genuinely fits what they just said."
    )
