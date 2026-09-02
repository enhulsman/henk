"""Config surface for session awareness (§2 of the `session-awareness` change).

From `specs/session-awareness`:

- **"Session awareness is configured under `sessions` and defaults to off"** — the
  four keys and their defaults, default resolution when the section is absent,
  unconditional validation (positive durations, `lookback_seconds` at least
  `stale_after_seconds`, a single-topic `topic`), no new secret and no new timeout
  key, and the prompt summary that appears only when the tool is registered.
- **"Henk enforces its own default-deny gate on the snapshot"** — the
  `personal_data.session_project_allowlist` half: absent resolves to empty, entries
  match exactly after a whitespace strip, and an entry empty after the strip is
  discarded without broadening scope.

Two properties do the work here, both about the *loader* rather than the
dataclasses, and both inherited from the read-depth tests
(`tests/test_config_read_depth.py`):

- **The builder must read every key.** rp5's `config.yaml` is skip-worktree'd and
  will never carry a new key, so a `from_dict` that silently ignores the section
  is indistinguishable from one that honours it until someone sets the key on the
  host and nothing changes. Every test here goes through `Config.from_dict`.
- **The capability ships off.** The publisher, the ntfy user, the topic grant and
  the label allowlist must all exist on the host first; `enabled: true` before
  that registers a tool that can only report G0 or G2. Pinned in the dataclass and
  in the loader (the owner-acknowledgement finding-2 pattern).

The allowlist assertions deliberately go through a *consumer* — the
`normalise_label_allowlist` helper the future `sessions_read` gate reads, and a
match predicate standing in for it — rather than reading the dataclass attribute
and calling that a behaviour.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from henk.config import (
    COUNT_WORDS,
    BASE_TOOL_SUMMARIES,
    DOCS_TOOL_SUMMARIES,
    QUERY_TOOL_SUMMARIES,
    REMINDER_TOOL_SUMMARIES,
    SESSIONS_TOOL_SUMMARIES,
    Config,
    ConfigError,
    PersonalDataConfig,
    Secrets,
    SessionsConfig,
    build_system_prompt,
    normalise_label_allowlist,
)
from tests.test_config import SAMPLE, _minimal_raw


def _raw(**sections):
    raw = _minimal_raw("+31600000000")
    raw.update(sections)
    return raw


#: Every new `sessions` key and its designed default. A table rather than four
#: hand-written assertions per test: the property is the same each time and the
#: interesting part is the value.
SESSIONS_DEFAULTS = {
    "enabled": False,
    "topic": "henk-sessions",
    "stale_after_seconds": 1500,  # 25 min: 5x the publisher's 300s tick
    "lookback_seconds": 21600,  # 6h, the ntfy instance's cache duration
}


# --- Defaults reach production through the loader (task 2.1) ---------------


def test_no_sessions_section_at_all_resolves_every_default():
    # The deployed file carries none of these keys and never will: this is the
    # path production actually takes.
    raw = _minimal_raw("+1")
    assert "sessions" not in raw
    config = Config.from_dict(raw, env={})
    for name, expected in SESSIONS_DEFAULTS.items():
        assert getattr(config.sessions, name) == expected, name


def test_an_empty_section_resolves_every_default():
    # `sessions: {}` in YAML loads as an empty mapping (or None). Neither may
    # crash, and neither may produce a different value than an absent section.
    for empty in ({}, None):
        config = Config.from_dict(_raw(sessions=empty), env={})
        for name, expected in SESSIONS_DEFAULTS.items():
            assert getattr(config.sessions, name) == expected, name


def test_every_sessions_key_is_actually_read_by_the_builder():
    # The real trap is a builder that never reads the key. Values are distinct
    # from the defaults, so a knob wired to the wrong key cannot pass by luck.
    supplied = {
        "enabled": True,
        "topic": "henk-sessions-test",
        "stale_after_seconds": 900,
        "lookback_seconds": 3600,
    }
    assert set(supplied) == set(SESSIONS_DEFAULTS)
    assert not any(supplied[k] == SESSIONS_DEFAULTS[k] for k in supplied)
    config = Config.from_dict(_raw(sessions=supplied), env={})
    for name, value in supplied.items():
        assert getattr(config.sessions, name) == value, name


def test_the_dataclass_defaults_and_the_loader_agree():
    # One source per default. If a literal were duplicated into the builder, this
    # is where the two copies would drift apart.
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.sessions == SessionsConfig()


def test_the_durations_are_read_as_integers():
    # Both are interpolated into ntfy's `since=<N>s` query parameter, where a
    # float renders `1500.0s` and the server rejects it.
    config = Config.from_dict(
        _raw(sessions={"stale_after_seconds": "1200", "lookback_seconds": 7200.0}),
        env={},
    )
    assert config.sessions.stale_after_seconds == 1200
    assert config.sessions.lookback_seconds == 7200
    assert isinstance(config.sessions.stale_after_seconds, int)
    assert isinstance(config.sessions.lookback_seconds, int)


# --- The capability ships off, pinned twice (task 2.2) --------------------


def test_a_config_omitting_every_new_key_ships_the_capability_off():
    # owner-acknowledgement finding 2: a default that is only on the dataclass is
    # not the deployed value when the builder reads an inline literal, so both
    # halves are pinned.
    assert SessionsConfig.enabled is False
    assert SessionsConfig().enabled is False
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.sessions.enabled is False, (
        "the publisher, the ntfy user, the topic grant and the label allowlist "
        "must exist on the host first; enabling here would register a tool that "
        "can only report that nothing is in scope"
    )
    assert Config.load(SAMPLE, env={}).sessions.enabled is False


def test_the_loader_takes_the_enabled_default_from_the_dataclass():
    # The literal half of finding 2, read off the source: `enabled` must fall back
    # to `SessionsConfig.enabled`, never to a hand-typed `True`/`False` that can
    # drift from the dataclass.
    source = inspect.getsource(Config.from_dict)
    assert 'sessions_sec.get("enabled", SessionsConfig.enabled)' in source


def test_the_flag_can_be_flipped_from_config():
    config = Config.from_dict(_raw(sessions={"enabled": True}), env={})
    assert config.sessions.enabled is True


# --- Validation: unconditional, named errors (task 2.3) -------------------

POSITIVE_SETTINGS = ["stale_after_seconds", "lookback_seconds"]


@pytest.mark.parametrize("key", POSITIVE_SETTINGS)
@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_duration_fails_load_naming_the_setting(key, bad):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(sessions={key: bad}), env={})
    # Naming the setting is the requirement, not merely failing: the operator is
    # editing a file on rp5 over SSH and the message is all they get.
    assert f"sessions.{key}" in str(exc.value)


@pytest.mark.parametrize("key", POSITIVE_SETTINGS)
def test_a_non_positive_duration_fails_even_with_the_capability_disabled(key):
    # Not conditional on the flag: a bad value that only surfaces when someone
    # flips `enabled` surfaces on the host, at the worst possible moment.
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(sessions={key: 0, "enabled": False}), env={})
    assert f"sessions.{key}" in str(exc.value)


@pytest.mark.parametrize("key", POSITIVE_SETTINGS)
def test_a_non_numeric_duration_fails_naming_the_setting(key):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(sessions={key: "soon"}), env={})
    assert f"sessions.{key}" in str(exc.value)


def test_a_lookback_shorter_than_the_staleness_bound_is_refused_naming_both():
    # A lookback below the staleness bound reports "nothing published" for a
    # snapshot that is merely stale — the one wrong answer this tool must not
    # give, because "no sessions" and "a stale report" are different facts.
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(
            _raw(sessions={"lookback_seconds": 600, "stale_after_seconds": 1500}),
            env={},
        )
    message = str(exc.value)
    assert "sessions.lookback_seconds" in message
    assert "sessions.stale_after_seconds" in message


def test_a_lookback_shorter_than_the_bound_is_refused_with_the_capability_off():
    with pytest.raises(ConfigError):
        Config.from_dict(
            _raw(
                sessions={
                    "enabled": False,
                    "lookback_seconds": 600,
                    "stale_after_seconds": 1500,
                }
            ),
            env={},
        )


def test_a_lookback_equal_to_the_staleness_bound_is_accepted():
    # The bound is "at least", not "greater than": equal windows mean the second
    # stage adds nothing, which is a legitimate (if pointless) configuration.
    config = Config.from_dict(
        _raw(sessions={"lookback_seconds": 1500, "stale_after_seconds": 1500}), env={}
    )
    assert config.sessions.lookback_seconds == 1500


@pytest.mark.parametrize(
    "bad_topic",
    [
        "henk-sessions,henk-events",  # a comma silently widens the read
        "henk-sessions/json",  # a slash changes the path, not the topic
        "",
        "   ",
    ],
)
def test_a_topic_that_is_not_one_name_is_refused_naming_the_setting(bad_topic):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(sessions={"topic": bad_topic}), env={})
    assert "sessions.topic" in str(exc.value)


def test_a_multi_topic_value_is_refused_with_the_capability_off():
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(
            _raw(sessions={"enabled": False, "topic": "henk-sessions,henk-events"}),
            env={},
        )
    assert "sessions.topic" in str(exc.value)


def test_a_non_string_topic_is_refused_naming_the_setting():
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(sessions={"topic": 7}), env={})
    assert "sessions.topic" in str(exc.value)


def test_the_staleness_default_is_the_designed_bound():
    # 1500s = 25 minutes, five publisher ticks. Pinned as a value because the
    # headline's fresh/stale split and the stage-1 poll window are both this
    # number, and lowering it silently would make every reply say "stale".
    assert SessionsConfig().stale_after_seconds == 1500
    assert Config.from_dict(_minimal_raw("+1"), env={}).sessions.stale_after_seconds == 1500


# --- No new secret, no new address, no second timeout source (task 2.4) ---


def test_the_secret_set_is_unchanged():
    # Session awareness introduces no credential: the publisher's write token
    # lives on the workstation and Henk reads the topic with the ntfy token it
    # already holds.
    assert {f.name for f in dataclasses.fields(Secrets)} == {
        "anthropic_credential",
        "taiga_token",
        "todo_token",
        "ntfy_token",
    }
    assert set(inspect.signature(Secrets.from_env).parameters) == {"env"}


@pytest.mark.parametrize(
    "forbidden", ["token", "secret", "url", "key", "timeout"]
)
def test_no_sessions_key_names_a_secret_an_address_or_a_timeout(forbidden):
    # The base URL and the request timeout REUSE `endpoints.ntfy`. A second source
    # would let two tools time out at different bounds against one backend, and a
    # token key here would be a second home for a credential.
    for field in dataclasses.fields(SessionsConfig):
        assert forbidden not in field.name.lower(), (
            f"SessionsConfig.{field.name} names {forbidden!r}: the base URL, the "
            "timeout and the credential all come from endpoints.ntfy and Secrets"
        )


def test_the_sessions_config_surface_is_exactly_this():
    # Enumerated rather than sampled: a forbidden-name blocklist only catches the
    # names someone thought of. An added key fails here and has to be justified in
    # the diff, which is the point.
    assert {f.name for f in dataclasses.fields(SessionsConfig)} == set(
        SESSIONS_DEFAULTS
    )


# --- The label allowlist, through its consumer (tasks 2.5 / 2.6) ----------


def _admits(config: Config, label: str) -> bool:
    """Stand-in for `sessions_read`'s label gate (task group 7 wires the real one).

    The tool receives the loader-normalised tuple and matches a snapshot's
    `project` against it exactly — so this predicate, not the attribute's repr, is
    what the allowlist tests below assert.
    """
    return label in config.personal_data.session_project_allowlist


def test_an_absent_allowlist_key_resolves_to_an_empty_allowlist():
    # Scenario: "Absent key resolves to an empty allowlist". Default-deny means
    # the miss is the empty tuple, and the empty tuple surfaces nothing.
    raw = _minimal_raw("+1")
    assert "personal_data" not in raw
    config = Config.from_dict(raw, env={})
    assert config.personal_data.session_project_allowlist == ()
    for label in ("henk", "config", "docs"):
        assert not _admits(config, label)


def test_an_empty_personal_data_section_resolves_to_an_empty_allowlist():
    for empty in ({}, None, {"todo_note_allowlist": []}):
        config = Config.from_dict(_raw(personal_data=empty), env={})
        assert config.personal_data.session_project_allowlist == ()
        assert not _admits(config, "henk")


def test_the_sample_config_ships_the_allowlist_empty_fail_closed():
    config = Config.load(SAMPLE, env={})
    assert config.personal_data.session_project_allowlist == ()
    assert not _admits(config, "henk")


def test_allowlisted_labels_are_admitted_and_others_are_not():
    # Scenario: "Non-allowlisted labels are filtered" — the config half of it.
    config = Config.from_dict(
        _raw(personal_data={"session_project_allowlist": ["henk", "config"]}), env={}
    )
    assert _admits(config, "henk")
    assert _admits(config, "config")
    assert not _admits(config, "docs")


def test_matching_is_exact_after_a_whitespace_strip():
    # 2.6: exact, not prefix and not substring. A prefix match would admit a
    # sibling root whose label merely starts the same way.
    config = Config.from_dict(
        _raw(personal_data={"session_project_allowlist": ["  henk  "]}), env={}
    )
    assert _admits(config, "henk")
    assert not _admits(config, "henk-docs")
    assert not _admits(config, "hen")
    assert not _admits(config, " henk ")


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   "])
def test_an_entry_empty_after_the_strip_is_discarded(blank):
    # Scenario: "Whitespace-only entries are discarded". The failure this guards
    # is a blank line in the host's YAML list becoming an entry that matches a
    # session whose `project` is itself empty — a widened scope from a typo.
    config = Config.from_dict(
        _raw(personal_data={"session_project_allowlist": ["henk", blank]}), env={}
    )
    assert config.personal_data.session_project_allowlist == ("henk",)
    assert not _admits(config, "")
    assert not _admits(config, blank)
    assert _admits(config, "henk")


def test_an_allowlist_of_only_blanks_surfaces_nothing():
    config = Config.from_dict(
        _raw(personal_data={"session_project_allowlist": ["", "   "]}), env={}
    )
    assert config.personal_data.session_project_allowlist == ()
    assert not _admits(config, "henk")
    assert not _admits(config, "")


def test_the_normalisation_lives_in_one_place_the_tool_will_read():
    # The helper is the single home for the strip-and-discard rule, so the gate in
    # `sessions_read` cannot re-derive it slightly differently. The loader's
    # output and the helper's output must be the same tuple for the same input.
    entries = ["  henk  ", "", "config", "   ", "docs"]
    assert normalise_label_allowlist(entries) == ("henk", "config", "docs")
    config = Config.from_dict(
        _raw(personal_data={"session_project_allowlist": list(entries)}), env={}
    )
    assert config.personal_data.session_project_allowlist == normalise_label_allowlist(
        entries
    )
    assert normalise_label_allowlist([]) == ()
    assert normalise_label_allowlist(None) == ()


def test_a_non_string_allowlist_entry_is_refused_naming_the_key():
    # Fail loud rather than coerce: `str(7)` would silently install a label no
    # publisher can emit, and the operator would see an empty result with no clue.
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(
            _raw(personal_data={"session_project_allowlist": ["henk", 7]}), env={}
        )
    assert "personal_data.session_project_allowlist" in str(exc.value)


# --- The sample config agrees with the defaults (task 2.8) ----------------


def test_the_sample_config_carries_the_same_values_as_the_defaults():
    config = Config.load(SAMPLE, env={})
    assert config.sessions == SessionsConfig()
    for name, expected in SESSIONS_DEFAULTS.items():
        assert getattr(config.sessions, name) == expected, name


def test_the_sample_config_spells_out_every_new_key():
    # The sample is documentation as much as configuration: a key present only as
    # a dataclass default is a key the owner never learns exists.
    import yaml

    raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    assert set(raw["sessions"]) == set(SESSIONS_DEFAULTS)
    for name, expected in SESSIONS_DEFAULTS.items():
        assert raw["sessions"][name] == expected, name
    assert raw["personal_data"]["session_project_allowlist"] == []


# --- The system prompt (tasks 2.10 / 2.11) --------------------------------

#: Verbatim from the spec's prompt-summary scenario and design D12.
SUMMARY_TEXT = (
    "the owner's Claude Code sessions on the workstation, as last reported. "
    "Every result states how old the report is; say so when it is stale, and "
    "never present listed sessions as all sessions."
)


def test_the_sessions_summary_is_one_entry_with_the_specified_text():
    assert SESSIONS_TOOL_SUMMARIES == (("sessions_read", SUMMARY_TEXT),)


def test_the_prompt_carries_the_summary_when_sessions_are_enabled():
    prompt = build_system_prompt(sessions_enabled=True)
    assert f"- sessions_read — {SUMMARY_TEXT}\n" in prompt
    # The two honesty clauses the spec scenario names, asserted as text rather
    # than trusted to the tuple above.
    assert "states how old the report is" in prompt
    assert "never present listed sessions as all sessions" in prompt


def test_the_prompt_omits_the_summary_when_sessions_are_disabled():
    for prompt in (build_system_prompt(), build_system_prompt(sessions_enabled=False)):
        assert "sessions_read" not in prompt
        assert "as last reported" not in prompt


def test_the_flag_off_prompt_is_byte_identical_to_the_flag_absent_prompt():
    # The kill switch is incomplete otherwise: with the capability off the prompt
    # must be the one that shipped before this change existed.
    assert build_system_prompt() == build_system_prompt(sessions_enabled=False)
    assert build_system_prompt(reminders_enabled=True) == build_system_prompt(
        reminders_enabled=True, sessions_enabled=False
    )


def test_the_loader_wires_the_flag_from_the_config_section():
    enabled = Config.from_dict(_raw(sessions={"enabled": True}), env={})
    assert "sessions_read" in enabled.agent.system_prompt
    disabled = Config.from_dict(_minimal_raw("+1"), env={})
    assert "sessions_read" not in disabled.agent.system_prompt


def test_the_full_capability_set_composes_and_its_count_matches_the_enumeration():
    """Task 2.10, prompt half.

    A thirteenth tool exceeds the old spelled-out count table and raises
    `KeyError` by design rather than shipping a wrong count, so this is the test
    that would have caught the omission. It asserts against the prompt's own
    summary tuples; the equality against the **production registry** is task 7.3's
    (the tool is not registered yet).
    """
    prompt = build_system_prompt(
        reminders_enabled=True,
        homelab_query_enabled=True,
        homelab_docs_enabled=True,
        sessions_enabled=True,
    )
    summaries = (
        BASE_TOOL_SUMMARIES
        + REMINDER_TOOL_SUMMARIES
        + QUERY_TOOL_SUMMARIES
        + DOCS_TOOL_SUMMARIES
        + SESSIONS_TOOL_SUMMARIES
    )
    assert len(summaries) == 13
    assert COUNT_WORDS[13] == "thirteen"
    assert f"exactly these {COUNT_WORDS[len(summaries)]}" in prompt
    # Every enumerated tool is actually enumerated, in the prompt, once.
    for name, summary in summaries:
        assert prompt.count(f"- {name} — {summary}\n") == 1


def test_the_count_table_covers_every_reachable_capability_combination():
    # Every combination of the four flags must compose: a missing count-table
    # entry is a KeyError at startup for one particular host configuration.
    for reminders in (False, True):
        for query in (False, True):
            for docs in (False, True):
                for sessions in (False, True):
                    prompt = build_system_prompt(
                        reminders_enabled=reminders,
                        homelab_query_enabled=query,
                        homelab_docs_enabled=docs,
                        sessions_enabled=sessions,
                    )
                    assert "exactly these " in prompt
