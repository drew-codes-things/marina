"""set_value should change one setting in place, add it to its section, or
add the section, and leave everything else in the file alone."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process import config

BASE = """llm:
  model: "marina"   # the brain

mac:
  enabled: false   # off by default

tools:
  timers: true
"""


def write(tmp_path, monkeypatch, text):
    path = tmp_path / "character_config.yaml"
    path.write_text(text)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "_value_overrides", {})
    return path


def test_changes_a_value_in_place(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, BASE)
    config.set_value("mac", "enabled", True)
    text = path.read_text()
    assert yaml.safe_load(text)["mac"]["enabled"] is True
    assert '  model: "marina"   # the brain' in text
    assert yaml.safe_load(text)["tools"] == {"timers": True}


def test_adds_a_missing_key_and_section(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, BASE)
    config.set_value("tools", "mac", False)
    config.set_value("wake", "listen_seconds", 12)
    d = yaml.safe_load(path.read_text())
    assert d["tools"] == {"timers": True, "mac": False}
    assert d["wake"] == {"listen_seconds": 12}
    assert d["mac"]["enabled"] is False
