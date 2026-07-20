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


def test_missing_required_section_raises():
    with pytest.raises(ConfigError):
        Config.from_dict({"owner": {"id": "x"}}, env={})


def test_missing_owner_id_raises():
    raw = {
        "owner": {},
        "signal": {"bridge_url": "http://b", "account": "+1"},
        "endpoints": {
            "gatus": {"base_url": "http://g"},
            "prometheus": {"base_url": "http://p"},
            "taiga": {"base_url": "http://t"},
            "todo": {"base_url": "http://d"},
            "ntfy": {"base_url": "http://n", "topic": "henk"},
        },
    }
    with pytest.raises(ConfigError):
        Config.from_dict(raw, env={})
