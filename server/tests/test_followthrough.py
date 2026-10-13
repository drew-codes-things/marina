"""A reply that ends with a plan, when she had tools, should get one nudge."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.llm_funcs.llm_scr import _promised


def test_spots_a_plan_left_hanging():
    assert _promised("Alright, let's do this properly. I'll check what's available first, then screenshot.")
    assert _promised("ok give me a sec, let me look")
    assert _promised("I’m going to install the theme now.")


def test_leaves_finished_answers_alone():
    assert not _promised("Done. It's dark now and the wallpaper is a purple gradient.")
    assert not _promised("Let's just say it went badly. The theme is installed and applied though.")
    assert not _promised("")


def test_only_requests_are_held_to_a_plan():
    from process.llm_funcs import llm_scr
    assert llm_scr._asked_for_work("can you look over your machine")
    assert not llm_scr._asked_for_work("then how about we talk about something else")
    assert not llm_scr._asked_for_work(None)
