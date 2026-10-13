"""Texts should split into bubbles like a person sends them, with code
fences kept whole and everything else escaped for Telegram."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.telegram import LIMIT, _bubbles, _to_html
from process.tools.workspace import _path


def test_blank_lines_make_separate_texts():
    assert _bubbles("wait what\n\nno way\n\n\nlmao") == ["wait what", "no way", "lmao"]


def test_single_newlines_stay_in_one_text():
    assert _bubbles("line one\nline two") == ["line one\nline two"]


def test_code_fence_with_blank_lines_is_one_text():
    reply = "try this\n\n```python\ndef f():\n\n    return 1\n```\n\nshould work"
    assert _bubbles(reply) == [
        "try this",
        "```python\ndef f():\n\n    return 1\n```",
        "should work",
    ]


def test_long_text_is_cut_under_the_limit():
    parts = _bubbles("\n".join(["x" * 100] * 100))
    assert len(parts) > 1
    assert all(len(p) <= LIMIT for p in parts)


def test_html_escapes_text_and_formats_code():
    assert _to_html("a < b & `x<y`") == "a &lt; b &amp; <code>x&lt;y</code>"
    assert _to_html("```bash\nls <dir>\n```") == "<pre>ls &lt;dir&gt;</pre>"


def test_workspace_paths_are_quoted_and_rooted():
    assert _path("app.py").endswith("/app.py")
    assert _path("my file; rm -rf /") == "~/'projects/my file; rm -rf /'"
    assert _path("~/x y") == "~/'x y'"
    assert _path("/etc/hosts") == "/etc/hosts"


from process import telegram  # noqa: E402

HEAVY = ("it's tuesday morning now though and everything still feels heavy "
         "sometimes but look, we're here together")


def test_the_same_text_to_a_new_message_is_a_repeat():
    assert telegram._repeats(HEAVY.replace("look", "look—"), [HEAVY])


def test_a_phrase_from_an_earlier_text_is_a_repeat():
    worn = telegram._repeats("lol yeah, everything still feels heavy sometimes ngl",
                             ["ugh", HEAVY])
    assert worn == ["everything still feels heavy sometimes"]


def test_a_phrase_looped_inside_one_text_is_a_repeat():
    assert telegram._repeats("we're here together okay. we're here together okay. "
                             "we're here together okay", [])


def test_a_fresh_text_is_not_a_repeat():
    assert telegram._repeats("wait you finished the whole series already??", [HEAVY]) == []
    assert telegram._repeats("lol", ["lol"]) == []


def _fake_generate(replies, tools=False):
    calls = []

    def generate(user_text, extra, note=""):
        calls.append(note)
        return replies[len(calls) - 1], tools
    return generate, calls


def test_a_repeat_is_asked_for_again_with_the_phrase_named(monkeypatch):
    generate, calls = _fake_generate([HEAVY, "omg happy late birthday btw"])
    monkeypatch.setattr(telegram, "_generate", generate)
    monkeypatch.setattr(telegram, "_recent_replies", lambda: [HEAVY])
    assert telegram._compose("hows it going", "") == "omg happy late birthday btw"
    assert "still feels heavy" in calls[1]


def test_a_reply_that_used_tools_is_never_asked_for_again(monkeypatch):
    generate, calls = _fake_generate([HEAVY], tools=True)
    monkeypatch.setattr(telegram, "_generate", generate)
    monkeypatch.setattr(telegram, "_recent_replies", lambda: [HEAVY])
    assert telegram._compose("open notes", "") == HEAVY
    assert len(calls) == 1


def test_stage_directions_are_dropped_but_code_kept():
    assert telegram._without_actions("*tilts head* i mean i could") == "i mean i could"
    assert telegram._without_actions("2*3*4 is maths") == "2*3*4 is maths"
    assert telegram._without_actions("```\nx = *y*\n```") == "```\nx = *y*\n```"


def test_only_the_answer_after_tool_use_is_texted(monkeypatch):
    monkeypatch.setattr(telegram, "llm_stream", lambda *a, **k: iter(
        ["let me check what's going on, give me a sec", "", "ok disk is at 40%, all good"]))
    reply, used = telegram._generate("check your disk", "")
    assert used and reply == "ok disk is at 40%, all good"


def test_day_and_time_only_when_they_bring_it_up():
    assert telegram._mentions_when("ugh fine. tuesday afternoon and we're dodging it",
                                   "how about we talk about something else") == "tuesday"
    assert telegram._mentions_when("morning! it's tuesday already", "Morning") is None
    assert telegram._mentions_when("what's up", "hey") is None


def test_a_topic_they_have_moved_on_from_is_caught():
    mine = ["kubernetes?? who asked for that", "classic kubernetes plotting your demise",
            "we're dodging kubernetes trauma"]
    moved_on = ["how about we talk about something else", "I got it, don't worry"]
    assert telegram._stuck_topics("kubernetes isn't a personality trait", mine, moved_on) == [
        "kubernetes"]
    # Still their subject, so still fine to talk about.
    assert telegram._stuck_topics("kubernetes again lol", mine, ["kubernetes broke again"]) == []


def test_a_draft_with_problems_is_written_again(monkeypatch):
    drafts = iter(["ugh tuesday again, kubernetes is plotting", "fair, what else is new with you"])
    notes = []

    def fake(user_text, extra, note=""):
        notes.append(note)
        return next(drafts), False

    monkeypatch.setattr(telegram, "_generate", fake)
    monkeypatch.setattr(telegram, "_recent", lambda role, n: (
        ["kubernetes?? who asked", "classic kubernetes", "dodging kubernetes"]
        if role == "assistant" else ["talk about something else"]))
    assert telegram._compose("anyway", "") == "fair, what else is new with you"
    assert "tuesday" in notes[1] and "kubernetes" in notes[1]


def test_a_text_resent_from_long_ago_is_caught():
    old = "hey! just checking in because it feels like we got distracted earlier. how's the coffee"
    assert telegram._repeats(old, recent=["something else entirely here ok"], older=[old])
