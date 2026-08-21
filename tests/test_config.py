"""Settings, and the handling of a stored API key.

The key is the one piece of data here that would matter if it leaked, so most
of these tests are about where it does *not* go: not into the page, not into a
world-readable file, not into a response.
"""

import json
import stat

import pytest

from atif_view import config


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "nested" / "config.json"


KEY = "sk-ant-api03-" + "x" * 40


def test_no_key_no_source(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert config.api_key(cfg) is None
    assert config.source(cfg) is None
    assert config.hint(cfg) == ""


def test_a_key_round_trips(cfg):
    config.set_api_key(KEY, cfg)
    assert config.api_key(cfg) == KEY
    assert config.source(cfg) == "settings"


def test_the_environment_is_seen_when_nothing_is_stored(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    assert config.source(cfg) == "environment"
    assert config.api_key(cfg) is None


def test_a_stored_key_wins_over_the_environment(cfg, monkeypatch):
    """Whichever the user set most explicitly should be the one in use."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    config.set_api_key(KEY, cfg)
    assert config.source(cfg) == "settings"


def test_clearing_falls_back_to_the_environment(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment")
    config.set_api_key(KEY, cfg)
    config.clear_api_key(cfg)
    assert config.api_key(cfg) is None
    assert config.source(cfg) == "environment"


def test_the_hint_shows_four_characters_and_no_more(cfg):
    config.set_api_key(KEY, cfg)
    hint = config.hint(cfg)
    assert hint == "…xxxx"
    assert KEY not in hint


def test_the_file_is_readable_only_by_its_owner(cfg):
    config.set_api_key(KEY, cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700


def test_clearing_does_not_leave_the_key_in_the_file(cfg):
    config.set_api_key(KEY, cfg)
    config.clear_api_key(cfg)
    assert KEY not in cfg.read_text()


def test_other_settings_survive_a_key_change(cfg):
    config.store.write_json(cfg, {"version": 1, "theme": "dark"}, private=True)
    config.set_api_key(KEY, cfg)
    config.clear_api_key(cfg)
    assert json.loads(cfg.read_text())["theme"] == "dark"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "short", "sk-ant " + "x" * 40, "x" * 400],
    ids=["empty", "blank", "too-short", "contains-a-space", "too-long"],
)
def test_obvious_non_keys_are_refused(cfg, bad):
    with pytest.raises(ValueError):
        config.set_api_key(bad, cfg)
    assert not cfg.exists()


def test_a_damaged_config_reads_as_empty_rather_than_raising(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    assert config.api_key(cfg) is None
