"""Config loading (task 2.1): YAML settings + env secrets."""

from __future__ import annotations

import json
import subprocess
import sys
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


# --- signal safe_length floor (channel-integrity, task 1.1) ---------------


def test_safe_length_below_one_code_point_is_refused_at_load():
    # The splitter measures UTF-8 bytes; below 4 no code point fits, so the
    # limit must be refused here rather than making no progress at send time.
    for bad in (0, 1, 3):
        raw = _minimal_raw("+1")
        raw["signal"]["safe_length"] = bad
        with pytest.raises(ConfigError):
            Config.from_dict(raw, env={})


def test_safe_length_exactly_one_code_point_is_accepted():
    raw = _minimal_raw("+1")
    raw["signal"]["safe_length"] = 4
    assert Config.from_dict(raw, env={}).signal.safe_length == 4


# --- signal transport timeouts (channel-integrity, task 3.1) --------------


def test_signal_timeouts_have_effective_defaults_when_absent():
    # rp5's config.yaml is locally modified and will not carry the new keys, so
    # the *effective* values come from the loader — which reads inline literals
    # for this section, not the dataclass defaults. Pin both.
    raw = _minimal_raw("+1")
    assert set(raw["signal"]) == {"bridge_url", "account"}
    signal = Config.from_dict(raw, env={}).signal
    assert signal.send_timeout_seconds == 10.0
    assert signal.open_timeout_seconds == 30.0
    assert signal.safe_length == 2000


def test_signal_timeouts_are_read_from_the_config_when_present():
    raw = _minimal_raw("+1")
    raw["signal"]["send_timeout_seconds"] = 15.5
    raw["signal"]["open_timeout_seconds"] = 45.0
    signal = Config.from_dict(raw, env={}).signal
    assert signal.send_timeout_seconds == 15.5
    assert signal.open_timeout_seconds == 45.0


def test_sample_config_declares_the_signal_timeouts():
    signal = Config.load(SAMPLE, env={}).signal
    assert signal.send_timeout_seconds == 10.0
    assert signal.open_timeout_seconds == 30.0


# --- signal owner acknowledgement (owner-acknowledgement, task 1.1) -------
#
# Every test goes through `Config.from_dict` with the keys absent or set, never
# against a dataclass attribute: rp5's config.yaml is locally modified and carries
# neither key, so the loader's fallback IS the production value (design D8).


def _signal_raw(**signal_keys):
    """A loadable mapping whose `signal` section is bridge_url, account, safe_length
    plus exactly the keys given."""
    raw = _minimal_raw("+1")
    raw["signal"]["safe_length"] = 2000
    raw["signal"].update(signal_keys)
    return raw


def test_acknowledgement_enabled_by_default_when_the_keys_are_absent():
    # Scenario: Enabled by default when the keys are absent. Catches a wrong
    # fallback in the builder.
    raw = _signal_raw()
    assert set(raw["signal"]) == {"bridge_url", "account", "safe_length"}
    signal = Config.from_dict(raw, env={}).signal
    assert signal.acknowledge_owner is True
    assert signal.acknowledge_timeout_seconds == 5.0


def test_an_explicit_false_acknowledge_owner_is_honoured():
    # Scenario: An explicit false is honoured. The absent-keys test passes on the
    # dataclass default alone; this one catches a builder that never reads the key
    # (design D8, proposal finding 2), so it must exist even though that one passes.
    signal = Config.from_dict(_signal_raw(acknowledge_owner=False), env={}).signal
    assert signal.acknowledge_owner is False


def test_an_explicit_true_acknowledge_owner_loads_true():
    signal = Config.from_dict(_signal_raw(acknowledge_owner=True), env={}).signal
    assert signal.acknowledge_owner is True


@pytest.mark.parametrize("bad", ["false", "true", 1, 0, None], ids=repr)
def test_a_non_boolean_acknowledge_owner_is_refused(bad):
    # Scenario: A non-boolean flag is refused. A quoted "false" is truthy and 1 is
    # not a flag; a blank `acknowledge_owner:` loads as YAML null. Each is refused
    # rather than read by bool(), which would turn "false" into True.
    with pytest.raises(ConfigError) as excinfo:
        Config.from_dict(_signal_raw(acknowledge_owner=bad), env={})
    assert "signal.acknowledge_owner" in str(excinfo.value)


def test_a_blank_acknowledge_owner_from_yaml_is_refused(tmp_path):
    # The blank case as the owner would actually write it, through the YAML parser.
    import yaml

    raw = _signal_raw()
    text = yaml.safe_dump(raw).replace(
        "safe_length: 2000", "safe_length: 2000\n  acknowledge_owner:"
    )
    assert yaml.safe_load(text)["signal"]["acknowledge_owner"] is None
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        Config.load(path, env={})


@pytest.mark.parametrize(
    "bad", [0, 0.0, -1, -0.5, "soon", "5", None, True, False, 7.5, 8, 60], ids=repr
)
def test_an_out_of_range_acknowledge_timeout_is_refused(bad):
    # Scenario: An out-of-range acknowledge timeout is refused. `True` is the trap:
    # float(True) is 1.0, which would otherwise pass as a valid timeout. 7.5 is
    # above TYPING_REFRESH_SECONDS: a timeout longer than the refresh interval lets
    # one hung refresh push the gap between starts past the ~15 s client expiry.
    with pytest.raises(ConfigError) as excinfo:
        Config.from_dict(_signal_raw(acknowledge_timeout_seconds=bad), env={})
    assert "signal.acknowledge_timeout_seconds" in str(excinfo.value)


@pytest.mark.parametrize("good", [2.5, 7.0, 7, 0.05])
def test_an_in_range_acknowledge_timeout_loads(good):
    # Exactly the refresh interval is allowed: the cap is `<=`, not `<` (D8's
    # arithmetic holds at T == interval).
    signal = Config.from_dict(
        _signal_raw(acknowledge_timeout_seconds=good), env={}
    ).signal
    assert signal.acknowledge_timeout_seconds == float(good)
    assert isinstance(signal.acknowledge_timeout_seconds, float)


def test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval():
    # config.py holds a copy of the cap (it must not import the Signal module; see
    # the next test). This is what keeps the copy honest: checked behaviourally
    # against the adapter's constant, so moving the interval without the cap fails
    # here.
    from henk.channel.signal import TYPING_REFRESH_SECONDS

    assert TYPING_REFRESH_SECONDS == 7.0
    ok = Config.from_dict(
        _signal_raw(acknowledge_timeout_seconds=TYPING_REFRESH_SECONDS), env={}
    ).signal
    assert ok.acknowledge_timeout_seconds == TYPING_REFRESH_SECONDS
    with pytest.raises(ConfigError):
        Config.from_dict(
            _signal_raw(acknowledge_timeout_seconds=TYPING_REFRESH_SECONDS + 0.001),
            env={},
        )


def test_loading_config_does_not_load_the_signal_adapter_module():
    # A replay calls Config.load, and the replay must never load
    # henk.channel.signal (tests/test_replay_isolation.py FORBIDDEN). The
    # acknowledge-timeout cap is therefore a copy, not an import, even a local one.
    code = (
        "import json, sys\n"
        "from henk.config import Config\n"
        "Config.load('config.yaml')\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('henk'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True,
        text=True, check=True,
    )
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert "henk.config" in loaded
    assert not any(
        m == "henk.channel.signal" or m.startswith("henk.channel.signal.")
        for m in loaded
    )


def test_acknowledgement_keys_leave_the_existing_signal_values_unchanged():
    base = Config.from_dict(_signal_raw(), env={}).signal
    with_keys = Config.from_dict(
        _signal_raw(acknowledge_owner=False, acknowledge_timeout_seconds=2.5), env={}
    ).signal
    for signal in (base, with_keys):
        assert signal.bridge_url == "http://b"
        assert signal.account == "+1"
        assert signal.safe_length == 2000
        assert signal.send_timeout_seconds == 10.0
        assert signal.open_timeout_seconds == 30.0


def test_sample_config_declares_acknowledgement_enabled():
    signal = Config.load(SAMPLE, env={}).signal
    assert signal.acknowledge_owner is True
    assert signal.acknowledge_timeout_seconds == 5.0
