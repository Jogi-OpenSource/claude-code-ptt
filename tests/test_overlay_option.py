"""Tests for the `overlay` config option.

The built-in floating session window is on by default; a user who shows the
daemon state in a front end of their own (GET /ui, POST /target) switches
it off in config.json.
"""
import json

from claude_code_ptt import config as config_module
from claude_code_ptt.config import Config


def test_overlay_is_on_by_default():
    assert Config().overlay is True


def test_overlay_can_be_switched_off_in_the_config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"overlay": False}), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", path)
    assert Config.load().overlay is False


def test_daemon_starts_the_overlay_only_when_enabled():
    """The daemon source must gate the overlay on the option (the Daemon
    itself cannot be constructed in a test: it opens audio devices)."""
    from pathlib import Path
    source = Path(config_module.__file__).with_name("daemon.py").read_text(
        encoding="utf-8")
    assert "if self.config.overlay:" in source
