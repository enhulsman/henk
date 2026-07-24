"""Config loading (task 2.1): YAML settings + env secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from henk.config import Config, ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "config.yaml"


def test_loads_sample_config_with_env_secrets():
    env = {
        "ANTHROPIC_CREDENTIAL": "cred",
        "TAIGA_TOKEN": "tk",
        "TODO_TOKEN": "todo",
        "NTFY_TOKEN": "ntfy",
    }
    config = Config.load(SAMPLE, env=env)

    assert config.owner.id
    assert config.signal.bridge_url.startswith("http")
    assert config.agent.model
    assert config.ntfy.topic
    assert config.secrets.taiga_token == "tk"
    assert config.secrets.ntfy_token == "ntfy"


def test_secrets_default_empty_when_env_absent():
    config = Config.load(SAMPLE, env={})
    assert config.secrets.anthropic_credential == ""
    assert config.secrets.todo_token == ""


def test_loads_events_config_with_overrides():
    config = Config.load(SAMPLE, env={})
    assert config.events.enabled is True
    assert config.events.events_topic == "henk-events"
    assert config.events.handoffs_topic == "henk-handoffs"
    assert config.events.cap_per_24h == 3
    assert config.events.cooldown_overrides[0]["pattern"] == "swap"
    assert config.events.cooldown_overrides[0]["cooldown_seconds"] == 86400


def test_events_section_absent_defaults_to_disabled():
    # No `events` section → v1 behaviour (subscriber never starts).
    config = Config.from_dict(_minimal_raw("+31600000000"), env={})
    assert config.events.enabled is False
    assert config.events.events_topic == "henk-events"


def test_personal_data_allowlist_defaults_empty_fail_closed():
    # Repo default stays empty → todo_read fails closed. The real prefix lives only
    # in the deployed rp5 config.
    config = Config.load(SAMPLE, env={})
    assert config.personal_data.todo_note_allowlist == ()
    assert config.personal_data.taiga_project_allowlist == ()


def test_personal_data_allowlist_parsed_when_present():
    raw = _minimal_raw("+31600000000")
    raw["personal_data"] = {"todo_note_allowlist": ["Personal/", "Homelab/"]}
    config = Config.from_dict(raw, env={})
    assert config.personal_data.todo_note_allowlist == ("Personal/", "Homelab/")


def test_personal_data_section_absent_defaults_empty():
    config = Config.from_dict(_minimal_raw("+31600000000"), env={})
    assert config.personal_data.todo_note_allowlist == ()


def test_missing_required_section_raises():
    with pytest.raises(ConfigError):
        Config.from_dict({"owner": {"id": "x"}}, env={})


def _minimal_raw(owner_id):
    return {
        "owner": {"id": owner_id},
        "signal": {"bridge_url": "http://b", "account": "+1"},
        "endpoints": {
            "gatus": {"base_url": "http://g"},
            "prometheus": {"base_url": "http://p"},
            "taiga": {"base_url": "http://t"},
            "todo": {"base_url": "http://d"},
            "ntfy": {"base_url": "http://n", "topic": "henk"},
        },
    }


def test_missing_owner_id_raises():
    raw = _minimal_raw("+1")
    del raw["owner"]["id"]
    with pytest.raises(ConfigError):
        Config.from_dict(raw, env={})


def test_empty_owner_id_rejected_fail_closed():
    # An empty owner id would open a fail-open hole in the allowlist.
    with pytest.raises(ConfigError):
        Config.from_dict(_minimal_raw(""), env={})
    with pytest.raises(ConfigError):
        Config.from_dict(_minimal_raw("   "), env={})
