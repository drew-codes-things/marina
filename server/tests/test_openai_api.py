"""The /v1/ API should hand a chat UI's thread to her as its own history,
stream her reply in OpenAI's format, and pass marina-plain straight to the
model."""
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process import openai_api
from process.llm_funcs import llm_scr

seen = {}


def fake_stream(user_input=None, extra_system=None, use_tools=True, history=None):
    seen.update(user=user_input, extra=extra_system, history=history)
    yield "hey "
    yield "you"


def client(monkeypatch):
    monkeypatch.setattr(llm_scr, "llm_stream", fake_stream)
    monkeypatch.setattr(openai_api, "_remember", lambda *a: None)
    app = FastAPI()
    openai_api.mount(app)
    return TestClient(app)


THREAD = [
    {"role": "system", "content": "Answer in British English."},
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "reply"},
    {"role": "user", "content": "second"},
]


def test_models_lists_her_and_plain(monkeypatch):
    ids = [m["id"] for m in client(monkeypatch).get("/v1/models").json()["data"]]
    assert ids == ["marina", "marina-plain"]


def test_thread_becomes_her_history(monkeypatch):
    r = client(monkeypatch).post("/v1/chat/completions",
                                 json={"model": "marina", "messages": THREAD})
    assert r.json()["choices"][0]["message"]["content"] == "hey you"
    assert seen["user"] == "second"
    assert seen["history"] == [{"role": "user", "content": "first"},
                               {"role": "assistant", "content": "reply"}]
    assert "Answer in British English." in seen["extra"]


def test_streams_openai_chunks(monkeypatch):
    r = client(monkeypatch).post("/v1/chat/completions",
                                 json={"model": "marina", "messages": THREAD, "stream": True})
    lines = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(l) for l in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "hey you"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_last_message_must_be_theirs(monkeypatch):
    r = client(monkeypatch).post("/v1/chat/completions", json={
        "model": "marina", "messages": [{"role": "assistant", "content": "hi"}]})
    assert r.status_code == 400


def test_steps_are_shown_and_not_fed_back(monkeypatch):
    def working(user_input=None, extra_system=None, use_tools=True, history=None):
        seen.update(history=history)
        yield llm_scr.Activity("running `ls`", "vm")
        yield ""
        yield "done"

    monkeypatch.setattr(llm_scr, "llm_stream", working)
    monkeypatch.setattr(openai_api, "_remember", lambda *a: None)
    app = FastAPI()
    openai_api.mount(app)
    r = TestClient(app).post("/v1/chat/completions", json={"model": "marina", "stream": True,
        "messages": [{"role": "user", "content": "a"},
                     {"role": "assistant", "content": "> ⚙️ *on her machine:* running `pwd`\n\nok"},
                     {"role": "user", "content": "b"}]})
    text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content", "")
                   for l in r.text.splitlines() if l.startswith("data: {"))
    assert "*on her machine:* running `ls`" in text and text.endswith("done")
    assert seen["history"][1]["content"] == "ok"
