"""Registration and startup for the two read-depth tools (§7).

From `specs/homelab-docs` ("Corpus failures are honest, and host state never
kills the agent", "homelab_docs tool over a host-delivered read-only corpus"),
`specs/homelab-tools` ("Named-query tool with a closed query-name enum",
"endpoint_history's domain is discovered at first use and fails closed") and
design D11.

Three properties, and the middle one is the one the first design got wrong:

- **A config error kills startup.** `homelab_docs.enabled` with no `path` is a
  `ConfigError` out of the loader, exactly like every other refusal in it — so
  `python -m henk` dies at `Config.load` and never reaches `build_runtime`.
- **Host state does not.** A missing, empty, unreadable or unstamped corpus
  directory still starts the agent, and the tool is still **registered**: an
  absent tool produces no honest failure at all, leaving the model to answer
  documentation questions from its priors with no marker that the corpus was
  unreachable.
- **Nothing dials out while the runtime is being built.** `build_runtime`'s own
  docstring promises that, and `endpoint_history`'s domain is discovered at first
  use precisely so registration stays offline. Asserted on a transport that fails
  the test if it is called at all, not on a return value.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import httpx
import pytest

from henk.config import Config, ConfigError
from henk.runtime import build_runtime
from henk.tools import build_production_registry
from henk.tools.base import ToolClass
from henk.tools.homelab_docs import HomelabDocsTool
from henk.tools.homelab_query import HomelabQueryTool
from tests.corpus_fixture import build_corpus, stamp_path
from tests.test_config import SAMPLE, _minimal_raw


# --- Helpers ---------------------------------------------------------------


def _config(**sections) -> Config:
    raw = _minimal_raw("+31600000000")
    for name, values in sections.items():
        raw[name] = values
    return Config.from_dict(raw, env={})


class _RefusingTransport(httpx.MockTransport):
    """A transport that records every request and fails the test on any of them."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        raise AssertionError(
            f"runtime construction issued an HTTP request to {request.url}"
        )


def _registry(config: Config, transport: httpx.MockTransport | None = None):
    client = httpx.AsyncClient(transport=transport or _RefusingTransport())
    return build_production_registry(config, client)


# --- 7.1 Startup: a config error kills it ---------------------------------


def test_the_corpus_enabled_without_a_path_kills_startup(tmp_path: Path):
    """The startup path is `Config.load(path)` — the same call `__main__` makes."""
    text = SAMPLE.read_text(encoding="utf-8").replace(
        "homelab_docs:\n  enabled: false", "homelab_docs:\n  enabled: true"
    )
    broken = tmp_path / "config.yaml"
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        Config.load(broken, env={})
    assert "homelab_docs.path" in str(exc.value)
    assert "homelab_docs.enabled" in str(exc.value)


def test_the_corpus_config_error_is_the_same_shape_as_every_other_refusal():
    # "The way other config errors do": one exception type, raised by the loader,
    # before any runtime object exists. A capability that invented its own failure
    # mode would need its own handling at the entrypoint.
    with pytest.raises(ConfigError):
        _config(homelab_docs={"enabled": True})
    with pytest.raises(ConfigError):
        _config(homelab_docs={"enabled": True, "path": "   "})


# --- 7.1 Startup: host state does not kill it -----------------------------


def _host_state_paths(tmp_path: Path) -> dict[str, Path]:
    """The four host-state conditions D11 names, as configurable paths."""
    missing = tmp_path / "never-mounted"

    empty = tmp_path / "empty"
    empty.mkdir()

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "src").mkdir()
    os.chmod(unreadable, 0o000)

    unstamped = build_corpus(tmp_path, name="unstamped")
    stamp_path(unstamped).unlink()

    return {
        "missing": missing,
        "empty": empty,
        "unreadable": unreadable,
        "unstamped": unstamped,
    }


@pytest.fixture
def host_states(tmp_path: Path):
    paths = _host_state_paths(tmp_path)
    yield paths
    os.chmod(paths["unreadable"], 0o755)  # so pytest can clean tmp_path up


@pytest.mark.parametrize(
    "condition", ["missing", "empty", "unreadable", "unstamped"]
)
def test_bad_host_state_starts_the_agent_and_registers_the_tool(
    host_states, condition: str
):
    config = _config(
        homelab_docs={"enabled": True, "path": str(host_states[condition])},
        personal_data={"docs_path_allowlist": ["devices/", "index.mdx"]},
    )
    registry = _registry(config)
    assert "homelab_docs" in registry.names(), condition
    assert registry.get("homelab_docs").tool_class is ToolClass.READ_ONLY


@pytest.mark.parametrize(
    "condition", ["missing", "empty", "unreadable", "unstamped"]
)
async def test_bad_host_state_fails_per_call_rather_than_at_startup(
    host_states, condition: str
):
    config = _config(
        homelab_docs={"enabled": True, "path": str(host_states[condition])},
        personal_data={"docs_path_allowlist": ["devices/", "index.mdx"]},
    )
    tool = _registry(config).get("homelab_docs")
    result = await tool.run(action="search", query="resolver")
    # An unstamped corpus is deliberately SERVED with a marker rather than refused
    # (§5 decision 1); the other three are explicit per-call errors. Either way the
    # tool answered, which is the property this test is about.
    if condition == "unstamped":
        assert result.ok
        assert "freshness" in result.content.lower()
    else:
        assert result.ok is False
        assert str(host_states[condition]) in (result.error or "")


async def test_a_bad_corpus_path_does_not_stop_the_runtime_from_building(
    tmp_path: Path,
):
    """The whole runtime, not just the registry: an absent mount is not fatal."""
    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base,
        homelab_docs=dataclasses.replace(
            base.homelab_docs, enabled=True, path=str(tmp_path / "never-mounted")
        ),
    )
    app, client = build_runtime(config)
    try:
        assert app is not None
        assert "homelab_docs" in app._core._factory._registry.names()
    finally:
        await client.aclose()


# --- 7.2 The toolset: class, absence, and no construction-time traffic ----


def test_both_tools_register_read_only_when_enabled(tmp_path: Path):
    config = _config(
        homelab_query={"enabled": True},
        homelab_docs={"enabled": True, "path": str(tmp_path / "corpus")},
        personal_data={"docs_path_allowlist": ["index.mdx"]},
    )
    registry = _registry(config)
    for name in ("homelab_query", "homelab_docs"):
        assert name in registry.names()
        assert registry.get(name).tool_class is ToolClass.READ_ONLY
    # Read-only means the gate is bypassed by classification: neither tool may
    # appear among the mutating set, and neither declares a tier.
    assert {t.name for t in registry.mutating()}.isdisjoint(
        {"homelab_query", "homelab_docs"}
    )


def test_the_query_tool_is_unregistered_when_its_key_is_off():
    registry = _registry(_config(homelab_query={"enabled": False}))
    assert "homelab_query" not in registry.names()


def test_the_corpus_tool_is_unregistered_by_default():
    # The default is off, and the default is what rp5 runs: its `config.yaml` is
    # skip-worktree'd and carries no read-depth keys, so the loader's fallback IS
    # the deployed value. The corpus half stays dark until the owner flips it.
    config = _config()
    assert config.homelab_docs.enabled is False
    assert "homelab_docs" not in _registry(config).names()


def test_with_both_keys_off_the_toolset_is_the_pre_change_one():
    off = _config(homelab_query={"enabled": False}, homelab_docs={"enabled": False})
    assert set(_registry(off).names()) == {
        "homelab_health",
        "todo_read",
        "notify",
        "publish_handoff",
        "store_memory",
        "capture",
        "inbox_read",
    }


def test_no_network_call_occurs_while_the_registry_is_built(tmp_path: Path):
    transport = _RefusingTransport()
    config = _config(
        homelab_query={"enabled": True},
        homelab_docs={"enabled": True, "path": str(tmp_path / "corpus")},
        personal_data={"docs_path_allowlist": ["index.mdx"]},
    )
    registry = _registry(config, transport)
    assert transport.requests == []
    # Discovery is first-use, so the key set must be unset at registration — a
    # seeded set would mean discovery ran somewhere, transport or no transport.
    assert registry.get("homelab_query")._discovered_endpoints is None


async def test_no_network_call_occurs_while_the_runtime_is_built(
    monkeypatch, tmp_path: Path
):
    """`build_runtime`'s docstring promises this; here it is as an assertion.

    The shared tool client is built inside `build_runtime`, so the only way to
    watch it is to hand that construction a transport that fails on any request.
    """
    transport = _RefusingTransport()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real(*args, transport=transport, **kwargs),
    )
    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base,
        homelab_docs=dataclasses.replace(
            base.homelab_docs, enabled=True, path=str(tmp_path / "corpus")
        ),
    )
    app, client = build_runtime(config)
    try:
        assert transport.requests == []
        names = app._core._factory._registry.names()
        assert "homelab_query" in names and "homelab_docs" in names
    finally:
        await client.aclose()


# --- 7.3 Wiring: the tools take their settings from config ----------------


def test_the_query_tool_takes_both_backends_and_the_point_bound_from_config():
    config = _config(homelab_query={"enabled": True, "query_range_max_points": 12})
    tool = _registry(config).get("homelab_query")
    assert isinstance(tool, HomelabQueryTool)
    assert tool._gatus_url == config.gatus.base_url.rstrip("/")
    assert tool._prometheus_url == config.prometheus.base_url.rstrip("/")
    # No new timeout key: each backend keeps the timeout its own endpoint section
    # already declares, so two tools cannot time out at different bounds.
    assert tool._gatus_timeout == config.gatus.timeout_seconds
    assert tool._prometheus_timeout == config.prometheus.timeout_seconds
    assert tool._max_points == 12


def test_the_corpus_tool_takes_its_path_bounds_and_allowlist_from_config(
    tmp_path: Path,
):
    config = _config(
        homelab_docs={
            "enabled": True,
            "path": str(tmp_path / "corpus"),
            "stamp_max_age_seconds": 3600,
            "read_byte_budget": 1234,
            "search_result_count": 3,
        },
        personal_data={"docs_path_allowlist": ["devices/", "index.mdx"]},
    )
    tool = _registry(config).get("homelab_docs")
    assert isinstance(tool, HomelabDocsTool)
    assert tool._path == str(tmp_path / "corpus")
    assert tool._stamp_max_age == 3600.0
    assert tool._read_byte_budget == 1234
    assert tool._search_result_count == 3
    # The allowlist reaches the tool from `personal_data`, where the other two
    # Tier-W boundaries live — not from a key on the corpus section.
    assert tool.effective_allowlist == ("devices/", "index.mdx")


def test_an_empty_corpus_allowlist_registers_but_warns(tmp_path: Path, caplog):
    # The `todo_read` precedent: registering with an empty effective allowlist is
    # safe but useless, so it is loud at startup rather than silently unhelpful.
    config = _config(
        homelab_docs={"enabled": True, "path": str(tmp_path / "corpus")},
        personal_data={"docs_path_allowlist": []},
    )
    with caplog.at_level("WARNING"):
        registry = _registry(config)
    assert "homelab_docs" in registry.names()
    assert any(
        "docs_path_allowlist" in record.message for record in caplog.records
    ), caplog.records


# --- 7.3 The enumerated toolset keeps matching the registry ---------------


def test_the_system_prompt_enumerates_the_read_depth_tools_when_they_are_on(
    tmp_path: Path,
):
    from henk.config import build_system_prompt

    prompt = build_system_prompt(homelab_query_enabled=True, homelab_docs_enabled=True)
    assert "homelab_query" in prompt
    assert "homelab_docs" in prompt


def test_the_system_prompt_omits_a_tool_whose_key_is_off():
    from henk.config import build_system_prompt

    prompt = build_system_prompt(
        homelab_query_enabled=False, homelab_docs_enabled=False
    )
    assert "homelab_query" not in prompt
    assert "homelab_docs" not in prompt


@pytest.mark.parametrize("reminders_on", [False, True])
@pytest.mark.parametrize("docs_on", [False, True])
@pytest.mark.parametrize("query_on", [False, True])
def test_the_composed_prompt_matches_the_registry_this_config_produces(
    tmp_path: Path, query_on: bool, docs_on: bool, reminders_on: bool
):
    """The enumeration and the registry must agree in **content and order**.

    Every combination, not only the default one — including reminders, because
    read depth's two groups are appended after that one and an ordering mistake
    only shows when all three are on. That case is also the largest toolset this
    build can produce (twelve), which is the top of `COUNT_WORDS`: a thirteenth
    tool raises a `KeyError` here rather than shipping a prompt with a wrong count.
    """
    docs: dict = {"enabled": docs_on}
    if docs_on:
        docs["path"] = str(tmp_path / "corpus")
    sections: dict = {
        "homelab_query": {"enabled": query_on},
        "homelab_docs": docs,
        "personal_data": {"docs_path_allowlist": ["index.mdx"]},
    }
    if reminders_on:
        # The loader refuses `reminders.enabled` without `owner.timezone`.
        sections["reminders"] = {"enabled": True}
        sections["owner"] = {"id": "+31600000000", "timezone": "Europe/Amsterdam"}
    config = _config(**sections)
    registry = _registry(config)
    prompt = config.agent.system_prompt
    enumerated = [
        line[2:].split(" — ", 1)[0]
        for line in prompt.splitlines()
        if line.startswith("- ")
    ]
    assert enumerated == registry.names()
