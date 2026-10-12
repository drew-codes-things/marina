"""With a shared home, memory and the personal conversation go there and a
local copy is kept; without one, or when it is down, they stay local. A
presentation never touches the shared conversation."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process import config, shared
from process.llm_funcs import llm_scr
from process.memory import store


class FakeHome:
    def __init__(self, up=True):
        self.up = up
        self.facts = []
        self.history = []
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if not self.up:
            return None
        if (method, path) == ("GET", "/shared/memory"):
            return list(self.facts)
        if (method, path) == ("POST", "/shared/memory"):
            fact = {"id": "f1", "text": body["text"]}
            self.facts.append(fact)
            return {"fact": fact}
        if (method, path) == ("GET", "/shared/history"):
            return list(self.history)
        if (method, path) == ("POST", "/shared/history"):
            self.history.extend(body["messages"])
            return {"ok": True}
        if (method, path) == ("DELETE", "/shared/history"):
            self.history.clear()
            return {"ok": True}
        return None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(llm_scr, "_history_file", lambda: str(tmp_path / "history.json"))
    monkeypatch.setattr(shared, "enabled", lambda: True)
    monkeypatch.setattr(config, "preset", lambda: "default")
    fake = FakeHome()
    monkeypatch.setattr(shared, "call", fake)
    return fake


def test_facts_go_home_and_are_copied_locally(home):
    store.add("The user has a cat named Mochi")
    assert home.facts[0]["text"] == "The user has a cat named Mochi"
    assert [f["text"] for f in store.all_facts()] == ["The user has a cat named Mochi"]
    assert [f["text"] for f in store.local_facts()] == ["The user has a cat named Mochi"]


def test_facts_fall_back_to_local_when_home_is_down(home):
    home.up = False
    store.add("The user lives in Leeds")
    assert home.facts == []
    assert [f["text"] for f in store.local_facts()] == ["The user lives in Leeds"]


def test_turns_are_appended_at_home(home):
    llm_scr.save_turn("hello", "hey")
    llm_scr.save_turn(None, "you there?")
    assert [m["content"] for m in home.history] == ["hello", "hey", "you there?"]
    assert [m["content"] for m in llm_scr.load_history()] == ["hello", "hey", "you there?"]
    # And the local copy matches, for when the home is down.
    assert [m["content"] for m in llm_scr.load_local_history()] == ["hello", "hey", "you there?"]


def test_turns_stay_local_when_home_is_down(home):
    home.up = False
    llm_scr.save_turn("hello", "hey")
    assert home.history == []
    assert [m["content"] for m in llm_scr.load_history()] == ["hello", "hey"]


def test_presentation_never_uses_the_shared_conversation(home, monkeypatch):
    monkeypatch.setattr(config, "preset", lambda: "presentation")
    llm_scr.save_turn("what can you do?", "quite a lot")
    llm_scr.load_history()
    assert not [c for c in home.calls if "history" in c[1]]


def test_reset_clears_both(home):
    llm_scr.save_turn("hello", "hey")
    llm_scr.reset_history()
    assert home.history == []
    assert llm_scr.load_local_history() == []
