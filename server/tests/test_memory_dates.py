"""Birthdays and anniversaries reach her prompt only on the day itself."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.memory import store

FACTS = [
    {"id": "1", "text": "The user's birthday is on 10 October", "updated": "b"},
    {"id": "2", "text": "The user has a cat named Mochi", "updated": "a"},
    {"id": "3", "text": "Their anniversary with Sam is March 3rd", "updated": "c"},
]


def test_birthday_only_on_the_day(monkeypatch):
    monkeypatch.setattr(store, "all_facts", lambda: FACTS)
    other = store.as_prompt_block(date(2026, 10, 13))
    assert "birthday" not in other and "anniversary" not in other and "Mochi" in other
    day = store.as_prompt_block(date(2026, 10, 10))
    assert "birthday is on 10 October (that's today)" in day
    assert "anniversary" not in day
    assert "(that's today)" in store.as_prompt_block(date(2027, 3, 3))


def test_other_dates_are_untouched():
    assert store._yearly_date("They start a new job on 4 November") is None
    assert store._yearly_date("Born on the 2nd of June") == (6, 2)
