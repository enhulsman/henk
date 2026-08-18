"""Config loading (task 2.1): YAML settings + env secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from henk.config import (
    LIVENESS_DEADLINE_KEEPALIVE_MULTIPLE,
    Config,
    ConfigError,
)

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
    # 5, not 3: one host outage produces two announceable conversations (measured
    # 2026-08-07, sensor-routing-coverage 4.3c — 153s arrival gap vs 120s debounce),
    # so 3 would gate the second half of a second outage. See test_event_pipeline.py
    # ::test_two_host_outages_in_24h_fit_under_the_shipped_cap.
    assert config.events.cap_per_24h == 5
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


# --- Liveness deadline vs. the recorded server keepalive interval -------------
#
# The deadline is Henk's policy (`events`), the interval is a recorded property of
# the ntfy server (`endpoints.ntfy`) -- two sections, which is why the ordering is
# validated after assembly rather than inside either builder. The predicate is
# `deadline >= k * interval` with k a whole multiple greater than one; a bare `>`
# would admit 1.33x, where one late keepalive trips the watchdog.


def test_sample_config_liveness_deadline_is_a_permitted_multiple():
    config = Config.load(SAMPLE, env={})
    interval = config.ntfy.keepalive_interval_seconds
    assert interval == 45.0  # the measured vps value (D2)
    assert (
        config.events.liveness_deadline_seconds
        >= LIVENESS_DEADLINE_KEEPALIVE_MULTIPLE * interval
    )


def test_deadline_exactly_the_required_multiple_is_accepted():
    # `>=`, not `>`: 3 x 45 = 135 is the intended production value.
    config = Config.from_dict(_raw_with_liveness(deadline=135, interval=45), env={})
    assert config.events.liveness_deadline_seconds == 135.0


def test_deadline_below_the_required_multiple_is_refused():
    # 60 > 45 but 60 < 135 -- the case a bare `>` check would wrongly admit.
    with pytest.raises(ConfigError) as excinfo:
        Config.from_dict(_raw_with_liveness(deadline=60, interval=45), env={})
    message = str(excinfo.value)
    assert "60" in message and "45" in message  # names both values


def test_deadline_below_the_interval_is_refused():
    with pytest.raises(ConfigError):
        Config.from_dict(_raw_with_liveness(deadline=30, interval=45), env={})


def test_non_positive_keepalive_interval_is_refused():
    # A zero interval would silently satisfy any deadline, disabling the one guard
    # this validator exists to provide.
    with pytest.raises(ConfigError):
        Config.from_dict(_raw_with_liveness(deadline=135, interval=0), env={})


def _raw_with_liveness(*, deadline, interval):
    """A loadable mapping carrying both values, so the validator is exercised
    against configuration rather than only against constructor defaults."""
    raw = _minimal_raw("+31600000000")
    raw["endpoints"]["ntfy"]["keepalive_interval_seconds"] = interval
    raw["events"] = {"enabled": True, "liveness_deadline_seconds": deadline}
    return raw


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


# --- store section (memory + capture inbox, task 1.1 / 6.1) ---------------


def test_store_section_absent_uses_safe_defaults():
    config = Config.from_dict(_minimal_raw("+1"), env={})
    # Default sits inside the audit volume's mount point, so state survives
    # container recreation without adding a volume (secure-deployment spec).
    assert config.store.path == "/data/audit/henk-store.db"
    assert config.store.memory_caps == {"pinned": 50, "agent": 20}
    assert config.store.fact_length_limit == 500
    assert config.store.recall_render_limit == 8000
    assert config.store.inbox_page_size == 20


def test_store_section_parsed_when_present():
    raw = _minimal_raw("+1")
    raw["store"] = {
        "path": "/data/store/other.db",
        "memory_pinned_cap": 5,
        "memory_agent_cap": 3,
        "fact_length_limit": 120,
        "recall_render_limit": 400,
        "inbox_page_size": 10,
    }
    config = Config.from_dict(raw, env={})
    assert config.store.path == "/data/store/other.db"
    assert config.store.memory_caps == {"pinned": 5, "agent": 3}
    assert config.store.fact_length_limit == 120
    assert config.store.recall_render_limit == 400
    assert config.store.inbox_page_size == 10


def test_sample_config_declares_the_store_path():
    config = Config.load(SAMPLE, env={})
    assert config.store.path


# --- gate section: the kill-switch only narrows (task 2.2 / 6.1) ----------


def test_gate_section_absent_defaults_to_standing_enabled():
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.gate.demote_standing is False


def test_gate_demotion_flag_parsed_when_present():
    raw = _minimal_raw("+1")
    raw["gate"] = {"demote_standing": True}
    assert Config.from_dict(raw, env={}).gate.demote_standing is True


def test_gate_config_exposes_no_widening_knob():
    # Structural, not stylistic: a promote/scope knob in config would move a
    # security decision out of code review (design D4).
    import dataclasses

    from henk.config import GateConfig

    assert [f.name for f in dataclasses.fields(GateConfig)] == ["demote_standing"]


# --- audit section: decoupled from event intake (task 3.2) -----------------


def test_audit_path_defaults_to_the_events_scoped_key():
    raw = _minimal_raw("+1")
    raw["events"] = {"audit_path": "/data/audit/legacy.jsonl"}
    config = Config.from_dict(raw, env={})
    assert config.audit.path == "/data/audit/legacy.jsonl"


def test_explicit_audit_path_wins_over_the_events_key():
    raw = _minimal_raw("+1")
    raw["events"] = {"audit_path": "/data/audit/legacy.jsonl"}
    raw["audit"] = {"path": "/data/audit/new.jsonl"}
    assert Config.from_dict(raw, env={}).audit.path == "/data/audit/new.jsonl"


def test_audit_path_present_even_with_events_absent():
    # Audit must exist in every supported configuration, rollback path included.
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.audit.path
    assert config.events.enabled is False
