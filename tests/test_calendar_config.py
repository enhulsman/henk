"""maintenance-calendar group 1: configuration and the scoped credential.

From the maintenance-calendar spec: *One calendar through Henk's own scoped
credential* (invalid key, missing key, pinned ``token_uri``, key never printed,
variable removed from the environment) and *The calendar capability is disabled by
default and inert when off* (config refusals and defaults), plus design D1 and D11.

Every key is generated per run (tests/calendar_fixtures.py); no identifier here is
real.
"""

from __future__ import annotations

import json
import os
import traceback

import pytest

from henk.calendar.token import (
    GOOGLE_TOKEN_URI,
    CredentialError,
    ServiceAccountKey,
    parse_service_account_key,
)
from henk.config import (
    CalendarConfig,
    Config,
    ConfigError,
    PersonalDataConfig,
    Secrets,
)
from tests.calendar_fixtures import (
    CALENDAR_ID,
    DROP,
    ENV_VAR,
    OWNER_EMAIL,
    PEER_EMAIL,
    SA_EMAIL,
    encode,
    enabled_raw,
    key_b64,
    key_document,
    minimal_raw,
    private_key_lines,
)
from tests.test_config import SAMPLE


def _load(raw, env):
    return Config.from_dict(raw, env=env)


def _secret_fragments(b64: str) -> list[str]:
    """What must never appear: the email, every PEM line, the base64 value."""
    return [SA_EMAIL, b64, *private_key_lines()]


def _assert_no_secret(text: str, b64: str) -> None:
    for fragment in _secret_fragments(b64):
        assert fragment not in text, fragment[:40]


def _full_error_text(excinfo) -> str:
    """The message AND the whole printed traceback, chained causes included."""
    return str(excinfo.value) + "".join(
        traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb)
    )


# --- Defaults: disabled and inert -----------------------------------------


def test_calendar_defaults_match_the_design():
    config = _load(minimal_raw(), env={})
    assert config.calendar == CalendarConfig()
    cal = config.calendar
    assert cal.enabled is False
    assert cal.calendar_id == ""
    assert cal.hold_alerts is True
    assert cal.lookahead_days == 14
    assert cal.refresh_seconds == 300
    assert cal.grace_minutes == 30
    assert cal.max_window_hours == 8
    assert cal.stale_after_seconds == 21600
    assert cal.timeout_seconds == 10
    assert config.personal_data.calendar_creator_allowlist == ()
    assert config.secrets.google_calendar_key is None


def test_the_sample_config_ships_the_calendar_disabled_and_the_allowlist_empty():
    config = Config.load(SAMPLE, env={})
    assert config.calendar.enabled is False
    assert config.calendar.calendar_id == ""
    assert config.personal_data.calendar_creator_allowlist == ()
    assert config.secrets.google_calendar_key is None


def test_disabled_calendar_never_reads_the_variable():
    # Inert when off: a junk (or a real) value in the environment is not decoded,
    # so a disabled deploy cannot fail on it and never holds the key in memory.
    config = _load(minimal_raw(enabled=False), env={ENV_VAR: "%%% not base64 %%%"})
    assert config.secrets.google_calendar_key is None


def test_disabled_calendar_does_not_need_a_calendar_id_or_a_zone():
    raw = minimal_raw(enabled=False)
    del raw["owner"]["timezone"]
    config = _load(raw, env={})
    assert config.calendar.enabled is False


def test_disabled_calendar_ignores_out_of_range_bounds():
    # The spec scopes the range refusals to an enabled calendar (decision recorded
    # in notes/apply-decisions.md, group 1).
    config = _load(minimal_raw(enabled=False, grace_minutes=999), env={})
    assert config.calendar.grace_minutes == 999


# --- Enabled: a valid key loads -------------------------------------------


def test_enabled_with_a_valid_key_loads_the_credential():
    config = _load(enabled_raw(), env={ENV_VAR: key_b64()})
    key = config.secrets.google_calendar_key
    assert isinstance(key, ServiceAccountKey)
    assert key.client_email == SA_EMAIL
    assert key.token_uri == GOOGLE_TOKEN_URI
    assert key.private_key.startswith("-----BEGIN PRIVATE KEY-----")
    assert config.calendar.enabled is True
    assert config.calendar.calendar_id == CALENDAR_ID


def test_enabled_settings_are_read_from_the_section():
    raw = enabled_raw(
        hold_alerts=False,
        lookahead_days=7,
        refresh_seconds=120,
        grace_minutes=45,
        max_window_hours=6,
        stale_after_seconds=3600,
        timeout_seconds=5,
    )
    cal = _load(raw, env={ENV_VAR: key_b64()}).calendar
    assert (cal.hold_alerts, cal.lookahead_days, cal.refresh_seconds) == (False, 7, 120)
    assert (cal.grace_minutes, cal.max_window_hours) == (45, 6)
    assert (cal.stale_after_seconds, cal.timeout_seconds) == (3600, 5)


def test_calendar_id_is_stripped():
    raw = enabled_raw(calendar_id=f"  {CALENDAR_ID}  ")
    assert _load(raw, env={ENV_VAR: key_b64()}).calendar.calendar_id == CALENDAR_ID


# --- Enabled: the key refusals --------------------------------------------


def test_missing_key_refuses_startup_naming_the_variable():
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={})
    assert ENV_VAR in str(excinfo.value)


def test_empty_key_refuses_startup_naming_the_variable():
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: "   "})
    assert ENV_VAR in str(excinfo.value)


def test_invalid_token_uri_refuses_startup_without_echoing_the_key():
    # Scenario "Invalid key refuses startup without echoing it".
    b64 = key_b64(token_uri="https://evil.example/token")
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: b64})
    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "token_uri" in message
    _assert_no_secret(_full_error_text(excinfo), b64)
    # Nor the attacker's URI: the error names the rule, not the value.
    assert "evil.example" not in message


@pytest.mark.parametrize(
    "token_uri",
    [
        DROP,
        "",
        "http://oauth2.googleapis.com/token",
        "https://oauth2.googleapis.com/token/",
        "https://oauth2.googleapis.com/token?x=1",
        "https://OAUTH2.googleapis.com/token",
        "https://oauth2.googleapis.com.evil.example/token",
        None,
    ],
)
def test_only_the_exact_pinned_token_uri_is_accepted(token_uri):
    b64 = key_b64(token_uri=token_uri)
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: b64})
    assert "token_uri" in str(excinfo.value)
    _assert_no_secret(_full_error_text(excinfo), b64)


@pytest.mark.parametrize(
    "case, b64, rule",
    [
        ("not base64", "!!!not-base64!!!", "base64"),
        ("not json", encode(b"\x00\x01 not json"), "JSON"),
        ("json array", encode([1, 2, 3]), "JSON object"),
        ("wrong type", key_b64(type="authorized_user"), "type"),
        ("type missing", key_b64(type=DROP), "type"),
        ("no client_email", key_b64(client_email=DROP), "client_email"),
        ("empty client_email", key_b64(client_email=""), "client_email"),
        ("non-string email", key_b64(client_email=7), "client_email"),
        ("no private_key", key_b64(private_key=DROP), "private_key"),
        ("empty private_key", key_b64(private_key=""), "private_key"),
        (
            "unparsable private_key",
            key_b64(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
            "private_key",
        ),
    ],
)
def test_every_invalid_key_refuses_startup_naming_the_variable_and_rule(case, b64, rule):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: b64})
    message = str(excinfo.value)
    assert ENV_VAR in message, case
    assert rule in message, case
    _assert_no_secret(_full_error_text(excinfo), b64)


def test_a_non_rsa_private_key_is_refused():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    b64 = key_b64(private_key=pem)
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: b64})
    assert "RSA" in str(excinfo.value)
    for line in pem.splitlines():
        if line.strip():
            assert line not in _full_error_text(excinfo)


def test_errors_do_not_chain_the_underlying_exception():
    # A chained JSONDecodeError or binascii.Error prints its own message in the
    # traceback; `from None` keeps the printed trail to the value-free message.
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(), env={ENV_VAR: encode(b"{not json")})
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_parser_errors_never_carry_the_value():
    # The token module's own error type, under the config layer.
    for b64 in (key_b64(token_uri="https://evil.example/token"), "@@@"):
        with pytest.raises(CredentialError) as excinfo:
            parse_service_account_key(b64)
        _assert_no_secret(_full_error_text(excinfo), b64)


# --- Enabled: the settings refusals ---------------------------------------


def test_enabled_without_a_calendar_id_refused():
    # Scenario "Enabled without a calendar id refused".
    for calendar_id in ("", "   ", None):
        raw = enabled_raw(calendar_id=calendar_id)
        with pytest.raises(ConfigError) as excinfo:
            _load(raw, env={ENV_VAR: key_b64()})
        assert "calendar.calendar_id" in str(excinfo.value)


def test_enabled_without_an_owner_timezone_refused():
    raw = enabled_raw()
    del raw["owner"]["timezone"]
    with pytest.raises(ConfigError) as excinfo:
        _load(raw, env={ENV_VAR: key_b64()})
    message = str(excinfo.value)
    assert "owner.timezone" in message
    assert "calendar.enabled" in message


def test_reminders_still_require_the_zone_on_their_own():
    raw = minimal_raw()
    del raw["owner"]["timezone"]
    raw["reminders"] = {"enabled": True}
    with pytest.raises(ConfigError) as excinfo:
        _load(raw, env={})
    assert "reminders.enabled" in str(excinfo.value)


@pytest.mark.parametrize("grace", [-1, 241, 10_000])
def test_grace_outside_0_to_240_refused(grace):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(grace_minutes=grace), env={ENV_VAR: key_b64()})
    assert "calendar.grace_minutes" in str(excinfo.value)


@pytest.mark.parametrize("grace", [0, 240])
def test_grace_bounds_are_inclusive(grace):
    config = _load(enabled_raw(grace_minutes=grace), env={ENV_VAR: key_b64()})
    assert config.calendar.grace_minutes == grace


@pytest.mark.parametrize("hours", [0, 25, -3])
def test_max_window_outside_1_to_24_refused(hours):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(max_window_hours=hours), env={ENV_VAR: key_b64()})
    assert "calendar.max_window_hours" in str(excinfo.value)


@pytest.mark.parametrize("hours", [1, 24])
def test_max_window_bounds_are_inclusive(hours):
    config = _load(enabled_raw(max_window_hours=hours), env={ENV_VAR: key_b64()})
    assert config.calendar.max_window_hours == hours


@pytest.mark.parametrize(
    "name", ["lookahead_days", "refresh_seconds", "stale_after_seconds", "timeout_seconds"]
)
def test_other_durations_must_be_positive_when_enabled(name):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(**{name: 0}), env={ENV_VAR: key_b64()})
    assert f"calendar.{name}" in str(excinfo.value)


@pytest.mark.parametrize("name", ["grace_minutes", "max_window_hours", "lookahead_days"])
def test_non_numeric_settings_refused_naming_the_setting(name):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(**{name: "soon"}), env={ENV_VAR: key_b64()})
    assert f"calendar.{name}" in str(excinfo.value)


@pytest.mark.parametrize("value", ["true", "false", 1, None])
def test_enabled_flag_must_be_a_real_boolean(value):
    # bool("false") is True: a quoted rollback would silently enable the calendar.
    with pytest.raises(ConfigError) as excinfo:
        _load(minimal_raw(enabled=value), env={ENV_VAR: key_b64()})
    assert "calendar.enabled" in str(excinfo.value)


def test_hold_alerts_flag_must_be_a_real_boolean():
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(hold_alerts="false"), env={ENV_VAR: key_b64()})
    assert "calendar.hold_alerts" in str(excinfo.value)


def test_unknown_calendar_key_refused():
    # A silently ignored `calendar.calendars` or `calendar.scope` would let the
    # owner believe a second calendar or a wider scope was configured.
    with pytest.raises(ConfigError) as excinfo:
        _load(minimal_raw(enabled=False, scope="calendar"), env={})
    assert "calendar.scope" in str(excinfo.value)


def test_calendar_section_must_be_a_mapping():
    raw = minimal_raw()
    raw["calendar"] = ["enabled"]
    with pytest.raises(ConfigError):
        _load(raw, env={})


# --- The creator allowlist ------------------------------------------------


def test_creator_allowlist_is_parsed_stripped_and_case_folded():
    raw = minimal_raw()
    raw["personal_data"] = {
        "calendar_creator_allowlist": [f"  {OWNER_EMAIL.upper()} ", "", PEER_EMAIL]
    }
    config = _load(raw, env={})
    assert config.personal_data.calendar_creator_allowlist == (OWNER_EMAIL, PEER_EMAIL)


@pytest.mark.parametrize("bad", [OWNER_EMAIL, [7], {"a": 1}])
def test_creator_allowlist_must_be_a_list_of_strings(bad):
    raw = minimal_raw()
    raw["personal_data"] = {"calendar_creator_allowlist": bad}
    with pytest.raises(ConfigError) as excinfo:
        _load(raw, env={})
    assert "personal_data.calendar_creator_allowlist" in str(excinfo.value)


def test_creator_allowlist_defaults_empty():
    assert PersonalDataConfig().calendar_creator_allowlist == ()


# --- Never printed --------------------------------------------------------


def test_the_key_never_appears_in_the_config_repr():
    b64 = key_b64()
    config = _load(enabled_raw(), env={ENV_VAR: b64})
    for text in (repr(config), str(config), repr(config.secrets), str(config.secrets)):
        _assert_no_secret(text, b64)
    _assert_no_secret(repr(config.secrets.google_calendar_key), b64)
    _assert_no_secret(str(config.secrets.google_calendar_key), b64)


def test_secrets_field_is_declared_repr_false():
    import dataclasses

    field = {f.name: f for f in dataclasses.fields(Secrets)}["google_calendar_key"]
    assert field.repr is False


def test_service_account_key_repr_is_redacted():
    key = parse_service_account_key(key_b64())
    assert "redacted" in repr(key)
    _assert_no_secret(repr(key), key_b64())


def test_the_decoded_document_is_not_retained_beyond_the_three_fields():
    # The key object carries what the token flow needs and nothing else: no
    # project id, no client id, no private_key_id to leak through a future dump.
    key = parse_service_account_key(key_b64())
    import dataclasses

    assert {f.name for f in dataclasses.fields(key)} == {
        "client_email",
        "private_key",
        "token_uri",
    }


# --- Not inherited: the variable is popped --------------------------------


async def test_build_runtime_pops_the_variable_when_enabled(monkeypatch, tmp_path):
    from henk.runtime import build_runtime

    b64 = key_b64()
    monkeypatch.setenv(ENV_VAR, b64)
    raw = enabled_raw()
    raw["audit"] = {"path": str(tmp_path / "audit.jsonl")}
    raw["store"] = {"path": str(tmp_path / "store.db")}
    config = Config.from_dict(raw, env=os.environ)
    assert config.secrets.google_calendar_key is not None
    app, client = build_runtime(config)
    try:
        assert ENV_VAR not in os.environ
        # ...and the decoded key the runtime needs is still there.
        assert config.secrets.google_calendar_key.client_email == SA_EMAIL
    finally:
        await client.aclose()


async def test_build_runtime_pops_the_variable_when_disabled(monkeypatch):
    # The rollback path keeps the `.env` line (design Migration Plan step 6), so a
    # disabled runtime must not hand it to the SDK subprocess either.
    from henk.runtime import build_runtime

    monkeypatch.setenv(ENV_VAR, key_b64())
    config = Config.load(SAMPLE, env=os.environ)
    app, client = build_runtime(config)
    try:
        assert ENV_VAR not in os.environ
    finally:
        await client.aclose()


async def test_the_pop_happens_before_the_session_factory_is_built(monkeypatch):
    # The SDK CLI subprocess inherits os.environ (sdk_session.py:335-347); the
    # factories are the first objects that can start it, so the variable must
    # already be gone when they are constructed.
    import henk.runtime as runtime

    seen: list[bool] = []
    real = runtime.SdkSessionFactory

    def spy(*args, **kwargs):
        seen.append(ENV_VAR in os.environ)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime, "SdkSessionFactory", spy)
    monkeypatch.setenv(ENV_VAR, key_b64())
    app, client = runtime.build_runtime(Config.load(SAMPLE, env=os.environ))
    try:
        assert seen and not any(seen)
    finally:
        await client.aclose()


def test_the_key_document_fixture_is_well_formed():
    # Guards the fixture itself: a key that failed for a fixture reason would make
    # every refusal test above pass vacuously.
    document = key_document()
    assert json.loads(json.dumps(document))["type"] == "service_account"
    assert parse_service_account_key(key_b64()).client_email == SA_EMAIL


# --- Non-finite settings (Codex adversarial-review follow-up) ---------------
#
# YAML reads `.nan`, `.inf` and `-.inf` as floats. NaN fails every comparison, so
# `value <= 0` let it through, and a NaN `stale_after_seconds` made the window
# provider's age check always false: a stale snapshot would hold alerts forever.
# `int(inf)` raised OverflowError, which escaped as a crash rather than a
# ConfigError. Every numeric calendar setting must be finite.

_NUMERIC_CALENDAR_SETTINGS = (
    "lookahead_days",
    "refresh_seconds",
    "grace_minutes",
    "max_window_hours",
    "stale_after_seconds",
    "timeout_seconds",
)


def _yaml_number(text: str):
    import yaml

    return yaml.safe_load(f"value: {text}")["value"]


@pytest.mark.parametrize("name", _NUMERIC_CALENDAR_SETTINGS)
@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
def test_non_finite_settings_refused_when_enabled(name, text):
    value = _yaml_number(text)
    assert isinstance(value, float)
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(**{name: value}), env={ENV_VAR: key_b64()})
    message = str(excinfo.value)
    assert f"calendar.{name}" in message
    assert "finite" in message


@pytest.mark.parametrize("name", _NUMERIC_CALENDAR_SETTINGS)
@pytest.mark.parametrize("text", ["nan", "inf"])
def test_non_finite_strings_refused_when_enabled(name, text):
    with pytest.raises(ConfigError) as excinfo:
        _load(enabled_raw(**{name: text}), env={ENV_VAR: key_b64()})
    assert f"calendar.{name}" in str(excinfo.value)


@pytest.mark.parametrize("name", _NUMERIC_CALENDAR_SETTINGS)
@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
def test_non_finite_settings_in_a_disabled_section_fall_back_to_defaults(name, text):
    # Disabled stays inert: no refusal, no crash, and no non-finite value kept for
    # a later flip of `enabled` to inherit unchecked.
    config = _load(minimal_raw(enabled=False, **{name: _yaml_number(text)}), env={})
    assert getattr(config.calendar, name) == getattr(CalendarConfig, name)
