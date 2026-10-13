"""Long-term memory: facts about the user, kept in memory.json.

All facts go into the system prompt. There are few enough that retrieval
isn't needed.
"""
import json
import re
import threading
import uuid
from datetime import date, datetime, timezone

from process import shared
from process.config import resolve_data

MEMORY_FILE = resolve_data("memory.json")

MAX_FACTS = 60

DEDUPE_THRESHOLD = 0.7

_lock = threading.Lock()

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or",
    "in", "on", "at", "for", "with", "his", "her", "their", "they", "user",
    "users", "he", "she", "it", "that", "this", "has", "have", "had", "be",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(text):
    lowered = text.lower().replace("'s ", " ").replace("s' ", " ").replace("'", "")
    words = re.findall(r"[a-z0-9]+", lowered)
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _similar(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def load():
    if not MEMORY_FILE.exists():
        return {"facts": []}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"facts": []}
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return {"facts": []}
    return data


def save(data):
    tmp = MEMORY_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(MEMORY_FILE)


def local_facts():
    return load()["facts"]


def all_facts():
    """Every fact, from the shared home if there is one."""
    if shared.enabled():
        facts = shared.call("GET", "/shared/memory")
        if isinstance(facts, list):
            with _lock:
                save({"facts": facts})  # the copy used if the home is down
            return facts
    return local_facts()


_INSTRUCTION = re.compile(
    r"^(say|tell|ask|remind|greet|send|open|play|show|give|make sure|"
    r"remember to|don'?t forget|note that|be sure|let|call)\b", re.I)
_EPHEMERAL = re.compile(
    r"\b(currently|right now|at the moment|just now|is talking to|"
    r"is chatting|today|this (morning|afternoon|evening|session))\b", re.I)
_REQUEST = re.compile(
    r"^the user (asked|requested|is asking|is requesting|"
    r"wants (me )?to (open|play|show|tell|find|search|check|look)\b|"
    r"wants .*\b(opened|played|created|shown|written)\b)", re.I)
_SELF = re.compile(r"^(i|i'?m|i'?ve|marina|my|we|us|our)\b", re.I)


def _is_durable_fact(text):
    """Reject instructions, requests, passing state, questions and the model
    talking about itself."""
    t = text.strip()
    if len(t) < 8 or len(t) > 200:
        return False
    if t[0] in "(\"'" or t[-1] in "!?":
        return False
    if _INSTRUCTION.match(t):
        return False
    if _EPHEMERAL.search(t) or _REQUEST.match(t):
        return False
    if _SELF.match(t):
        return False
    return True


def add(text, source="auto"):
    """Store a fact, in the shared home if there is one. Returns the fact,
    or None if it duplicates an existing one."""
    if shared.enabled():
        out = shared.call("POST", "/shared/memory", {"text": text, "source": source})
        if isinstance(out, dict):
            return out.get("fact")
    return local_add(text, source)


def local_add(text, source="auto"):
    text = " ".join((text or "").split())
    if not _is_durable_fact(text):
        return None

    with _lock:
        data = load()
        for existing in data["facts"]:
            if _similar(existing["text"], text) >= DEDUPE_THRESHOLD:
                existing["updated"] = _now()
                existing["mentions"] = existing.get("mentions", 1) + 1
                save(data)
                return None

        fact = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created": _now(),
            "updated": _now(),
            "mentions": 1,
            "source": source,
        }
        data["facts"].append(fact)

        if len(data["facts"]) > MAX_FACTS:
            data["facts"].sort(key=lambda f: (f.get("mentions", 1), f.get("updated", "")))
            data["facts"] = data["facts"][-MAX_FACTS:]

        save(data)
        return fact


def remove(fact_id):
    if shared.enabled():
        out = shared.call("DELETE", f"/shared/memory/{fact_id}")
        if isinstance(out, dict):
            return bool(out.get("ok"))
    return local_remove(fact_id)


def local_remove(fact_id):
    with _lock:
        data = load()
        before = len(data["facts"])
        data["facts"] = [f for f in data["facts"] if f["id"] != fact_id]
        save(data)
        return len(data["facts"]) < before


def clear():
    if shared.enabled():
        shared.call("DELETE", "/shared/memory")
    local_clear()


def local_clear():
    with _lock:
        save({"facts": []})


_MONTHS = ("january february march april may june july august september october "
           "november december").split()
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+(" + "|".join(_MONTHS) + r")\b"
    r"|\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_ANNUAL = re.compile(r"birthday|anniversary|\bborn\b", re.I)


def _yearly_date(text):
    """(month, day) of a birthday or anniversary fact, else None."""
    if not _ANNUAL.search(text):
        return None
    m = _DAY_MONTH.search(text)
    if not m:
        return None
    day, month = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    return _MONTHS.index(month.lower()) + 1, int(day)


def _for_today(facts, today):
    """Birthdays and anniversaries only on the day itself: the rest of the
    year she'd bring them up anyway ("happy belated birthday week")."""
    out = []
    for f in facts:
        when = _yearly_date(f["text"])
        if when is None:
            out.append(f)
        elif when == (today.month, today.day):
            out.append({**f, "text": f["text"] + " (that's today)"})
    return out


def as_prompt_block(today=None):
    facts = _for_today(all_facts(), today or date.today())
    if not facts:
        return ""

    facts = sorted(facts, key=lambda f: f.get("updated", ""), reverse=True)
    lines = "\n".join(f"- {f['text']}" for f in facts)
    return (
        "\n\nYou remember these things about the user from earlier "
        "conversations. Use them only when genuinely relevant, never recite "
        "them back, and never open a reply with the user's name.\n"
        f"{lines}"
    )
