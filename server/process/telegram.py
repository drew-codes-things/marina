"""Texting her over Telegram.

Turned on by `telegram.enabled` with a bot token from @BotFather. It long
polls, so nothing has to be reachable from the internet. Only
`telegram.owner_chat_id` can talk to her; anyone else, including you before
that is set, is told their chat id and nothing more.

Run it on one always-on bridge only. Two bridges polling the same bot steal
each other's messages.
"""
import difflib
import html
import io
import random
import re
import tempfile
import threading
import time
from pathlib import Path

import requests

from process import idle
from process.config import load_config, setting
from process.llm_funcs.llm_scr import llm_stream, load_history, reset_history, save_turn
from process.text_func.speech import split_reply
from process.tools import registry as tools

_cfg = load_config().get("telegram") or {}

TOKEN = str(_cfg.get("token") or "").strip()
OWNER = int(_cfg.get("owner_chat_id") or 0)
VOICE_REPLIES = bool(_cfg.get("voice_replies", True))
CHARS_PER_SECOND = float(_cfg.get("typing_chars_per_second", 22))
MAX_PAUSE = float(_cfg.get("max_pause_seconds", 5))
TEXTS_PER_DAY = int(_cfg.get("unprompted_per_day", 3))
GAP_HOURS = float(_cfg.get("unprompted_gap_hours", 3))

API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
LIMIT = 4000
MAX_RETRY_AFTER = 30
POLL_ERROR_PAUSE = 10

TEXTING = """

Right now you are not speaking out loud and there is no avatar: you are
texting them on your phone. Everything above about your voice, the avatar
and *actions* does not apply here. Text like a real person does:
- Mostly lowercase, light punctuation, no full stops at the end of a text.
- Short. If you have more than one thought, put a blank line between them;
  each part arrives as its own message, like double-texting.
- No *actions* in asterisks. A real person doesn't narrate themselves; if
  you want to react, write it: "lmao", "ugh", "wait what".
- Don't text that you're about to do something ("give me a sec", "let me
  check"). If something needs checking, check it with your tools first and
  text them what you found.
- An emoji now and then is fine if it is the one you would actually send.
- If they need to copy a command or a snippet, put it in ``` fences.
  Code you are building on your own machine goes there through your tools,
  not pasted into the chat."""

VOICE_NOTE = """

They sent you a voice note. Your reply is sent back as a voice note too, so
say it the way you'd talk, not the way you'd text: no fences, no links."""

UNPROMPTED = """

You haven't texted each other for a while and you feel like messaging them
first. Send one short text, the way you would to a friend out of nowhere:
something you just thought of, something that happened to you, a callback
to something they told you, or just checking in on a thing they mentioned.
Don't greet them formally, don't offer help, don't ask what they're up to
in a generic way, and don't repeat anything already said above."""

# Long polling gets a session of its own. Everything else opens a fresh
# connection: home routers drop idle connections without telling either
# end, and a send on a pooled connection that has quietly died just hangs
# until its timeout.
_poll_session = requests.Session()
POLL_SECONDS = 25
_turn = threading.Lock()
_last_text_at = time.time()
_unprompted_at = []


def enabled():
    return bool(_cfg.get("enabled")) and bool(TOKEN)


def _call(method, timeout=30, retried=False, **params):
    files = params.pop("files", None)
    body = None
    for attempt in range(2):
        try:
            r = requests.post(f"{API}/{method}", data=params, files=files,
                              timeout=(5, timeout), headers={"Connection": "close"})
            body = r.json()
            break
        except requests.ConnectionError as e:
            # Never reached Telegram, so trying again can't send it twice.
            if attempt == 0:
                if files:
                    for f in files.values():
                        f[1].seek(0)
                continue
            print(f"[telegram] {method} failed: {type(e).__name__}", flush=True)
            return None
        except (requests.RequestException, ValueError) as e:
            print(f"[telegram] {method} failed: {type(e).__name__}", flush=True)
            return None
    if not body.get("ok"):
        wait = _retry_after(body)
        if wait is not None and not retried:
            print(f"[telegram] {method}: asked too often, waiting {wait}s", flush=True)
            time.sleep(wait)
            if files:
                for f in files.values():
                    f[1].seek(0)
                params["files"] = files
            return _call(method, timeout, True, **params)
        print(f"[telegram] {method}: {body.get('description')}", flush=True)
        return None
    return body.get("result")


def _retry_after(body):
    """Seconds Telegram asks us to wait before repeating a request it refused
    for coming too fast, or None if that isn't why it refused (or the wait is
    too long to sit through)."""
    if body.get("error_code") != 429:
        return None
    wait = (body.get("parameters") or {}).get("retry_after")
    return wait if isinstance(wait, int) and 0 <= wait <= MAX_RETRY_AFTER else None


class _Typing:
    """Keeps "typing…" showing for as long as she is thinking."""

    def __init__(self, chat, action="typing"):
        self.chat, self.action = chat, action
        self._stop = threading.Event()

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                _call("sendChatAction", chat_id=self.chat, action=self.action)
                self._stop.wait(4.5)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


_FENCE = re.compile(r"```[^\n]*\n?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")


def _to_html(text):
    """Telegram HTML for a bubble: code fences and `inline code`, the rest
    escaped as plain text."""
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out.append(_INLINE.sub(lambda c: f"<code>{c.group(1)}</code>",
                               html.escape(text[pos:m.start()])))
        out.append(f"<pre>{html.escape(m.group(1).strip(chr(10)))}</pre>")
        pos = m.end()
    out.append(_INLINE.sub(lambda c: f"<code>{c.group(1)}</code>",
                           html.escape(text[pos:])))
    return "".join(out)


def _bubbles(reply):
    """Split a reply into separate texts on blank lines, keeping code
    fences whole."""
    parts, buf, in_fence = [], [], False
    for line in reply.strip().splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if buf:
                parts.append("\n".join(buf).strip())
                buf = []
            continue
        buf.append(line)
    if buf:
        parts.append("\n".join(buf).strip())
    out = []
    for p in (p for p in parts if p):
        while len(p) > LIMIT:
            cut = p.rfind("\n", 0, LIMIT)
            cut = cut if cut > LIMIT // 2 else LIMIT
            out.append(p[:cut])
            p = p[cut:].lstrip("\n")
        out.append(p)
    return out


def _send_text(chat, reply):
    bubbles = _bubbles(reply)
    for i, bubble in enumerate(bubbles):
        if i:
            # The pause a person takes to type the next one.
            pause = min(MAX_PAUSE, 0.6 + len(bubble) / CHARS_PER_SECOND)
            with _Typing(chat):
                time.sleep(pause * random.uniform(0.7, 1.1))
        sent = _call("sendMessage", chat_id=chat, text=_to_html(bubble),
                     parse_mode="HTML", link_preview_options='{"is_disabled": true}')
        if sent is None:
            _call("sendMessage", chat_id=chat, text=bubble)


def _send_voice(chat, reply, synthesize):
    """Her reply as a Telegram voice note. Falls back to text."""
    speech = split_reply(reply)["speech"]
    if not speech:
        return False
    try:
        import soundfile as sf

        with _Typing(chat, "record_voice"):
            wav, _visemes = synthesize(speech)
            data, rate = sf.read(io.BytesIO(wav))
            ogg = io.BytesIO()
            sf.write(ogg, data, rate, format="OGG", subtype="OPUS")
    except Exception as e:
        print(f"[telegram] voice note failed: {type(e).__name__}: {e}", flush=True)
        return False
    ogg.seek(0)
    return _call("sendVoice", timeout=60, chat_id=chat,
                 files={"voice": ("marina.ogg", ogg, "audio/ogg")}) is not None


# How far back she checks herself for repeats, how alike two texts have to
# be to count as the same one, and how many words in a row make a phrase.
RECENT_REPLIES = 8
# A whole text sent again is checked much further back than a phrase: she
# resent a day-old "just checking in" text word for word.
SAME_TEXT_REPLIES = 60
SAME_TEXT = 0.75
PHRASE_WORDS = 5

AGAIN = """

(Note from the app, not from them. Never mention it. Your texts have been
falling into a groove and this one did it again: {phrases}. Answer what
they just said, as yourself, but don't use those words again.)"""


def _generate(user_text, extra, note=""):
    """Her reply, and whether she used any tools on the way."""
    if user_text is None:
        pieces = list(llm_stream(None, extra_system=TEXTING + extra + note))
    else:
        pieces = list(llm_stream(user_text + note, extra_system=TEXTING + extra))
    # Text only ever arrives in non-empty pieces; the empty ones mark tool use.
    used_tools = any(p == "" for p in pieces)
    if used_tools:
        # What she said before and between tool calls ("let me check… give
        # me a sec") is narration; the text after the last one is her answer.
        last = max(i for i, p in enumerate(pieces) if p == "")
        after = "".join(pieces[last + 1:]).strip()
        if after:
            pieces = [after]
    return _without_actions("".join(pieces)).strip(), used_tools


_ACTION = re.compile(r"(?<![*\w])\*(?!\s)[^*\n`]{1,60}(?<!\s)\*(?![*\w])")


def _without_actions(text):
    """Drop *tilts head* style stage directions, which she still slips in
    now and then; code fences are left alone."""
    out, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            stripped = _ACTION.sub("", line)
            if stripped != line:
                line = re.sub(r"  +", " ", stripped).strip()
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("\u2019", "'"))


def _recent(role, n):
    try:
        history = load_history()
    except Exception:
        return []
    return [m["content"] for m in history
            if m.get("role") == role and isinstance(m.get("content"), str)][-n:]


def _recent_replies():
    return _recent("assistant", RECENT_REPLIES)


# The day and time. She has the clock on every message and kept telling
# them it was Tuesday; fine when they bring it up, not otherwise.
_WEEKDAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MONTH = (r"january|february|march|april|may|june|july|august|september|october"
          r"|november|december")
_SAYS_WHEN = re.compile(
    rf"\b(?:{_WEEKDAY})s?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH})\b"
    rf"|\b(?:{_MONTH})\s+\d{{1,2}}\b|\b\d{{1,2}}:\d{{2}}\b|\b\d{{1,2}}\s?(?:am|pm)\b"
    r"|\bwhat (?:day|time) it is\b", re.I)
_ASKS_WHEN = re.compile(
    rf"\b(?:{_WEEKDAY}|{_MONTH}|day|date|time|today|tonight|tomorrow|yesterday|week"
    r"|weekend|morning|afternoon|evening|night|clock|hour|when|late|early)\b", re.I)


def _mentions_when(reply, user_text):
    """The day, date or time she brought up without them asking."""
    if user_text and _ASKS_WHEN.search(user_text):
        return None
    m = _SAYS_WHEN.search(reply or "")
    return m.group(0) if m else None


# A topic she won't let go of: a word in this text and in most of her
# recent ones that they've stopped mentioning. Common words never count.
_COMMON = set("""
about above after again against almost already also always another anyone anything
around because before being below between could doing during either enough every
everything first going gonna gotta great guess having honestly later little maybe
might never nothing other really right should since something still sure thing
things think though through today together tonight trying until using wanna where
which while would yeah yours actually basically literally probably definitely
pretty kinda sorta stuff looks looking feels feeling getting making taking
morning afternoon evening night totally ok okay sounds running check
checking setup start started working work done ready happy hope nice
""".split())


def _stuck_topics(reply, recent, their_recent):
    mine = [set(_words(t)) for t in recent[-3:]]
    if len(mine) < 2:
        return []
    theirs = set().union(*(set(_words(t)) for t in their_recent)) if their_recent else set()
    out = []
    for word in sorted(set(_words(reply))):
        if len(word) < 5 or word in _COMMON or word in theirs or "'" in word:
            continue
        if sum(word in earlier for earlier in mine) >= 2:
            out.append(word)
    return out


def _repeats(reply, recent, older=()):
    """What in this reply she has already been saying: a whole text she
    sent before (looked for in `older` too), or a phrase from one of the
    `recent` ones, or a phrase she loops within it."""
    words = _words(reply)
    if len(words) < PHRASE_WORDS:
        return []  # "lol" twice is just texting
    text = " ".join(words)
    found = []
    for earlier in map(_words, older):
        if difflib.SequenceMatcher(None, text, " ".join(earlier),
                                   autojunk=False).ratio() >= SAME_TEXT:
            found.append(" ".join(earlier[:12]))
    for earlier in map(_words, recent):
        if difflib.SequenceMatcher(None, text, " ".join(earlier),
                                   autojunk=False).ratio() >= SAME_TEXT:
            found.append(" ".join(earlier[:12]))
            continue
        blocks = difflib.SequenceMatcher(None, words, earlier, autojunk=False)
        for b in blocks.get_matching_blocks():
            if b.size >= PHRASE_WORDS:
                found.append(" ".join(words[b.a:b.a + min(b.size, 12)]))
    grams = [" ".join(words[i:i + PHRASE_WORDS]) for i in range(len(words) - PHRASE_WORDS + 1)]
    found += [g for i, g in enumerate(grams) if g in grams[:i]]
    unique = []
    for phrase in sorted(set(found), key=len, reverse=True):
        if not any(phrase in kept for kept in unique):
            unique.append(phrase)
    return unique


AGAIN_WHEN = """

(Note from the app, not from them. Never mention it. You brought up {when}
without them asking. Don't mention the day, date or time this time.)"""

AGAIN_TOPIC = """

(Note from the app, not from them. Never mention it. You keep coming back to
{topics}, which they've moved on from. Leave it out this time and answer
what they actually said.)"""


def _problems(reply, user_text, recent, their_recent, older=()):
    """What's wrong with a draft text: phrases she keeps reusing, the day
    or time unprompted, and a topic she won't drop."""
    return {"worn": _repeats(reply, recent, older),
            "when": _mentions_when(reply, user_text),
            "topics": _stuck_topics(reply, recent, their_recent)}


def _count(problems):
    return len(problems["worn"]) + bool(problems["when"]) + len(problems["topics"])


def _compose(user_text, extra):
    """Her reply, written once more if it falls into one of her grooves.
    A small model repeats itself: the same text to two different messages,
    one line in everything, the day of the week in every text, a topic it
    won't let go. Asking again with the problem named gets her out of it
    without changing how she talks."""
    recent = _recent_replies()
    older = _recent("assistant", SAME_TEXT_REPLIES)[:-RECENT_REPLIES]
    their_recent = _recent("user", 2) + ([user_text] if user_text else [])
    reply, used_tools = _generate(user_text, extra)
    found = _problems(reply, user_text, recent, their_recent, older)
    # Asking again would run her tools twice, so a reply that did things stands.
    if not _count(found) or used_tools:
        return reply
    notes = []
    if found["worn"]:
        print(f"[telegram] she repeated herself ({'; '.join(found['worn'])}), asking again",
              flush=True)
        notes.append(AGAIN.format(phrases="; ".join(f'"{p}"' for p in found["worn"])))
    if found["when"]:
        print(f"[telegram] she brought up the time unasked ({found['when']}), asking again",
              flush=True)
        notes.append(AGAIN_WHEN.format(when=f'"{found["when"]}"'))
    if found["topics"]:
        print(f"[telegram] she won't let go of {', '.join(found['topics'])}, asking again",
              flush=True)
        notes.append(AGAIN_TOPIC.format(topics=", ".join(f'"{t}"' for t in found["topics"])))
    retry, _ = _generate(user_text, extra, "".join(notes))
    if not retry or _count(_problems(retry, user_text, recent, their_recent, older)) >= _count(found):
        return reply
    return retry


def _reply(chat, user_text, voice, synthesize):
    global _last_text_at
    with _turn:
        try:
            with _Typing(chat):
                reply = _compose(user_text, VOICE_NOTE if voice else "")
        except Exception as e:
            print(f"[telegram] reply failed: {type(e).__name__}: {e}", flush=True)
            _call("sendMessage", chat_id=chat,
                  text="(my brain isn't answering right now, try again in a bit)")
            return
        if not reply:
            return
        save_turn(user_text, reply)
        idle.note_interaction()
        _last_text_at = time.time()
        print(f"[telegram] marina: {reply}", flush=True)
        if voice and synthesize and "```" not in reply and _send_voice(chat, reply, synthesize):
            return
        _send_text(chat, reply)


def _transcribe_voice(message, transcribe):
    voice = message.get("voice") or message.get("audio") or {}
    info = _call("getFile", file_id=voice.get("file_id"))
    if not info or not transcribe:
        return ""
    try:
        r = requests.get(f"{FILE_API}/{info['file_path']}", timeout=(5, 60))
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[telegram] could not fetch voice note: {type(e).__name__}", flush=True)
        return ""
    with tempfile.NamedTemporaryFile(suffix=Path(info["file_path"]).suffix or ".ogg",
                                     delete=False) as tmp:
        tmp.write(r.content)
    path = Path(tmp.name)
    try:
        return transcribe(path)
    finally:
        path.unlink(missing_ok=True)


def _command(chat, text):
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/reset":
        reset_history()
        _call("sendMessage", chat_id=chat, text="(conversation cleared)")
    elif cmd == "/quiet":
        idle.set_muted(True)
        _call("sendMessage", chat_id=chat, text="(she won't text first until /unquiet)")
    elif cmd == "/unquiet":
        idle.set_muted(False)
        _call("sendMessage", chat_id=chat, text="(she can text first again)")
    else:
        return False
    return True


def _handle(message, transcribe, synthesize):
    chat = (message.get("chat") or {}).get("id")
    if not chat:
        return
    if chat != OWNER:
        who = (message.get("from") or {}).get("username") or "someone"
        print(f"[telegram] ignored a message from {who} (chat id {chat})", flush=True)
        hint = ("Put this in telegram.owner_chat_id to pair with her."
                if not OWNER else "This is a private bot.")
        _call("sendMessage", chat_id=chat, text=f"Your chat id is {chat}. {hint}")
        return

    text = (message.get("text") or "").strip()
    voice = bool(message.get("voice") or message.get("audio"))
    if voice:
        with _Typing(chat):
            text = _transcribe_voice(message, transcribe)
        if not text:
            _call("sendMessage", chat_id=chat, text="(couldn't make out that voice note)")
            return
    if not text:
        caption = (message.get("caption") or "").strip()
        if not caption:
            return
        text = f"[sent a photo or file you can't open] {caption}"
    if text.startswith("/") and _command(chat, text):
        return
    if text == "/start":
        text = "hey"
    print(f"[telegram] you{' (voice)' if voice else ''}: {text}", flush=True)
    _reply(chat, text, voice, synthesize)


def _poll(transcribe, synthesize):
    offset = None
    while True:
        params = {"timeout": POLL_SECONDS, "allowed_updates": '["message"]'}
        if offset:
            params["offset"] = offset
        try:
            r = _poll_session.post(f"{API}/getUpdates", data=params,
                                   timeout=(5, POLL_SECONDS + 10))
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            # A dropped long poll is routine on home networks; start a clean
            # connection and carry on.
            if not isinstance(e, requests.ReadTimeout):
                print(f"[telegram] polling failed ({type(e).__name__}), retrying", flush=True)
            _poll_session.close()
            time.sleep(2)
            continue
        if not body.get("ok"):
            print(f"[telegram] getUpdates: {body.get('description')}", flush=True)
            time.sleep(_retry_after(body) or POLL_ERROR_PAUSE)
            continue
        for update in body.get("result") or []:
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            try:
                _handle(message, transcribe, synthesize)
            except Exception as e:
                print(f"[telegram] handling a message failed: {type(e).__name__}: {e}",
                      flush=True)


def _unprompted_due(now):
    if not OWNER or TEXTS_PER_DAY <= 0 or idle.muted() or idle.in_quiet_hours():
        return False
    if not bool(setting("idle", "enabled", True)):
        return False
    _unprompted_at[:] = [t for t in _unprompted_at if now - t < 86400]
    if len(_unprompted_at) >= TEXTS_PER_DAY:
        return False
    last = max([_last_text_at] + _unprompted_at)
    if now - last < GAP_HOURS * 3600:
        return False
    # Checked every few minutes, so this spreads the texts out over the
    # day rather than sending one the moment the gap is up.
    return random.random() < 0.06


def _send_unprompted(extra, record_as=None):
    global _last_text_at
    with _turn:
        try:
            reply = _compose(None, extra)
        except Exception as e:
            print(f"[telegram] unprompted text failed: {type(e).__name__}: {e}", flush=True)
            return
        if not reply:
            return
        save_turn(record_as, reply)
        _last_text_at = time.time()
        print(f"[telegram] marina (unprompted): {reply}", flush=True)
        _send_text(OWNER, reply)


def _proactive():
    while True:
        time.sleep(20)
        if not OWNER:
            continue
        announcement = tools.pending_announcement()
        if announcement:
            _send_unprompted("\n\n" + announcement)
            continue
        now = time.time()
        if int(now) % 300 < 20 and _unprompted_due(now):
            _unprompted_at.append(now)
            _send_unprompted(UNPROMPTED)


def start(transcribe=None, synthesize=None):
    """Start polling in the background. `transcribe(path) -> str` and
    `synthesize(text) -> (wav bytes, visemes)` come from the bridge."""
    if not enabled():
        return False
    me = _call("getMe", timeout=15)
    if not me:
        print("[telegram] the bot token was refused, so texting is off", flush=True)
        return False
    print(f"[telegram] texting as @{me.get('username')}"
          f"{'' if OWNER else ' (not paired yet: message the bot to get your chat id)'}",
          flush=True)
    threading.Thread(target=_poll, args=(transcribe, synthesize if VOICE_REPLIES else None),
                     daemon=True, name="telegram-poll").start()
    threading.Thread(target=_proactive, daemon=True, name="telegram-first").start()
    return True
