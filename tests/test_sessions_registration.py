"""Audit assertions and registration for `sessions_read` (§7).

From `specs/session-awareness`:

- **"Session snapshot content is excluded from audit records"** — both scenarios:
  *Not result-capturing* (the tool is absent from the opt-in set) and *Audit
  records are body-independent* (the same session written with a fully populated
  snapshot and with an empty one produces byte-identical records once timestamps
  are normalised).
- **"Session awareness is configured under `sessions` and defaults to off"** —
  *Disabled by omission* (a configuration omitting every new key registers no
  tool) and the registration half of *Full registry composes* (the enumerated
  toolset and the production registry agree on the count; its sibling lives at
  `tests/test_config_sessions.py`).
- **"Henk enforces its own default-deny gate on the snapshot"** — the
  registration half of *Absent key resolves to an empty allowlist*: the registry
  passes `personal_data.session_project_allowlist` into the tool, and an empty
  one registers with the same startup WARNING as an empty `todo_read` scope.
- **"Selection is a gate whose non-pass outcomes are terminal"** — *No request at
  construction*: building the registry with the capability on dials nothing.

Three properties this file exists to hold, and none of them adds a mechanism:

- **The audit assertions run the real path.** Result capture is global and
  default-deny (design D11): nothing here filters anything, and no mutation below
  removes a filter. The tool is executed against a `MockTransport`, its genuinely
  rendered text is fed to the production `_StatsAccumulator` through the
  read-depth decision-17 harness (`tests/test_audit_read_depth.py`), and the
  assertions read the JSONL back off disk. The harness's own helpers are imported
  rather than copied, so a weakening of the harness cannot pass here while
  failing there.
- **"No request" is asserted on the transport.** `_RefusingTransport` fails the
  test if registration touches the network, in either flag state.
- **The startup WARNING is pinned by shape, not by wording.** The `todo_read`
  precedent (`henk/tools/__init__.py:144-146`) and this one are matched against
  one regex, so a session warning that drifted from the precedent's sentence
  fails even though it would still "mention the allowlist".

Placeholder labels only (`alpha`, `beta`, `zeta-nine`) and placeholder pane ids:
no estate value reaches this repo.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from henk.agent.sdk_session import RESULT_CAPTURING_TOOLS
from henk.agent.session import HANDOFF_TOOL_NAME
from henk.config import Config
from henk.tools import build_production_registry
from henk.tools.base import ToolClass
from henk.tools.sessions_read import SessionsReadTool
from tests.test_audit_read_depth import _TIMESTAMP, _audit_for, assert_body_absent
from tests.test_config import _minimal_raw
from tests.test_read_depth_registration import _RefusingTransport, _config
from tests.test_tools_sessions_read import Poll, build, frame, session, snapshot

#: The three placeholder labels the populated snapshot carries. `zeta-nine` is the
#: distinctive one: it appears nowhere else in this repository, so its absence
#: from an audit record is evidence rather than coincidence.
LABELS = ("alpha", "beta", "zeta-nine")

#: Both startup warnings must match this. Two capture groups so the test can also
#: say *which* tool and *which* key each record named.
WARNING_SHAPE = re.compile(
    r"^(\w+) registered but always empty — no allowlist configured "
    r"\((personal_data\.\w+)\); it will surface nothing$"
)

POPULATED = [
    session(project="alpha", status="working", age_s=42, pane="w1:p1"),
    session(project="beta", status="blocked", age_s=900, pane="w2:p3"),
    session(project="zeta-nine", status="idle", age_s=7200, pane="w3:p1"),
]


# --- Helpers ---------------------------------------------------------------


async def _rendered(sessions: list[dict]) -> str:
    """The tool's genuine output for one snapshot, through the real poll path."""
    tool = build(Poll(frame(snapshot(sessions=sessions))), allowlist=LABELS)
    result = await tool.run()
    assert result.ok and result.content, "the tool must produce a body to withhold"
    return result.content


def _sessions_raw(*, enabled: bool = True, allowlist=("alpha", "beta"), **endpoints):
    """A configuration mapping with the two keys the capability needs."""
    raw = _minimal_raw("+31600000000")
    raw["sessions"] = {"enabled": enabled}
    if allowlist is not None:
        raw["personal_data"] = {"session_project_allowlist": list(allowlist)}
    if endpoints:
        raw["endpoints"] = endpoints
    return raw


def _sessions_config(*, enabled: bool = True, allowlist=("alpha", "beta"), env=None):
    return Config.from_dict(
        _sessions_raw(enabled=enabled, allowlist=allowlist), env=env or {}
    )


def _registry(config: Config, transport: httpx.MockTransport | None = None):
    client = httpx.AsyncClient(transport=transport or _RefusingTransport())
    return build_production_registry(config, client)


# --- 7.1 The tool does not opt into result capture ------------------------


def test_sessions_read_does_not_opt_into_result_capture():
    # The whole-set pin — "capture is global, default-deny, and exactly one tool
    # opts in" — already exists three times and is deliberately NOT restated here:
    # `tests/test_audit_read_depth.py:219`, `tests/test_sdk_session_stats.py:258`
    # and `tests/test_sdk_session_stats.py:311`. Any of the three fails if the set
    # grows, including by this tool; what is new here is the named absence, which
    # is what the spec scenario asserts.
    assert SessionsReadTool.name not in RESULT_CAPTURING_TOOLS
    assert SessionsReadTool.name != HANDOFF_TOOL_NAME


# --- 7.2 The audit record is body-independent -----------------------------


@pytest.mark.parametrize("populated", [True, False])
async def test_no_rendered_snapshot_text_reaches_an_audit_record(
    tmp_path: Path, populated: bool
):
    content = await _rendered(POPULATED if populated else [])
    if populated:
        # Non-vacuity: the rendering really does carry the labels whose absence is
        # asserted below, so this is about the audit path rather than about an
        # empty string.
        for label in LABELS:
            assert label in content
    else:
        assert "No live sessions" in content

    directory = tmp_path / ("populated" if populated else "empty")
    raw, records, control = await _audit_for(
        directory, [("sessions_read", "read-only", content)]
    )
    # The call itself stays fully auditable — only the payload is gone.
    call = records[-1]["tool_calls"][0]
    assert call["name"] == "sessions_read"
    assert call["tool_class"] == "read-only"
    assert call["result_id"] is None
    assert_body_absent(raw, control, content)
    assert "zeta-nine" not in raw


async def test_the_two_snapshots_write_byte_identical_records(tmp_path: Path):
    """The scenario as written: twice, once full and once empty, normalised."""
    full = await _rendered(POPULATED)
    empty = await _rendered([])
    assert full != empty, "the two runs must differ in what they render"

    populated_raw, _, _ = await _audit_for(
        tmp_path / "full", [("sessions_read", "read-only", full)]
    )
    empty_raw, _, _ = await _audit_for(
        tmp_path / "none", [("sessions_read", "read-only", empty)]
    )
    assert _TIMESTAMP.sub('"at": 0', populated_raw) == _TIMESTAMP.sub(
        '"at": 0', empty_raw
    )


async def test_the_harness_would_catch_a_session_leak(tmp_path: Path):
    """The leak detector: the same text through the one tool that DOES opt in.

    Without this, every assertion above would hold against a harness that wrote
    an empty log, and the mutation "add `sessions_read` to the opt-in set" would
    be indistinguishable from a passing suite.
    """
    content = await _rendered(POPULATED)
    raw, records, _ = await _audit_for(
        tmp_path / "leak", [(HANDOFF_TOOL_NAME, "notify-only", content)]
    )
    assert "zeta-nine" in raw
    assert records[-1]["tool_calls"][0]["result_id"] == content


# --- 7.3 Registration is gated on the flag --------------------------------


def test_the_section_absent_registers_no_session_tool():
    # *Disabled by omission*: rp5's config carries none of these keys, so "absent"
    # is the state the deployed host is actually in.
    transport = _RefusingTransport()
    registry = _registry(_config(), transport)
    assert "sessions_read" not in registry.names()
    assert transport.requests == []


def test_the_flag_off_registers_no_session_tool():
    transport = _RefusingTransport()
    config = _sessions_config(enabled=False, allowlist=("alpha",))
    registry = _registry(config, transport)
    assert "sessions_read" not in registry.names()
    assert transport.requests == []


def test_the_flag_off_registry_is_the_pre_change_one():
    # The kill switch is incomplete otherwise: with the capability off the
    # registry must be the one that shipped before this change existed. The
    # read-depth pair is switched off here for the same reason its own test does
    # it (`tests/test_read_depth_registration.py:222-233`): `homelab_query` ships
    # enabled, so leaving it on would compare against a different baseline.
    raw = _sessions_raw(enabled=False, allowlist=("alpha",))
    raw["homelab_query"] = {"enabled": False}
    raw["homelab_docs"] = {"enabled": False}
    off = _registry(Config.from_dict(raw, env={}))
    assert set(off.names()) == {
        "homelab_health",
        "todo_read",
        "notify",
        "publish_handoff",
        "store_memory",
        "capture",
        "inbox_read",
    }


def test_the_flag_on_registers_a_read_only_tool_with_no_parameters():
    transport = _RefusingTransport()
    registry = _registry(_sessions_config(), transport)
    assert "sessions_read" in registry.names()
    tool = registry.get("sessions_read")
    assert isinstance(tool, SessionsReadTool)
    assert tool.tool_class is ToolClass.READ_ONLY
    assert tool.parameters["properties"] == {}
    assert tool.parameters["additionalProperties"] is False
    # *No request at construction*, asserted on the transport rather than on a
    # return value.
    assert transport.requests == []


# --- 7.4 The allowlist and the endpoint reach the tool from config --------


def test_the_registry_passes_the_session_allowlist_through():
    tool = _registry(_sessions_config(allowlist=("alpha", "beta"))).get("sessions_read")
    # From `personal_data`, where the other Tier-W boundaries live — not from a
    # key on the `sessions` section, and not from another capability's allowlist.
    assert tool.effective_allowlist == ("alpha", "beta")


def test_an_absent_allowlist_key_reaches_the_tool_as_empty():
    # *Absent key resolves to an empty allowlist*, through `Config.from_dict` and
    # then through the registry: the tool registers and surfaces nothing (G0).
    config = _sessions_config(allowlist=None)
    tool = _registry(config).get("sessions_read")
    assert tool.effective_allowlist == ()


def test_an_empty_allowlist_registers_but_warns(caplog):
    # The `todo_read` precedent (`henk/tools/__init__.py:144-146`): registering
    # with an empty effective allowlist is safe but useless, so it is loud at
    # startup rather than silently unhelpful. Half-applying migration step 8.8 —
    # the flag without the allowlist — is exactly this state.
    config = _sessions_config(allowlist=())
    with caplog.at_level("WARNING"):
        registry = _registry(config)
    assert "sessions_read" in registry.names()
    warned = [
        record.message
        for record in caplog.records
        if "session_project_allowlist" in record.message
    ]
    assert len(warned) == 1, caplog.records
    assert "sessions_read" in warned[0]


def test_the_session_warning_has_the_same_shape_as_the_todo_read_one(caplog):
    # Both are emitted by the same registry build: the minimal config leaves
    # `todo_note_allowlist` empty too. One regex for both, so a session warning
    # that drifted from the precedent's sentence fails here even though it would
    # still "mention the allowlist".
    with caplog.at_level("WARNING"):
        _registry(_sessions_config(allowlist=()))
    matched = {}
    for record in caplog.records:
        found = WARNING_SHAPE.match(record.message)
        if found:
            matched[found.group(1)] = found.group(2)
    assert matched == {
        "todo_read": "personal_data.todo_note_allowlist",
        "sessions_read": "personal_data.session_project_allowlist",
    }, caplog.records


def test_a_populated_allowlist_emits_no_session_warning(caplog):
    with caplog.at_level("WARNING"):
        _registry(_sessions_config(allowlist=("alpha",)))
    assert not [
        record
        for record in caplog.records
        if "session_project_allowlist" in record.message
    ]


async def test_the_poll_rides_the_ntfy_endpoint_and_the_ntfy_timeout():
    """No new URL, topic, timeout or secret key: `endpoints.ntfy` and `sessions`.

    Driven through a recording transport rather than read off private fields, so
    the assertion is about the request that actually leaves the tool. The gatus
    timeout is set to a different value in the same config, so a wiring that
    reached for the wrong section cannot pass by coincidence.
    """
    raw = _sessions_raw(
        allowlist=("alpha",),
        gatus={"base_url": "http://g", "timeout_seconds": 3.25},
        prometheus={"base_url": "http://p"},
        taiga={"base_url": "http://t"},
        todo={"base_url": "http://d"},
        ntfy={
            "base_url": "http://n",
            "topic": "henk",
            "timeout_seconds": 7.5,
        },
    )
    raw["sessions"]["topic"] = "henk-sessions"
    config = Config.from_dict(raw, env={"NTFY_TOKEN": "placeholder-token"})
    assert config.ntfy.timeout_seconds != config.gatus.timeout_seconds

    poll = Poll(frame(snapshot(sessions=POPULATED)))
    client = httpx.AsyncClient(transport=httpx.MockTransport(poll))
    registry = build_production_registry(config, client)
    result = await registry.get("sessions_read").run()
    assert result.ok

    assert len(poll.requests) == 1
    request = poll.requests[0]
    assert str(request.url).startswith(
        f"{config.ntfy.base_url}/{config.sessions.topic}/json?"
    )
    assert request.url.params["poll"] == "1"
    # The credential Henk already holds, not a new secret.
    assert request.headers["Authorization"] == "Bearer placeholder-token"
    timeout = request.extensions["timeout"]
    assert set(timeout.values()) == {config.ntfy.timeout_seconds}
