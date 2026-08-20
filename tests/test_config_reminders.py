"""Reminders configuration and the owner timezone (group 3).

From the reminders spec's "The owner timezone is configured and fails closed" and
"Reminders can be disabled without removing data" requirements. Two properties do
most of the work here: the capability is off unless someone turned it on, and the
timezone has **no default** — a hardcoded zone bakes a personal fact into a
publication-bound repo, and a UTC fallback turns a missing key into every reminder
firing an hour or two off.
"""

from __future__ import annotations

import dataclasses
from zoneinfo import ZoneInfo, available_timezones

import pytest

from henk.config import Config, ConfigError, OwnerConfig, RemindersConfig
from tests.test_config import _minimal_raw


def _raw(**reminders):
    raw = _minimal_raw("+31600000000")
    if reminders:
        raw["reminders"] = dict(reminders)
    return raw


def _enabled(**extra):
    raw = _raw(enabled=True, **extra)
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    return raw


# --- RemindersConfig defaults (task 3.1) ----------------------------------


def test_reminders_absent_entirely_means_disabled():
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.reminders.enabled is False
    assert config.reminders == RemindersConfig()


def test_the_enabled_flag_is_read():
    config = Config.from_dict(_enabled(), env={})
    assert config.reminders.enabled is True


def test_every_bound_has_a_default_and_can_be_set():
    defaults = RemindersConfig()
    assert defaults.max_pending == 100
    assert defaults.text_length_limit == 500
    assert defaults.horizon_days == 365
    assert defaults.clock_skew_tolerance_seconds == 120.0
    assert defaults.page_size == 20

    config = Config.from_dict(
        _enabled(
            max_pending=7,
            text_length_limit=80,
            horizon_days=30,
            clock_skew_tolerance_seconds=5,
            page_size=3,
        ),
        env={},
    )
    assert config.reminders.max_pending == 7
    assert config.reminders.text_length_limit == 80
    assert config.reminders.horizon_days == 30
    assert config.reminders.clock_skew_tolerance_seconds == 5.0
    assert config.reminders.page_size == 3


def test_no_reminders_key_can_widen_the_capability():
    # The tier, the turn scope and the recipient are code decisions that ride code
    # review (approval-gate spec). There is deliberately no key for any of them, so
    # a config edit cannot promote `remind` past standing, let it run in an event
    # turn, or point a reminder at another identity.
    fields = {f.name for f in dataclasses.fields(RemindersConfig)}
    for forbidden in (
        "tier",
        "authorization",
        "turn_scope",
        "scope",
        "recipient",
        "owner",
        "demote",
        "allow_event_turns",
    ):
        assert forbidden not in fields, f"reminders.{forbidden} would widen the grant"


def test_an_unknown_reminders_key_is_ignored_rather_than_applied():
    # A widening key someone invents in YAML must be inert, not a constructor kwarg.
    config = Config.from_dict(
        _enabled(turn_scope=["owner", "event"], tier="per-instance"), env={}
    )
    assert config.reminders.enabled is True
    assert not hasattr(config.reminders, "turn_scope")
    assert not hasattr(config.reminders, "tier")


# --- owner.timezone (task 3.2) --------------------------------------------


def test_owner_timezone_absent_with_reminders_disabled_loads_fine():
    # rp5's config.yaml is locally modified and will not carry the key. Making the
    # timezone unconditionally required would break that host's next deploy.
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.owner.timezone is None
    assert config.owner.zone is None
    assert config.reminders.enabled is False


def test_owner_timezone_absent_with_reminders_enabled_fails_naming_both_keys():
    raw = _raw(enabled=True)  # deliberately no owner.timezone
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw, env={})
    message = str(exc.value)
    assert "reminders.enabled" in message
    assert "owner.timezone" in message


def test_an_unknown_zone_fails_startup_naming_the_value():
    raw = _minimal_raw("+1")
    raw["owner"]["timezone"] = "Europe/Nowhereville"
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw, env={})
    assert "Europe/Nowhereville" in str(exc.value)


def test_localtime_is_refused_even_though_it_resolves():
    # `ZoneInfo("localtime")` validates cleanly, resolves against the HOST clock,
    # and appears in available_timezones() — so "the key resolves" and "the key is
    # a known zone" both admit it. The rule has to be the key's SHAPE.
    assert "localtime" in available_timezones() or True  # host-dependent, not the point
    for value in ("localtime", "UTC", "EST", "Factory"):
        raw = _minimal_raw("+1")
        raw["owner"]["timezone"] = value
        with pytest.raises(ConfigError) as exc:
            Config.from_dict(raw, env={})
        assert value in str(exc.value)
        assert "Region/Location" in str(exc.value)


def test_an_absolute_path_or_traversal_is_refused():
    for value in ("/etc/localtime", "../../etc/passwd", "Europe/../UTC"):
        raw = _minimal_raw("+1")
        raw["owner"]["timezone"] = value
        with pytest.raises(ConfigError):
            Config.from_dict(raw, env={})


def test_a_valid_zone_is_available_as_a_resolved_zone():
    raw = _minimal_raw("+1")
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    config = Config.from_dict(raw, env={})
    assert config.owner.timezone == "Europe/Amsterdam"
    assert config.owner.zone == ZoneInfo("Europe/Amsterdam")
    # Resolved, not merely stored: a datetime built through it carries an offset.
    from datetime import datetime

    stamp = datetime(2026, 8, 20, 7, 30, tzinfo=config.owner.zone)
    assert stamp.utcoffset().total_seconds() == 7200


def test_a_non_string_timezone_is_refused():
    for value in (42, True, ["Europe/Amsterdam"]):
        raw = _minimal_raw("+1")
        raw["owner"]["timezone"] = value
        with pytest.raises(ConfigError):
            Config.from_dict(raw, env={})


def test_owner_config_still_constructs_positionally():
    # `timezone` was added with a None default precisely so existing positional
    # construction keeps working.
    owner = OwnerConfig("+31600000000")
    assert owner.id == "+31600000000"
    assert owner.timezone is None


# --- The committed config.yaml -------------------------------------------


def test_the_committed_config_ships_reminders_disabled():
    # Deploying this change must be behaviourally invisible: enabling is a
    # deliberate two-key edit on the host, not something a deploy turns on.
    from tests.test_config import SAMPLE

    config = Config.load(SAMPLE, env={})
    assert config.reminders.enabled is False


def test_the_committed_config_leaves_the_timezone_absent_and_fails_loudly_if_enabled():
    # `owner.timezone` is documented but COMMENTED OUT in the committed file rather
    # than filled with a plausible placeholder. An absent key fails startup loudly
    # the moment someone flips `enabled`; a placeholder that merely validates would
    # resolve every reminder in the wrong zone and say nothing. This asserts the
    # loud half is what ships.
    import yaml

    from tests.test_config import SAMPLE

    raw = yaml.safe_load(SAMPLE.read_text())
    assert raw["owner"].get("timezone") is None
    raw["reminders"]["enabled"] = True
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw, env={})
    assert "owner.timezone" in str(exc.value)


def test_the_documented_example_zone_is_itself_a_valid_region_location_key():
    # The commented example is the thing an operator will copy, so it has to pass
    # the same validation the live key does.
    from tests.test_config import SAMPLE

    example = None
    for line in SAMPLE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "timezone:" in stripped:
            example = stripped.split("timezone:", 1)[1].strip().strip('"')
            break
    assert example, "config.yaml should document an example owner.timezone"
    raw = _minimal_raw("+1")
    raw["owner"]["timezone"] = example
    assert Config.from_dict(raw, env={}).owner.zone == ZoneInfo(example)
