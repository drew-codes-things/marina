"""Chatting keeps her personality settings; once she is using tools she
works with steady sampling, no repetition penalty, and thinking on."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.llm_funcs import llm_scr


def test_chat_and_work_use_different_settings(monkeypatch):
    sent = []
    monkeypatch.setattr(llm_scr, "chat_completion", lambda messages, **kw: sent.append(kw))
    llm_scr.get_reply_stream([], False)
    llm_scr.get_reply_stream([], True)
    chat, work = sent
    assert chat["temperature"] == llm_scr._temperature()
    assert "extra_body" not in chat
    assert work["temperature"] < chat["temperature"]
    assert work["extra_body"]["repeat_penalty"] == 1.0
    assert work["frequency_penalty"] == 0.0
    assert work["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_work_settings_survive_the_request_defaults():
    kw = llm_scr._request_extras(llm_scr._work_settings())
    assert kw["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert kw["extra_body"]["repeat_penalty"] == 1.0
    assert kw["frequency_penalty"] == 0.0


def test_old_tool_results_are_shortened_new_ones_kept():
    long = "x" * 9000
    msgs = [{"role": "system", "content": long}, {"role": "user", "content": long}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "content": long} for i in range(6)]
    out = llm_scr._shorten_old_results(msgs)
    assert out[0]["content"] == long and out[1]["content"] == long
    assert [len(m["content"]) < 400 for m in out[2:]] == [True, True, False, False, False, False]


def test_short_jobs_are_left_alone():
    msgs = [{"role": "tool", "tool_call_id": str(i), "content": "y" * 1000} for i in range(6)]
    assert llm_scr._shorten_old_results(msgs) == msgs


def test_speaking_first_never_ends_on_her_own_message(monkeypatch):
    sent = []

    def fake(messages, working=False, **kw):
        sent.append(messages)
        return iter([])

    monkeypatch.setattr(llm_scr, "get_reply_stream", fake)
    history = [{"role": "user", "content": "night"}, {"role": "assistant", "content": "night!"},
               {"role": "assistant", "content": "you up?"}]
    list(llm_scr.llm_stream(None, use_tools=False, history=history))
    roles = [m["role"] for m in sent[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "night!" in sent[0][2]["content"] and "you up?" in sent[0][2]["content"]
