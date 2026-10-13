"""Texting her, end to end over HTTP: the real polling loop and senders run
against a local server that answers the way Telegram's Bot API documents, and
every request they make is checked against that contract (parameter names,
formats, the OGG/Opus voice note). Nothing here reaches the real service."""
import io
import json
import sys
import threading
import time
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import numpy as np
import pytest
import requests
import soundfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process import telegram

TOKEN = "123456:TEST-TOKEN"
OWNER = 4242
SECONDS_TO_WAIT = 10


class _Stop(BaseException):
    """Ends the polling thread (it isn't an Exception, so the loop lets it out)."""


class FakeTelegram:
    """What the Bot API does, as far as she uses it."""

    def __init__(self):
        self.calls = []
        self.updates = []
        self.refusals = {}
        self.voice_file = b""
        self.lock = threading.Lock()
        self.pollers = []

    def poll(self, transcribe=None, synthesize=None):
        """Run her polling loop in the background until the test ends."""
        def run():
            try:
                telegram._poll(transcribe, synthesize)
            except _Stop:
                pass
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.pollers.append(thread)

    def calls_to(self, method):
        return [params for name, params in self.calls if name == method]

    def reply_to(self, method, params):
        with self.lock:
            self.calls.append((method, params))
            if self.refusals.get(method):
                return self.refusals[method].pop(0)
            return self._answer(method, params)

    def _answer(self, method, params):
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "marina_test_bot"}}
        if method == "getUpdates":
            json.loads(params["allowed_updates"])
            offset = int(params.get("offset", 0))
            due = [u for u in self.updates if u["update_id"] >= offset]
            return {"ok": True, "result": due}
        if method == "sendChatAction":
            assert params["action"] in ("typing", "record_voice")
            return {"ok": True, "result": True}
        if method == "sendMessage":
            assert 1 <= len(params["text"]) <= 4096
            assert params.get("parse_mode") in (None, "HTML")
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        if method == "sendVoice":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        if method == "getFile":
            return {"ok": True, "result": {"file_id": params["file_id"], "file_path": "voice/file_7.oga"}}
        return {"ok": False, "error_code": 404, "description": "Not Found"}


def _parse(headers, body):
    """The request's fields, from a form post or a multipart upload."""
    kind = headers.get("Content-Type", "")
    if kind.startswith("application/x-www-form-urlencoded"):
        return {k: v[0] for k, v in parse_qs(body.decode()).items()}
    fields = {}
    message = BytesParser().parsebytes(f"Content-Type: {kind}\r\n\r\n".encode() + body)
    for part in message.get_payload():
        name = part.get_param("name", header="content-disposition")
        fields[name] = part.get_payload(decode=True)
    return fields


@pytest.fixture
def service(monkeypatch):
    fake = FakeTelegram()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload, kind="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            prefix = f"/bot{TOKEN}/"
            if not self.path.startswith(prefix):
                return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            method = self.path[len(prefix):]
            answer = fake.reply_to(method, _parse(self.headers, body))
            time.sleep(0.05)
            self._send(200, answer)

        def do_GET(self):
            if self.path == f"/file/bot{TOKEN}/voice/file_7.oga":
                return self._send(200, fake.voice_file, "audio/ogg")
            self._send(404, {"ok": False})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    stopping = threading.Event()

    class PollSession(requests.Session):
        def post(self, *args, **kwargs):
            if stopping.is_set():
                raise _Stop()
            return super().post(*args, **kwargs)

    monkeypatch.setattr(telegram, "TOKEN", TOKEN)
    monkeypatch.setattr(telegram, "OWNER", OWNER)
    monkeypatch.setattr(telegram, "API", f"{base}/bot{TOKEN}")
    monkeypatch.setattr(telegram, "FILE_API", f"{base}/file/bot{TOKEN}")
    monkeypatch.setattr(telegram, "_poll_session", PollSession())
    monkeypatch.setattr(telegram, "_cfg", {"enabled": True, "token": TOKEN})
    monkeypatch.setattr(telegram, "POLL_ERROR_PAUSE", 0.3)
    monkeypatch.setattr(telegram, "MAX_PAUSE", 0)
    monkeypatch.setattr(telegram, "load_history", lambda: [])
    monkeypatch.setattr(telegram, "save_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(telegram, "llm_stream",
                        lambda text, extra_system=None: iter(["hey, got it", "\n\nsecond bubble"]))
    yield fake
    stopping.set()
    for thread in fake.pollers:
        thread.join(timeout=5)
    server.shutdown()


def wait_for(condition):
    deadline = time.time() + SECONDS_TO_WAIT
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def a_text(update_id, text, chat=OWNER):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat},
                                               "from": {"id": chat, "username": "me"}, "text": text}}


@pytest.fixture
def no_background_threads(monkeypatch):
    """start() launches the polling and unprompted-text loops; these tests only
    look at what it asks Telegram first."""
    monkeypatch.setattr(telegram, "_poll", lambda *args: None)
    monkeypatch.setattr(telegram, "_proactive", lambda: None)


def test_start_checks_the_token_with_getMe(service, no_background_threads):
    assert telegram.start() is True
    assert service.calls_to("getMe") == [{}]


def test_a_refused_token_turns_texting_off(service, no_background_threads):
    service.refusals["getMe"] = [{"ok": False, "error_code": 401, "description": "Unauthorized"}]
    assert telegram.start() is False


def test_a_text_gets_her_reply_as_separate_messages(service):
    service.updates = [a_text(10, "hello there")]
    service.poll()
    assert wait_for(lambda: len(service.calls_to("sendMessage")) >= 2)
    sent = service.calls_to("sendMessage")
    assert [m["text"] for m in sent] == ["hey, got it", "second bubble"]
    assert all(m["chat_id"] == str(OWNER) and m["parse_mode"] == "HTML" for m in sent)
    assert json.loads(sent[0]["link_preview_options"]) == {"is_disabled": True}
    assert "disable_web_page_preview" not in sent[0]
    assert {c["action"] for c in service.calls_to("sendChatAction")} == {"typing"}


def test_updates_are_confirmed_so_a_text_is_answered_once(service):
    service.updates = [a_text(10, "hello there")]
    service.poll()
    assert wait_for(lambda: len(service.calls_to("sendMessage")) >= 2)
    assert wait_for(lambda: any(p.get("offset") == "11" for p in service.calls_to("getUpdates")))
    time.sleep(0.4)
    assert len(service.calls_to("sendMessage")) == 2


def test_a_stranger_is_only_told_their_chat_id(service):
    service.updates = [a_text(20, "hi", chat=999)]
    service.poll()
    assert wait_for(lambda: service.calls_to("sendMessage"))
    assert "Your chat id is 999" in service.calls_to("sendMessage")[0]["text"]
    assert len(service.calls_to("sendMessage")) == 1


def test_a_voice_note_is_downloaded_heard_and_answered_with_an_opus_voice_note(service):
    tone = (np.sin(np.linspace(0, 200, 24000)) * 0.3).astype("float32")
    heard = io.BytesIO()
    soundfile.write(heard, tone, 24000, format="OGG", subtype="OPUS")
    service.voice_file = heard.getvalue()
    service.updates = [{"update_id": 30, "message": {
        "message_id": 30, "chat": {"id": OWNER}, "from": {"id": OWNER},
        "voice": {"file_id": "abc", "duration": 1, "mime_type": "audio/ogg"}}}]
    received = []

    def transcribe(path):
        received.append(Path(path).read_bytes())
        return "this is a voice note"

    def synthesize(text):
        wav = io.BytesIO()
        soundfile.write(wav, tone, 24000, format="WAV")
        return wav.getvalue(), []

    service.poll(transcribe, synthesize)
    assert wait_for(lambda: service.calls_to("sendVoice"))
    assert service.calls_to("getFile") == [{"file_id": "abc"}]
    assert received and received[0].startswith(b"OggS")
    voice = service.calls_to("sendVoice")[0]
    assert voice["chat_id"] == str(OWNER).encode()
    assert voice["voice"].startswith(b"OggS") and b"OpusHead" in voice["voice"]
    assert {c["action"] for c in service.calls_to("sendChatAction")} >= {"typing", "record_voice"}


def test_polling_backs_off_when_telegram_refuses_it(service):
    """A revoked token or a webhook answers at once with ok:false; asking again
    straight away would be a tight loop against the real service."""
    service.refusals["getUpdates"] = [
        {"ok": False, "error_code": 409, "description": "Conflict: webhook is active"}] * 50
    service.poll()
    time.sleep(1.2)
    asked = len(service.calls_to("getUpdates"))
    assert 1 <= asked <= 6, f"polled {asked} times in 1.2 seconds"


def test_a_message_refused_for_coming_too_fast_is_sent_after_the_wait(service):
    service.refusals["sendMessage"] = [{"ok": False, "error_code": 429,
                                        "description": "Too Many Requests: retry after 1",
                                        "parameters": {"retry_after": 1}}]
    started = time.time()
    assert telegram._call("sendMessage", chat_id=OWNER, text="hello") is not None
    assert len(service.calls_to("sendMessage")) == 2
    assert 0.9 <= time.time() - started < 5


def test_a_very_long_wait_is_not_sat_through(service):
    service.refusals["sendMessage"] = [{"ok": False, "error_code": 429, "description": "Too Many Requests",
                                        "parameters": {"retry_after": 3600}}]
    started = time.time()
    assert telegram._call("sendMessage", chat_id=OWNER, text="hello") is None
    assert time.time() - started < 2
    assert len(service.calls_to("sendMessage")) == 1


def test_other_refusals_are_reported_not_retried(service):
    service.refusals["sendMessage"] = [{"ok": False, "error_code": 400,
                                        "description": "Bad Request: chat not found"}]
    assert telegram._call("sendMessage", chat_id=OWNER, text="hello") is None
    assert len(service.calls_to("sendMessage")) == 1
