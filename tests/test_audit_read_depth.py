"""Audit assertions for the two read-depth tools (§6).

From `specs/homelab-tools` ("Query result content is excluded from audit
records") and `specs/homelab-docs` ("Corpus text is excluded from audit
records").

**These are assertions about a mechanism that already exists, not a new one.**
Result capture is global and default-deny — :data:`RESULT_CAPTURING_TOOLS` is
opt-in per tool and exactly one tool (the handoff tool) opts in, a posture
established when a 2026-08-18 deploy found `homelab_health`'s tailnet addresses
inside audit records. Neither spec delta asks for per-tool redaction; both ask
that the global mechanism be shown to cover the new tools. So nothing here adds
a filter, and no mutation below removes one: removing result capture would break
`handoff_message_id` for every other tool.

Everything runs through the **real** path rather than a hand-built record: the
tools are executed against fakes/fixtures, their genuinely rendered output is fed
to the production :class:`_StatsAccumulator` in real SDK block shapes, the
resulting stats go through :class:`AgentCore`'s audit writer, and the assertions
read the JSONL file back off disk. `test_the_harness_would_catch_a_leak` runs the
same corpus text through the one tool that *does* opt in and asserts it lands in
the record — without it, every assertion here could pass against a harness that
writes nothing at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from henk.agent.core import AgentCore
from henk.agent.sdk_session import RESULT_CAPTURING_TOOLS, _StatsAccumulator
from henk.agent.session import HANDOFF_TOOL_NAME
from henk.audit import AuditLog
from henk.tools.homelab_docs import HomelabDocsTool
from henk.tools.homelab_query import HomelabQueryTool
from tests.conftest import EventSessionFactory, FakeChannel, make_clock
from tests.corpus_fixture import FRESH_NOW, FULL_ALLOWLIST, build_corpus

# Placeholder addresses only — no test in this repository carries a real one.
PROMETHEUS = "http://10.0.0.1:9090"
GATUS = "http://10.0.0.2:8080"

NOW = 1_756_000_000.0
STEP = 600.0

#: A corpus page that carries a placeholder address in its body (6.4). The
#: assertion below proves the tool really returns it before proving the audit
#: log really does not.
CORPUS_ADDRESS = "10.0.0.5"

#: A search query shaped like model-authored free text. It must not survive into
#: a record even though it is an *argument* rather than a result (D10).
SEARCH_QUERY = "SYNTHETIC-SEARCH-QUERY-SENTINEL resolver"


# --- Executing the real tools ---------------------------------------------


def _matrix(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def _series(metric: dict, values: list[float]) -> dict:
    start = NOW - STEP * (len(values) - 1)
    return {
        "metric": dict(metric),
        "values": [[start + STEP * i, f"{v}"] for i, v in enumerate(values)],
    }


def _query_tool(payload: dict) -> HomelabQueryTool:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        gatus_timeout=5.0,
        prometheus_timeout=5.0,
        max_points=60,
        clock=lambda: NOW,
    )


def _docs_tool(clone: Path) -> HomelabDocsTool:
    return HomelabDocsTool(
        path=str(clone),
        allowlist=FULL_ALLOWLIST,
        stamp_max_age_seconds=93600.0,
        read_byte_budget=8000,
        search_result_count=5,
        clock=lambda: FRESH_NOW,
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return build_corpus(tmp_path)


# --- The real audit path ---------------------------------------------------


def _write_session_audit(calls: list[tuple[str, str, str]], *, capture=None):
    """Run ``calls`` through the production stats accumulator and audit writer.

    ``calls`` is ``(tool_name, tool_class, result_text)``. ``capture`` overrides
    the opt-in set (used only by the leak-detector control); left at ``None`` the
    accumulator uses the production :data:`RESULT_CAPTURING_TOOLS`.

    Returns the raw JSONL text of the log and its parsed records — raw text as
    well as records, because "no field contains it" has to hold for nested
    structures and for keys, not only for the values a test remembers to walk.
    """
    accumulator = _StatsAccumulator(
        {name: tool_class for name, tool_class, _ in calls},
        capture_results_for=capture,
    )
    uses = [
        NS(id=f"tu-{index}", name=f"mcp__henk__{name}", input={})
        for index, (name, _, _) in enumerate(calls)
    ]
    accumulator.observe(NS(content=uses, model="claude-sonnet-5"))
    accumulator.observe(
        NS(
            content=[
                NS(tool_use_id=f"tu-{index}", content=text, is_error=False)
                for index, (_, _, text) in enumerate(calls)
            ]
        )
    )
    accumulator.observe(
        NS(
            content=None,
            total_cost_usd=0.01,
            usage={"input_tokens": 900, "output_tokens": 120},
        )
    )
    return accumulator.snapshot()


async def _audit_once(
    path: Path, calls: list[tuple[str, str, str]], *, capture=None
) -> tuple[str, list[dict]]:
    stats = _write_session_audit(calls, capture=capture)
    core = AgentCore(
        EventSessionFactory(reply="had a look", stats=stats),
        FakeChannel(),
        audit=AuditLog(path),
        model="claude-sonnet-5",
        clock=make_clock([0, 0, 1, 1]),
    )
    await core.process("what does the corpus say?")
    await core.aclose()
    raw = path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert records, "the harness wrote no audit record at all"
    return raw, records


async def _audit_for(
    tmp_path: Path, calls: list[tuple[str, str, str]], *, capture=None
) -> tuple[str, list[dict], str]:
    """Write the session twice — with the real bodies, and with empty ones.

    The second run is the **control**, and it is what makes the assertions below
    need no hand-maintained vocabulary exemption list. A record legitimately
    contains words like ``read-only`` and ``memory_hash``, which a rendered result
    may also contain; a token that appears in the control appeared without any
    result text existing, so it is the record's own vocabulary by construction
    rather than by somebody's judgement.
    """
    raw, records = await _audit_once(tmp_path / "audit.jsonl", calls, capture=capture)
    control, _ = await _audit_once(
        tmp_path / "control.jsonl",
        [(name, tool_class, "") for name, tool_class, _ in calls],
        capture=capture,
    )
    return raw, records, control


_TIMESTAMP = re.compile(r'"at": [0-9.]+')


def assert_body_absent(raw: str, control: str, body: str) -> None:
    """No line and no distinctive token of ``body`` survives into ``raw``.

    Three assertions of increasing strength: the body is absent whole; every line
    of it is absent; and the record is **byte-identical** to the one the same
    session produces with no result text at all, so the body had no influence on
    the record rather than merely no verbatim copy inside it.
    """
    assert body not in raw
    for line in body.splitlines():
        line = line.strip()
        if line:
            assert line not in raw, f"an audit record carried the result line {line!r}"
    tokens = {t for t in re.findall(r"[A-Za-z0-9_./:%-]{4,}", body)}
    leaked = sorted(t for t in tokens if t in raw and t not in control)
    assert not leaked, f"audit records carried result substrings: {leaked}"
    assert _TIMESTAMP.sub('"at": 0', raw) == _TIMESTAMP.sub('"at": 0', control)


# --- 6.1 Neither new tool is in the result-capturing set -------------------


def test_neither_read_depth_tool_opts_into_result_capture():
    assert HomelabQueryTool.name not in RESULT_CAPTURING_TOOLS
    assert HomelabDocsTool.name not in RESULT_CAPTURING_TOOLS


def test_the_capture_set_is_still_exactly_the_handoff_tool():
    # Stated as the whole set rather than as two absences: the property both spec
    # deltas assert is that capture is global and default-deny, and an opt-in set
    # that had grown would satisfy "the two new tools are absent" while having
    # re-opened the firehose for something else.
    assert RESULT_CAPTURING_TOOLS == frozenset({HANDOFF_TOOL_NAME})


# --- 6.2 No homelab_query result text, and no bound parameter -------------


async def test_no_query_result_text_or_parameter_reaches_an_audit_record(
    tmp_path: Path,
):
    payload = _matrix(
        _series({"job": "node-exporter-pi2", "instance": "10.0.0.7:9100"}, [3.0, 60.0, 128.7])
    )
    result = await _query_tool(payload).run(
        query_name="node_resource_trend", node="rp2", resource="swap_io", window="6h"
    )
    assert result.ok and result.content, "the query must produce a body to withhold"

    raw, records, control = await _audit_for(
        tmp_path, [("homelab_query", "read-only", result.content)]
    )
    # The call itself stays fully auditable — only the payload is gone.
    call = records[-1]["tool_calls"][0]
    assert call["name"] == "homelab_query"
    assert call["tool_class"] == "read-only"
    assert call["result_id"] is None
    assert_body_absent(raw, control, result.content)


@pytest.mark.parametrize("value", ["rp2", "swap_io", "6h", "node_resource_trend"])
async def test_bound_parameter_values_are_absent_from_the_record(
    tmp_path: Path, value: str
):
    # D10: parameters are deliberately not logged either. `rp2` and `6h` are
    # shorter than the token sweep's floor, so they are asserted by name.
    payload = _matrix(
        _series({"job": "node-exporter-pi2", "instance": "10.0.0.7:9100"}, [3.0, 128.7])
    )
    result = await _query_tool(payload).run(
        query_name="node_resource_trend", node="rp2", resource="swap_io", window="6h"
    )
    assert result.ok
    raw, _, control = await _audit_for(
        tmp_path, [("homelab_query", "read-only", result.content)]
    )
    assert value not in raw


async def test_a_backend_failure_message_is_withheld_too(tmp_path: Path):
    # The failure path renders backend-authored text, which is the surface most
    # likely to quote an address. It is a result like any other.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "no route to 10.0.0.7:9100"})

    tool = HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        gatus_timeout=5.0,
        prometheus_timeout=5.0,
        max_points=60,
        clock=lambda: NOW,
    )
    result = await tool.run(query_name="freshness_check")
    assert result.ok is False and result.error
    raw, _, control = await _audit_for(
        tmp_path, [("homelab_query", "read-only", result.error)]
    )
    assert_body_absent(raw, control, result.error)


# --- 6.3 No corpus section text, no snippet, no search query --------------


async def test_no_corpus_section_text_reaches_an_audit_record(
    tmp_path: Path, clone: Path
):
    tool = _docs_tool(clone)
    found = await tool.run(action="search", query="resolver")
    assert found.ok
    section_id = re.search(r"sec-[0-9a-f]{16}", found.content)
    assert section_id, "the search must name a section id to read"
    read = await tool.run(action="read", section_id=section_id.group(0))
    assert read.ok and read.content

    raw, records, control = await _audit_for(
        tmp_path, [("homelab_docs", "read-only", read.content)]
    )
    assert records[-1]["tool_calls"][0]["result_id"] is None
    assert_body_absent(raw, control, read.content)


async def test_no_search_snippet_or_query_string_reaches_an_audit_record(
    tmp_path: Path, clone: Path
):
    found = await _docs_tool(clone).run(action="search", query=SEARCH_QUERY)
    assert found.ok and found.content

    raw, _, control = await _audit_for(
        tmp_path, [("homelab_docs", "read-only", found.content)]
    )
    # The snippets the ranker rendered.
    assert_body_absent(raw, control, found.content)
    # And the query itself, which never was part of a result at all: model-authored
    # free text that can quote owner-personal content.
    assert SEARCH_QUERY not in raw
    assert "SYNTHETIC-SEARCH-QUERY-SENTINEL" not in raw


# --- 6.4 A placeholder address in corpus content reaches no audit field ---


async def test_a_placeholder_address_in_a_section_reaches_no_audit_field(
    tmp_path: Path, clone: Path
):
    tool = _docs_tool(clone)
    found = await tool.run(action="search", query="placeholder address")
    assert found.ok
    read = None
    for candidate in re.findall(r"sec-[0-9a-f]{16}", found.content):
        attempt = await tool.run(action="read", section_id=candidate)
        if attempt.ok and CORPUS_ADDRESS in attempt.content:
            read = attempt
            break
    # Non-vacuity: the fixture section really does carry the address, so the
    # assertion below is about the audit path rather than about an empty string.
    assert read is not None, f"no fixture section carried {CORPUS_ADDRESS}"

    raw, records, control = await _audit_for(
        tmp_path, [("homelab_docs", "read-only", read.content)]
    )
    assert CORPUS_ADDRESS not in raw
    # Field by field as well as over the raw text, because "no audit field" is
    # what the task asks and a nested structure is still a field.
    for record in records:
        assert CORPUS_ADDRESS not in json.dumps(record, ensure_ascii=False)
    # A single named value is a narrow assertion: a partial capture that happens
    # to stop before the address would satisfy it (mutation M31 did exactly that).
    # The surrounding section is asserted too, so this test binds on its own.
    assert_body_absent(raw, control, read.content)


# --- The harness is not vacuous -------------------------------------------


async def test_the_harness_would_catch_a_leak(tmp_path: Path, clone: Path):
    """The same text, through the one tool that DOES opt in, lands in the record.

    Without this, every assertion above would hold against a harness that wrote
    an empty log — and the mutation "add a read-depth tool to the opt-in set"
    would be indistinguishable from a passing suite.
    """
    read = None
    tool = _docs_tool(clone)
    found = await tool.run(action="search", query="placeholder address")
    for candidate in re.findall(r"sec-[0-9a-f]{16}", found.content):
        attempt = await tool.run(action="read", section_id=candidate)
        if attempt.ok and CORPUS_ADDRESS in attempt.content:
            read = attempt
            break
    assert read is not None

    raw, records, control = await _audit_for(
        tmp_path,
        [(HANDOFF_TOOL_NAME, "notify-only", read.content)],
    )
    assert CORPUS_ADDRESS in raw
    assert records[-1]["tool_calls"][0]["result_id"] == read.content
