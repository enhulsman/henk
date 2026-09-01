"""Config surface for read depth: the named-query tool and the docs corpus (§2).

From `specs/homelab-tools` ("Range queries return bounded summaries" — the
configured maximum point count) and `specs/homelab-docs` ("Safe defaults apply
without a config file entry", "Default-deny path allowlist enforced at index
build", "Read results are byte-capped", "Every result carries a freshness stamp",
"Corpus failures are honest").

Two properties do most of the work here, and both are about the *loader* rather
than the dataclasses:

- **The builder must read every key.** rp5's `config.yaml` is skip-worktree'd and
  will never carry a new key, so a `from_dict` that silently ignores a section is
  indistinguishable from one that honours it — until someone sets the key on the
  host and nothing changes. Every test below goes through `Config.from_dict`.
- **The corpus ships disabled and the queries ship enabled.** The query half needs
  no host provisioning; the corpus half is dead until a clone, a timer, and an
  allowlist exist on rp5 (migration steps 2-6). A later edit must not silently
  flip either, so both are pinned.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from henk.config import (
    Config,
    ConfigError,
    HomelabDocsConfig,
    HomelabQueryConfig,
    PersonalDataConfig,
)
from tests.test_config import SAMPLE, _minimal_raw


def _raw(**sections):
    raw = _minimal_raw("+31600000000")
    raw.update(sections)
    return raw


# --- Defaults reach production through the loader (task 2.1 / 2.3 / 2.4) ---

#: Every new setting, its section, and its designed default. A table rather than
#: sixteen hand-written assertions, because the property under test is the same
#: each time and the interesting part is the value.
QUERY_DEFAULTS = {
    "enabled": True,
    "query_range_max_points": 60,
}

DOCS_DEFAULTS = {
    "enabled": False,
    "path": "",
    "stamp_max_age_seconds": 93600.0,  # 26h, the fleet's own BackupStale bound
    "read_byte_budget": 8000,
    "search_result_count": 5,
}


def test_no_read_depth_section_at_all_resolves_every_default():
    # The deployed file carries none of these keys and never will: this is the
    # path production actually takes (homelab-docs spec, "Safe defaults apply
    # without a config file entry").
    raw = _minimal_raw("+1")
    assert "homelab_query" not in raw and "homelab_docs" not in raw
    config = Config.from_dict(raw, env={})
    for name, expected in QUERY_DEFAULTS.items():
        assert getattr(config.homelab_query, name) == expected, name
    for name, expected in DOCS_DEFAULTS.items():
        assert getattr(config.homelab_docs, name) == expected, name
    assert config.personal_data.docs_path_allowlist == ()


def test_an_empty_section_resolves_every_default():
    # `homelab_docs: {}` in YAML loads as an empty mapping (or None). Neither may
    # crash, and neither may produce a different value than an absent section.
    for empty in ({}, None):
        config = Config.from_dict(
            _raw(homelab_query=empty, homelab_docs=empty), env={}
        )
        for name, expected in QUERY_DEFAULTS.items():
            assert getattr(config.homelab_query, name) == expected, name
        for name, expected in DOCS_DEFAULTS.items():
            assert getattr(config.homelab_docs, name) == expected, name


def test_every_query_key_is_actually_read_by_the_builder():
    # The real trap is a builder that never reads the key. Values are distinct
    # from the defaults, so a knob wired to the wrong key cannot pass by luck.
    supplied = {"enabled": False, "query_range_max_points": 12}
    assert set(supplied) == set(QUERY_DEFAULTS)
    assert not any(supplied[k] == QUERY_DEFAULTS[k] for k in supplied)
    config = Config.from_dict(_raw(homelab_query=supplied), env={})
    for name, value in supplied.items():
        assert getattr(config.homelab_query, name) == value, name


def test_every_docs_key_is_actually_read_by_the_builder():
    supplied = {
        "enabled": True,
        "path": "/docs",
        "stamp_max_age_seconds": 3600,
        "read_byte_budget": 1234,
        "search_result_count": 3,
    }
    assert set(supplied) == set(DOCS_DEFAULTS)
    assert not any(supplied[k] == DOCS_DEFAULTS[k] for k in supplied)
    config = Config.from_dict(_raw(homelab_docs=supplied), env={})
    assert config.homelab_docs.enabled is True
    assert config.homelab_docs.path == "/docs"
    assert config.homelab_docs.stamp_max_age_seconds == 3600.0
    assert config.homelab_docs.read_byte_budget == 1234
    assert config.homelab_docs.search_result_count == 3


def test_the_dataclass_defaults_and_the_loader_agree():
    # 2.4: one source per default. If a literal were duplicated into the builder,
    # this is where the two copies would drift apart.
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.homelab_query == HomelabQueryConfig()
    assert config.homelab_docs == HomelabDocsConfig()


def test_the_sample_config_carries_the_same_values_as_the_defaults():
    config = Config.load(SAMPLE, env={})
    assert config.homelab_query == HomelabQueryConfig()
    assert config.homelab_docs == HomelabDocsConfig()


# --- The two enable flags, pinned in both directions (task 2.2) ------------


def test_the_corpus_ships_disabled_and_the_queries_ship_enabled():
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.homelab_query.enabled is True, (
        "the query half needs no host provisioning — it rides the existing "
        "tag:henk egress to Gatus and Prometheus, so it ships on"
    )
    assert config.homelab_docs.enabled is False, (
        "the corpus half is dead until rp5 carries a clone, a pull timer and an "
        "allowlist (migration steps 2-6); enabling it here would register a tool "
        "that can only fail"
    )
    # And the same, read from the file the repo actually ships.
    sample = Config.load(SAMPLE, env={})
    assert sample.homelab_query.enabled is True
    assert sample.homelab_docs.enabled is False


def test_both_flags_can_be_flipped_from_config():
    config = Config.from_dict(
        _raw(
            homelab_query={"enabled": False},
            homelab_docs={"enabled": True, "path": "/opt/homelab-docs"},
        ),
        env={},
    )
    assert config.homelab_query.enabled is False
    assert config.homelab_docs.enabled is True


# --- The corpus path allowlist (task 2.3, homelab-docs spec) ---------------


def test_the_corpus_allowlist_defaults_empty_fail_closed():
    config = Config.from_dict(_minimal_raw("+1"), env={})
    assert config.personal_data.docs_path_allowlist == ()
    assert Config.load(SAMPLE, env={}).personal_data.docs_path_allowlist == ()


def test_the_corpus_allowlist_is_read_from_config():
    raw = _minimal_raw("+1")
    raw["personal_data"] = {
        "docs_path_allowlist": ["devices/workstation.md", "services/"]
    }
    allowlist = Config.from_dict(raw, env={}).personal_data.docs_path_allowlist
    assert allowlist == ("devices/workstation.md", "services/")


def test_the_corpus_allowlist_sits_with_the_other_tier_w_allowlists():
    # Structural, not stylistic: `todo_note_allowlist` lives here rather than in a
    # todo-tool section, because the data axis is reviewed as one surface. The
    # corpus allowlist is the same kind of boundary (design D13) and belongs
    # beside it, not in the tool's own section.
    fields = {f.name for f in dataclasses.fields(PersonalDataConfig)}
    assert fields == {
        "todo_note_allowlist",
        "taiga_project_allowlist",
        "docs_path_allowlist",
    }


# --- Validation: positive values, named errors (task 2.5) ------------------

#: (section, key, the value that must be refused). Zero and negative for every
#: bound: a zero byte budget serves empty sections, a zero result count surfaces
#: nothing while claiming to have searched, a zero staleness bound marks every
#: result stale, and a zero point count makes the range step a division by zero.
POSITIVE_SETTINGS = [
    ("homelab_docs", "stamp_max_age_seconds"),
    ("homelab_docs", "read_byte_budget"),
    ("homelab_docs", "search_result_count"),
    ("homelab_query", "query_range_max_points"),
]


@pytest.mark.parametrize("section,key", POSITIVE_SETTINGS)
@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_bound_fails_load_naming_the_setting(section, key, bad):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(**{section: {key: bad}}), env={})
    # Naming the setting is the requirement, not merely failing: the operator is
    # editing a file on rp5 over SSH and the message is all they get.
    assert key in str(exc.value)
    assert section in str(exc.value)


@pytest.mark.parametrize("section,key", POSITIVE_SETTINGS)
def test_a_non_positive_bound_fails_even_with_the_capability_disabled(section, key):
    # Not conditional on the flag: a bad value that only surfaces when someone
    # flips `enabled` surfaces on the host, at the worst possible moment.
    raw = _raw(**{section: {key: 0, "enabled": False}})
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw, env={})
    assert key in str(exc.value)


@pytest.mark.parametrize("section,key", POSITIVE_SETTINGS)
def test_a_non_numeric_bound_fails_naming_the_setting(section, key):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(**{section: {key: "soon"}}), env={})
    assert key in str(exc.value)


def test_a_range_point_count_below_two_is_refused():
    # A single point is not a trend: first, last, min and max collapse onto one
    # sample and "direction of travel" becomes unanswerable, so the summary the
    # spec requires cannot be produced.
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(homelab_query={"query_range_max_points": 1}), env={})
    assert "query_range_max_points" in str(exc.value)
    assert Config.from_dict(
        _raw(homelab_query={"query_range_max_points": 2}), env={}
    ).homelab_query.query_range_max_points == 2


def test_the_corpus_enabled_without_a_path_fails_load_naming_both_keys():
    # D11 layer 1: a config error is a startup refusal, like every other refusal
    # in this loader. Host state (a missing directory) is deliberately NOT one —
    # that registers the tool and fails honestly per call.
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(_raw(homelab_docs={"enabled": True}), env={})
    message = str(exc.value)
    assert "homelab_docs.path" in message
    assert "homelab_docs.enabled" in message


def test_the_corpus_enabled_with_a_blank_path_fails_the_same_way():
    for blank in ("", "   "):
        with pytest.raises(ConfigError):
            Config.from_dict(
                _raw(homelab_docs={"enabled": True, "path": blank}), env={}
            )


def test_a_configured_path_with_the_corpus_disabled_is_accepted():
    # The path may be staged before the flag is flipped — that is migration step
    # 5 before step 6, and it must load.
    config = Config.from_dict(
        _raw(homelab_docs={"enabled": False, "path": "/opt/homelab-docs"}), env={}
    )
    assert config.homelab_docs.path == "/opt/homelab-docs"
    assert config.homelab_docs.enabled is False


# --- No address, and no second timeout source (task 2.3 / 2.6) -------------

#: Anything that would put an address, a label value, or a node↔series mapping
#: into configuration. `dns_performance`'s mapping is DERIVED at query time from
#: job-labelled series precisely so neither this repo nor rp5's config.yaml has to
#: hold one (homelab-tools spec, "DNS node identification is derived, never
#: configured or hardcoded").
FORBIDDEN_KEY_SUBSTRINGS = (
    "address",
    "instance",
    "server",
    "scrape_url",
    "scrapeurl",
    "host",
    "url",
    "ip",
    "node",
    "mapping",
)

_ADDRESS_SHAPED = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|://")

READ_DEPTH_CONFIG_CLASSES = (HomelabQueryConfig, HomelabDocsConfig)


@pytest.mark.parametrize("cls", READ_DEPTH_CONFIG_CLASSES)
def test_no_read_depth_config_key_names_an_address_or_a_mapping(cls):
    for field in dataclasses.fields(cls):
        lowered = field.name.lower()
        for forbidden in FORBIDDEN_KEY_SUBSTRINGS:
            assert forbidden not in lowered, (
                f"{cls.__name__}.{field.name} names {forbidden!r}: the node↔series "
                "mapping is derived at query time, never configured"
            )


@pytest.mark.parametrize("cls", READ_DEPTH_CONFIG_CLASSES)
def test_no_read_depth_config_default_holds_an_address(cls):
    for field in dataclasses.fields(cls):
        value = getattr(cls(), field.name)
        if isinstance(value, str):
            assert not _ADDRESS_SHAPED.search(value), f"{cls.__name__}.{field.name}"


def test_the_loaded_sample_config_holds_no_address_in_the_new_sections():
    config = Config.load(SAMPLE, env={})
    for section in (config.homelab_query, config.homelab_docs):
        for field in dataclasses.fields(section):
            value = getattr(section, field.name)
            if isinstance(value, str):
                assert not _ADDRESS_SHAPED.search(value), field.name
    for entry in config.personal_data.docs_path_allowlist:
        assert not _ADDRESS_SHAPED.search(entry)


@pytest.mark.parametrize("cls", READ_DEPTH_CONFIG_CLASSES)
def test_no_read_depth_config_key_adds_a_second_timeout_source(cls):
    # Backend timeouts REUSE endpoints.gatus / endpoints.prometheus. A second
    # source would let one tool time out at a different bound than the other
    # against the same backend.
    for field in dataclasses.fields(cls):
        assert "timeout" not in field.name.lower(), field.name


def test_the_backend_timeouts_still_come_from_the_endpoint_sections():
    config = Config.load(SAMPLE, env={})
    assert config.gatus.timeout_seconds > 0
    assert config.prometheus.timeout_seconds > 0


# --- The surface is exactly this and nothing wider ------------------------


def test_the_read_depth_config_surface_is_exactly_this():
    # Enumerated rather than sampled: a forbidden-name blocklist only catches the
    # names someone thought of. An added key fails here and has to be justified
    # in the diff, which is the point.
    assert {f.name for f in dataclasses.fields(HomelabQueryConfig)} == set(
        QUERY_DEFAULTS
    )
    assert {f.name for f in dataclasses.fields(HomelabDocsConfig)} == set(
        DOCS_DEFAULTS
    )


def test_no_read_depth_key_admits_a_free_text_query_or_a_widened_grant():
    # There is deliberately no key that admits a PromQL string, a metric name, a
    # label selector, or a filesystem path as a tool argument (homelab-tools
    # spec, "No free-text query path": no configuration option, debug flag, or
    # code path admits a model-supplied query expression).
    fields = {f.name for f in dataclasses.fields(HomelabQueryConfig)} | {
        f.name for f in dataclasses.fields(HomelabDocsConfig)
    }
    for forbidden in (
        "promql",
        "query",
        "expression",
        "metric",
        "selector",
        "allow_free_text",
        "debug",
        "raw",
        "tier",
        "authorization",
        "turn_scope",
    ):
        offenders = [f for f in fields if forbidden in f and f != "query_range_max_points"]
        assert not offenders, f"{offenders} would admit {forbidden}"
