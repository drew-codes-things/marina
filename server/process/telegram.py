"""Texting her over Telegram.

Turned on by `telegram.enabled` with a bot token from @BotFather. It long
polls, so nothing has to be reachable from the internet. Only
`telegram.owner_chat_id` can talk to her; anyone else, including you before
that is set, is told their chat id and nothing more.

Run it on one always-on bridge only. Two bridges polling the same bot steal
each other's messages.
"""
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
from process.llm_funcs.llm_scr import llm_stream, reset_history, save_turn
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
LIMIT = 4000

TEXTING = """

Right now you are not speaking out loud and there is no avatar: you are
texting them on your phone. Everything above about your voice, the avatar
and *actions* does not apply here. Text like a real person does:
- Mostly lowercase, light punctuation, no full stops at the end of a text.
- Short. If you have more than one thought, put a blank line between them;
  each part arrives as its own message, like double-texting.
- No *actions* in asterisks. A real person doesn't narrate themselves; if
  you want to react, write it: "lmao", "ugh", "wait what".
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


def _call(method, timeout=30, **params):
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
        print(f"[telegram] {method}: {body.get('description')}", flush=True)
        return None
    return body.get("result")


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
                     parse_mode="HTML", disable_web_page_preview="true")
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


def _generate(user_text, extra):
    return "".join(llm_stream(user_text, extra_system=TEXTING + extra)).strip()


def _reply(chat, user_text, voice, synthesize):
    global _last_text_at
    with _turn:
        try:
            with _Typing(chat):
                reply = _generate(user_text, VOICE_NOTE if voice else "")
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
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}",
                         timeout=(5, 60))
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
            updates = r.json().get("result") or []
        except (requests.RequestException, ValueError) as e:
            # A dropped long poll is routine on home networks; start a clean
            # connection and carry on.
            if not isinstance(e, requests.ReadTimeout):
                print(f"[telegram] polling failed ({type(e).__name__}), retrying", flush=True)
            _poll_session.close()
            time.sleep(2)
            continue
        for update in updates:
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
            reply = _generate(None, extra)
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
