"""Config surface for triage-quality (group 2; design D11, D13, D14, D17).

The new keys are the three event-profile keys (`agent.event_model`,
`agent.event_effort`, `agent.event_thinking`), `triage_recording.enabled` and
`replay.judge_model` / `replay.judge_effort`. Every other bound is a module
constant, and the recording, case and replay-output directories are derived from
`audit.path` rather than configured.

Every default is tested through `Config.from_dict` with the key ABSENT (standing
rule 4): rp5's `config.yaml` is locally modified and carries none of these keys,
so the loader's fallback is the deployed value there.

The event session factory itself is group 9's work (D11 "Wiring"); until it
exists, 2.1 asserts the resolved event values on the `Config` and on a factory
config built from them the way `runtime.py` builds the chat one.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

from henk.agent.sdk_session import SdkSessionFactory, reasoning_options
from henk.config import (
    EFFORT_LEVELS,
    THINKING_MODES,
    AgentConfig,
    AuditConfig,
    Config,
    ConfigError,
    ReplayConfig,
    Secrets,
    TriageRecordingConfig,
)
from henk.tools.base import ToolRegistry
from tests.test_config import SAMPLE, _minimal_raw

EVENT_KEYS = ("event_model", "event_effort", "event_thinking")
#: Each event key and the chat key it falls back to (D11 table).
EVENT_TO_CHAT = {"event_model": "model", "event_effort": "effort", "event_thinking": "thinking"}


def _load(agent=None, **sections):
    raw = _minimal_raw("+31600000000")
    if agent is not None:
        raw["agent"] = agent
    raw.update(sections)
    return Config.from_dict(raw, env={})


def _event_triple(config):
    return (
        config.agent.event_model,
        config.agent.event_effort,
        config.agent.event_thinking,
    )


def _chat_triple(config):
    return (config.agent.model, config.agent.effort, config.agent.thinking)


def _factory(config, *, event):
    """A factory built exactly as `runtime.py:135-142` builds the chat one."""
    agent = config.agent
    return SdkSessionFactory(
        ToolRegistry(),
        None,  # the gate is stored, never touched, until a session is created
        model=agent.event_model if event else agent.model,
        system_prompt=agent.system_prompt,
        effort=agent.event_effort if event else agent.effort,
        thinking=agent.event_thinking if event else agent.thinking,
    )


# --- 2.1 Defaults change nothing (agent-core, "Defaults change nothing") ----


def test_absent_event_keys_equal_the_default_chat_values():
    raw = _minimal_raw("+31600000000")
    assert "agent" not in raw
    config = Config.from_dict(raw, env={})
    assert _event_triple(config) == _chat_triple(config)
    assert _event_triple(config) == ("claude-sonnet-5", "high", "adaptive")


@pytest.mark.parametrize(
    "chat",
    [
        {"model": "claude-opus-5-5", "effort": "max", "thinking": "disabled"},
        {"model": "claude-haiku-5", "effort": "low"},
        {"thinking": "disabled"},
        {"effort": None, "thinking": None},
        {"model": "claude-opus-5-5", "effort": None},
    ],
)
def test_absent_event_keys_follow_the_resolved_chat_values_even_when_overridden(chat):
    # The fallback is the RESOLVED chat value, not the chat dataclass default: an
    # overridden chat profile must carry over, or shipping the keys absent would
    # silently move triage onto a different model or effort than chat.
    config = _load(dict(chat))
    assert not set(EVENT_KEYS) & set(chat)
    assert _event_triple(config) == _chat_triple(config)
    for key, value in chat.items():
        assert getattr(config.agent, "event_" + key) == value


def test_an_empty_agent_section_resolves_like_an_absent_one():
    for empty in ({}, None):
        raw = _minimal_raw("+31600000000")
        raw["agent"] = empty
        config = Config.from_dict(raw, env={})
        assert _event_triple(config) == ("claude-sonnet-5", "high", "adaptive")


def test_the_event_factory_config_equals_the_chat_factory_config_by_default():
    # "Assert this through the built event factory's config." Group 9 builds the
    # second factory in runtime.py; until then this is a factory built from the
    # event values the same way runtime.py builds the chat one.
    config = _load({"model": "claude-opus-5-5", "effort": "xhigh", "thinking": "disabled"})
    chat = _factory(config, event=False).config
    event = _factory(config, event=True).config
    assert event == chat
    assert event.model == "claude-opus-5-5"
    assert reasoning_options(event) == reasoning_options(chat) == {
        "effort": "xhigh",
        "thinking": {"type": "disabled"},
    }


def test_the_dataclass_default_also_inherits_the_chat_values():
    # The default is pinned in both places (D17): the loader resolves from the
    # resolved chat values, and a directly constructed AgentConfig inherits too.
    default = AgentConfig()
    assert (default.event_model, default.event_effort, default.event_thinking) == (
        default.model,
        default.effort,
        default.thinking,
    )
    built = AgentConfig(model="claude-opus-5-5", effort="low", thinking="disabled")
    assert (built.event_model, built.event_effort, built.event_thinking) == (
        "claude-opus-5-5",
        "low",
        "disabled",
    )
    # An explicit None on the dataclass is kept, not inherited.
    explicit = AgentConfig(effort="low", event_effort=None, event_thinking=None)
    assert explicit.event_effort is None
    assert explicit.event_thinking is None


# --- 2.2 The explicit values ------------------------------------------------


def test_explicit_event_values_apply_to_the_event_profile_only():
    config = _load(
        {
            "model": "claude-sonnet-5",
            "effort": "high",
            "event_model": "claude-opus-5-5",
            "event_effort": "max",
            "event_thinking": "disabled",
        }
    )
    assert _chat_triple(config) == ("claude-sonnet-5", "high", "adaptive")
    assert _event_triple(config) == ("claude-opus-5-5", "max", "disabled")
    event = _factory(config, event=True).config
    assert event.model == "claude-opus-5-5"
    assert reasoning_options(event) == {"effort": "max", "thinking": {"type": "disabled"}}


def test_null_event_effort_defers_to_the_cli_which_is_not_the_same_as_absent():
    explicit = _load({"effort": "high", "event_effort": None})
    absent = _load({"effort": "high"})
    assert explicit.agent.event_effort is None
    assert absent.agent.event_effort == "high"
    # Deferring means the reasoning kwarg is omitted, not passed as None.
    options = reasoning_options(_factory(explicit, event=True).config)
    assert "effort" not in options
    assert options == {"thinking": {"type": "adaptive"}}


def test_null_event_thinking_defers_to_the_cli_which_is_not_the_same_as_absent():
    explicit = _load({"thinking": "adaptive", "event_thinking": None})
    absent = _load({"thinking": "adaptive"})
    assert explicit.agent.event_thinking is None
    assert absent.agent.event_thinking == "adaptive"
    options = reasoning_options(_factory(explicit, event=True).config)
    assert "thinking" not in options
    assert options == {"effort": "high"}


@pytest.mark.parametrize("chat_model", [None, "", 5])
def test_an_absent_event_model_never_refuses_what_the_chat_model_accepted(chat_model):
    """Absent means "the resolved agent.model, whatever it is" (D11).

    The chat key is not validated, and loaded before this change, so an absent
    event key must not start refusing it under a key the owner never wrote.
    """
    config = _load({"model": chat_model})
    assert config.agent.model == chat_model
    assert config.agent.event_model == chat_model


def test_null_event_model_is_refused_naming_the_key():
    with pytest.raises(ConfigError) as excinfo:
        _load({"event_model": None})
    assert "agent.event_model" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   ", 5, ["claude-opus-5-5"]])
def test_a_blank_or_non_string_event_model_is_refused_naming_the_key(bad):
    with pytest.raises(ConfigError) as excinfo:
        _load({"event_model": bad})
    assert "agent.event_model" in str(excinfo.value)


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_every_effort_level_is_accepted_for_the_event_profile(level):
    assert _load({"event_effort": level}).agent.event_effort == level


@pytest.mark.parametrize("mode", THINKING_MODES)
def test_every_thinking_mode_is_accepted_for_the_event_profile(mode):
    assert _load({"event_thinking": mode}).agent.event_thinking == mode


@pytest.mark.parametrize(
    ("key", "value"),
    [("event_effort", "extreme"), ("event_thinking", "enabled")],
)
def test_event_reasoning_keys_are_refused_by_the_chat_keys_path(key, value):
    # Same `_require_choice` path as `agent.effort` / `agent.thinking`, so the
    # refusal reads the same: the section-qualified key, the value, the choices,
    # and the null escape.
    with pytest.raises(ConfigError) as event_exc:
        _load({key: value})
    with pytest.raises(ConfigError) as chat_exc:
        _load({EVENT_TO_CHAT[key]: value})
    event_msg, chat_msg = str(event_exc.value), str(chat_exc.value)
    assert f"agent.{key}" in event_msg
    assert repr(value) in event_msg
    assert event_msg.replace(f"agent.{key}", "agent.X") == chat_msg.replace(
        f"agent.{EVENT_TO_CHAT[key]}", "agent.X"
    )


def test_an_invalid_chat_value_is_refused_before_it_could_be_inherited():
    # The event fallback never launders an invalid chat value: the chat key is
    # validated first and named.
    with pytest.raises(ConfigError) as excinfo:
        _load({"effort": "extreme"})
    assert "agent.effort" in str(excinfo.value)


# --- 2.3 triage_recording and replay ----------------------------------------


def test_triage_recording_is_enabled_by_default_with_the_section_absent():
    raw = _minimal_raw("+31600000000")
    assert "triage_recording" not in raw
    assert Config.from_dict(raw, env={}).triage_recording.enabled is True
    assert TriageRecordingConfig().enabled is True


def test_triage_recording_can_be_disabled_the_rollback():
    config = _load(triage_recording={"enabled": False})
    assert config.triage_recording.enabled is False


def test_an_empty_triage_recording_section_keeps_the_default():
    for empty in ({}, None):
        assert _load(triage_recording=empty).triage_recording.enabled is True


def test_replay_judge_defaults_with_the_section_absent():
    raw = _minimal_raw("+31600000000")
    assert "replay" not in raw
    replay = Config.from_dict(raw, env={}).replay
    assert replay.judge_model == "claude-fable-5-1"
    assert replay.judge_effort == "high"
    assert ReplayConfig().judge_model == "claude-fable-5-1"
    assert ReplayConfig().judge_effort == "high"


def test_an_empty_replay_section_keeps_the_defaults():
    for empty in ({}, None):
        replay = _load(replay=empty).replay
        assert (replay.judge_model, replay.judge_effort) == ("claude-fable-5-1", "high")


def test_each_replay_default_holds_with_only_the_other_key_present():
    assert _load(replay={"judge_effort": "max"}).replay.judge_model == "claude-fable-5-1"
    assert _load(replay={"judge_model": "claude-opus-5-5"}).replay.judge_effort == "high"


def test_replay_judge_values_are_read_when_present():
    replay = _load(replay={"judge_model": "claude-opus-5-5", "judge_effort": "xhigh"}).replay
    assert (replay.judge_model, replay.judge_effort) == ("claude-opus-5-5", "xhigh")


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_every_effort_level_is_accepted_for_the_judge(level):
    assert _load(replay={"judge_effort": level}).replay.judge_effort == level


@pytest.mark.parametrize("bad", ["extreme", "HIGH", "", None])
def test_judge_effort_is_validated_against_effort_levels(bad):
    # Null is refused too: a grade records the judge's effort, and a CLI-chosen
    # effort would make two grades of one case incomparable without saying so.
    with pytest.raises(ConfigError) as excinfo:
        _load(replay={"judge_effort": bad})
    assert "replay.judge_effort" in str(excinfo.value)


@pytest.mark.parametrize("bad", [None, "", "   ", 7])
def test_a_null_blank_or_non_string_judge_model_is_refused_naming_the_key(bad):
    with pytest.raises(ConfigError) as excinfo:
        _load(replay={"judge_model": bad})
    assert "replay.judge_model" in str(excinfo.value)


@pytest.mark.parametrize("section", ["triage_recording", "replay"])
def test_a_non_mapping_section_is_refused_naming_it(section):
    with pytest.raises(ConfigError) as excinfo:
        _load(**{section: True})
    assert section in str(excinfo.value)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("triage_recording", "path"),
        ("triage_recording", "max_recordings"),
        ("triage_recording", "retention_days"),
        ("replay", "output_dir"),
        ("replay", "judge_token"),
        ("replay", "base_url"),
    ],
)
def test_an_unknown_key_in_a_new_section_is_refused_not_ignored(section, key):
    # A silently ignored `triage_recording.path` would let the owner believe the
    # recordings moved. The paths are derived and the bounds are module constants,
    # so any such key is refused, naming it.
    with pytest.raises(ConfigError) as excinfo:
        _load(**{section: {key: "x"}})
    assert f"{section}.{key}" in str(excinfo.value)


# --- 2.4 Invariants ----------------------------------------------------------


def test_secrets_from_env_is_unchanged():
    assert {f.name for f in dataclasses.fields(Secrets)} == {
        "anthropic_credential",
        "taiga_token",
        "todo_token",
        "ntfy_token",
    }
    env = {
        "ANTHROPIC_CREDENTIAL": "a",
        "TAIGA_TOKEN": "t",
        "TODO_TOKEN": "d",
        "NTFY_TOKEN": "n",
        "JUDGE_TOKEN": "j",
        "REPLAY_TOKEN": "r",
    }
    assert Secrets.from_env(env) == Secrets(
        anthropic_credential="a", taiga_token="t", todo_token="d", ntfy_token="n"
    )
    raw = _minimal_raw("+31600000000")
    raw["replay"] = {"judge_model": "claude-opus-5-5"}
    raw["triage_recording"] = {"enabled": False}
    assert Config.from_dict(raw, env=env).secrets == Secrets.from_env(env)


def test_the_new_config_surface_is_exactly_this():
    # Enumerated, not sampled: an added key fails here and must be justified in
    # the diff. This is also the "no archive, digest or recording bound is a key"
    # invariant (D17): those are module constants, so no field may exist for them.
    assert {f.name for f in dataclasses.fields(TriageRecordingConfig)} == {"enabled"}
    assert {f.name for f in dataclasses.fields(ReplayConfig)} == {
        "judge_model",
        "judge_effort",
    }
    agent_fields = {f.name for f in dataclasses.fields(AgentConfig)}
    assert set(EVENT_KEYS) <= agent_fields
    assert {n for n in agent_fields if n.startswith("event_")} == set(EVENT_KEYS)


def _new_key_names():
    return (
        [f"agent.{k}" for k in EVENT_KEYS]
        + [f"triage_recording.{f.name}" for f in dataclasses.fields(TriageRecordingConfig)]
        + [f"replay.{f.name}" for f in dataclasses.fields(ReplayConfig)]
    )


@pytest.mark.parametrize(
    "forbidden",
    ["path", "dir", "file", "token", "secret", "key", "credential", "url", "host", "port"],
)
def test_no_new_key_names_a_path_a_token_or_a_url(forbidden):
    for name in _new_key_names():
        leaf = name.split(".", 1)[1]
        assert forbidden not in leaf.lower(), name


@pytest.mark.parametrize(
    "bound",
    ["max", "limit", "cap", "days", "bytes", "count", "retention", "rows", "size", "archive", "digest"],
)
def test_no_new_key_configures_an_archive_digest_or_recording_bound(bound):
    for name in _new_key_names():
        assert bound not in name.split(".", 1)[1].lower(), name


def test_no_new_default_holds_an_address_or_a_path():
    values = [
        getattr(TriageRecordingConfig(), f.name) for f in dataclasses.fields(TriageRecordingConfig)
    ] + [getattr(ReplayConfig(), f.name) for f in dataclasses.fields(ReplayConfig)]
    for value in values:
        if isinstance(value, str):
            assert "/" not in value and ":" not in value, value


# --- 2.4 Recording paths are derived from the audit path (secure-deployment) --


@pytest.mark.parametrize(
    "audit_path",
    [
        "/data/audit/henk-audit.jsonl",
        "/srv/elsewhere/deeper/audit.jsonl",
        "relative/dir/audit.jsonl",
    ],
)
def test_recording_case_and_replay_directories_are_subdirectories_of_the_audit_dir(
    audit_path,
):
    config = _load(audit={"path": audit_path})
    audit_dir = Path(audit_path).parent
    audit = config.audit
    assert audit.triage_recordings_dir == audit_dir / "triage-recordings"
    assert audit.triage_cases_dir == audit_dir / "triage-cases"
    assert audit.triage_replays_dir == audit_dir / "triage-replays"
    for derived in (
        audit.triage_recordings_dir,
        audit.triage_cases_dir,
        audit.triage_replays_dir,
    ):
        assert derived.parent == audit_dir


def test_the_derived_directories_follow_the_events_audit_path_fallback():
    # rp5 carries `events.audit_path` and no `audit` section; the derivation must
    # follow the path the audit log is ACTUALLY written to.
    config = _load(events={"audit_path": "/var/lib/henk-a/audit.jsonl"})
    assert config.audit.path == "/var/lib/henk-a/audit.jsonl"
    assert config.audit.triage_recordings_dir == Path("/var/lib/henk-a/triage-recordings")
    assert config.audit.triage_replays_dir == Path("/var/lib/henk-a/triage-replays")


def test_the_deployed_default_directories():
    config = _load()
    assert config.audit.triage_recordings_dir == Path("/data/audit/triage-recordings")
    assert config.audit.triage_cases_dir == Path("/data/audit/triage-cases")
    assert config.audit.triage_replays_dir == Path("/data/audit/triage-replays")
    assert AuditConfig().triage_recordings_dir == Path("/data/audit/triage-recordings")


def test_no_configuration_key_changes_the_derived_directories():
    baseline = _load(audit={"path": "/data/audit/henk-audit.jsonl"}).audit
    attempted = _load(
        audit={
            "path": "/data/audit/henk-audit.jsonl",
            "triage_recordings_dir": "/tmp/x",
            "recordings_dir": "/tmp/x",
            "replays_dir": "/tmp/x",
        },
        store={"path": "/elsewhere/henk-store.db"},
        homelab_docs={"path": "/elsewhere/docs"},
    ).audit
    for name in ("triage_recordings_dir", "triage_cases_dir", "triage_replays_dir"):
        assert getattr(attempted, name) == getattr(baseline, name), name
    # They are derived properties, not fields: nothing a constructor or a
    # `dataclasses.replace` could be handed.
    assert {f.name for f in dataclasses.fields(AuditConfig)} == {"path"}


def test_the_derived_directories_are_posix_shaped_under_the_container_path():
    # The container path is POSIX; the derivation must not mangle it.
    config = _load()
    assert PurePosixPath(str(config.audit.triage_recordings_dir)).parts[:3] == (
        "/",
        "data",
        "audit",
    )


# --- 2.5 The sample config agrees with the defaults --------------------------


def _sample_raw():
    return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))


def test_the_sample_config_carries_the_new_sections_at_their_defaults():
    config = Config.load(SAMPLE, env={})
    assert config.triage_recording == TriageRecordingConfig()
    assert config.replay == ReplayConfig()


def test_the_sample_config_spells_out_every_new_section_key():
    raw = _sample_raw()
    assert raw["triage_recording"] == {"enabled": True}
    assert raw["replay"] == {"judge_model": "claude-fable-5-1", "judge_effort": "high"}


def test_the_sample_config_leaves_the_profile_keys_absent_but_documented():
    # Absent is their default, so they go in commented out: a live key would turn
    # "follow the chat profile" into a second value to keep in step.
    raw = _sample_raw()
    for key in EVENT_KEYS:
        assert key not in raw["agent"], key
    text = SAMPLE.read_text(encoding="utf-8")
    for key in EVENT_KEYS:
        assert re.search(rf"^\s*#\s*{key}:", text, re.MULTILINE), key
    config = Config.load(SAMPLE, env={})
    assert _event_triple(config) == _chat_triple(config)


def test_the_commented_profile_example_is_itself_a_valid_config():
    # Uncommenting the documented example must load, not fail on first use.
    text = SAMPLE.read_text(encoding="utf-8")
    example = {}
    for line in text.splitlines():
        stripped = line.strip()
        for key in EVENT_KEYS:
            if re.match(rf"#\s*{key}:", stripped):
                example[key] = yaml.safe_load(stripped.lstrip("#"))[key]
    assert set(example) == set(EVENT_KEYS)
    config = _load(dict(example))
    assert _event_triple(config) == tuple(example[k] for k in EVENT_KEYS)
