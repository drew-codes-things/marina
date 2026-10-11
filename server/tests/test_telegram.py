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
