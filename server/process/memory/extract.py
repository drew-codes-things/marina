"""Pull facts about the user out of each exchange, on a background thread."""
import json
import re
import threading
from datetime import datetime

from process.memory import store

PROMPT = """Extract durable facts the USER stated about themselves.

RECORD when the user states something about themselves that stays true:
hardware they own, projects they are building, where they live or work, what
they prefer to be called, ongoing constraints.

DO NOT RECORD:
- anything you inferred rather than heard. If they did not say it, it does
  not go in. Never guess name, gender, age, location or job.
- anything about you, the assistant, or anything from your own reply
- passing context: questions, requests, opinions, greetings, thanks
- file names, model names or app names treated as personal details

Write each fact as one short sentence starting with "The user". Turn
relative dates into real ones using today's date, so "my birthday was
yesterday" is stored as the actual day, never as "yesterday".
Reply with ONLY a JSON array of strings. No prose, no markdown, no fences.

user: "my beelink has 32 gigs of ram"
["The user has a Beelink with 32 GB of RAM"]

user: "i'm building a vtuber assistant"
["The user is building a VTuber assistant"]

user: "it was my birthday yesterday" (today is Monday 12 October 2026)
["The user's birthday is on 11 October"]

user: "call me Sam"
["The user prefers to be called Sam"]

user: "what's the weather like"
[]

user: "thanks, that works"
[]"""

MAX_PER_TURN = 3
MAX_FACT_CHARS = 160

MIN_USER_CHARS = 15

_TRIVIAL = {
    "hi", "hey", "hello", "yo", "sup", "thanks", "thank you", "ok", "okay",
    "yes", "no", "yeah", "nah", "cool", "nice", "lol", "bye", "goodnight",
    "good morning", "what", "huh", "why", "how", "sure", "please", "stop",
}


def worth_extracting(user_text):
    text = (user_text or "").strip().lower().rstrip("?!.,")
    if len(text) < MIN_USER_CHARS:
        return False
    if text in _TRIVIAL:
        return False
    return True


def _parse(raw):
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()

    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        item = " ".join(item.split())
        if 4 <= len(item) <= MAX_FACT_CHARS:
            out.append(item)
    return out[:MAX_PER_TURN]


def extract(client, model, user_text, assistant_text):
    try:
        from process.llm_funcs.llm_scr import chat_completion
        completion = chat_completion(
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user",
                 "content": f"TODAY IS {datetime.now():%A %-d %B %Y}.\n\n"
                            f"USER SAID: {user_text}\n\nYOU REPLIED: {assistant_text}"},
            ],
            temperature=0,
            max_tokens=200,
            stream=False,
        )
        return _parse(completion.choices[0].message.content)
    except Exception as e:
        print(f"[memory] extraction failed: {type(e).__name__}: {e}", flush=True)
        return []


_EXPLICIT = [
    re.compile(r"\b(?:please\s+)?remember\s*[:,]?\s*(?:that\s+|this\s*[:,]?\s*)?(.+)", re.I),
    re.compile(r"\bdon'?t\s+forget\s*[:,]?\s*(?:that\s+)?(.+)", re.I),
    re.compile(r"\bmake\s+a\s+note\s*[:,]?\s*(?:that\s+|of\s+)?(.+)", re.I),
    re.compile(r"\bkeep\s+in\s+mind\s*[:,]?\s*(?:that\s+)?(.+)", re.I),
]

_IRREGULAR = {"have": "has", "am": "is", "'m": "is", "do": "does", "go": "goes"}
_VERBS = {
    "live", "work", "prefer", "use", "own", "need", "want", "run", "like",
    "hate", "have", "build", "study", "play", "speak", "drive", "code",
    "stream", "sleep", "eat", "am", "do", "go",
}
_ADVERBS = {"only", "just", "also", "currently", "usually", "always", "never",
            "sometimes", "still", "mainly", "mostly", "often", "really"}


def _conjugate(word):
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    return w + "s"


def _to_third_person(fact):
    """"I live in Leeds" -> "The user lives in Leeds"."""
    fact = re.sub(r"^i\s+am\b", "the user is", fact, flags=re.I)
    fact = re.sub(r"^i'?m\b", "the user is", fact, flags=re.I)
    fact = re.sub(r"^my\b", "the user's", fact, flags=re.I)
    fact = re.sub(r"^me\b", "the user", fact, flags=re.I)
    fact = re.sub(r"^i\s+", "the user ", fact, flags=re.I)

    parts = fact.split()
    if len(parts) >= 3 and parts[0].lower() == "the" and parts[1].lower() == "user":
        i = 2
        while i < len(parts) and parts[i].lower() in _ADVERBS:
            i += 1
        if i < len(parts) and parts[i].lower() in _VERBS:
            parts[i] = _conjugate(parts[i])
            fact = " ".join(parts)
    return fact


def explicit_fact(user_text):
    """The fact from "remember that ...", or None."""
    text = " ".join((user_text or "").split())
    for pattern in _EXPLICIT:
        m = pattern.search(text)
        if not m:
            continue
        fact = m.group(1).strip(" .!?,")
        if len(fact) < 3:
            continue
        fact = _to_third_person(fact)
        return fact[0].upper() + fact[1:]
    return None


def remember_async(client, model, user_text, assistant_text):
    direct = explicit_fact(user_text)
    if direct:
        fact = store.add(direct, source="explicit")
        print(f"[memory] +{direct}" if fact else f"[memory] (already knew) {direct}", flush=True)
        return

    if not worth_extracting(user_text):
        return

    def work():
        for text in extract(client, model, user_text, assistant_text):
            fact = store.add(text, source="auto")
            if fact:
                print(f"[memory] +{fact['text']}", flush=True)

    threading.Thread(target=work, daemon=True).start()
