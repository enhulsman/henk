"""Replay serving: recorded calls, honest misses, captured handoffs, reconstructed
cases (triage-quality group 11, tasks 11.1 and 11.4).

From `specs/triage-replay`:
- *Replay serves recorded results and says when it cannot*: *A recorded call is
  served its recorded result*, *An unrecorded call is answered honestly*, *A
  handoff in replay is captured, not published*;
- *Reconstructed cases are built from captured backend data and say what they
  are*: *A reconstructed case says what it is*, *Any in-domain query is answerable
  from the capture*, *scrape_targets is rendered from up, with the targets part
  unavailable*, *A missing targets payload is not read as "no error"*, *A
  Gatus-backed query is unavailable*, *A drifted template is not served*,
  *Uncaptured tools are unavailable, not guessed*, *The case cannot leak the answer
  through memory or history*, *Original arguments are not invented*;
- *Recordings are bounded and are not audit records*: *Only case.json directories
  are cases*; and the capture requirement's *A point-budget change is drift*.

Every replay here runs through `run_replay` with the real factory, hook, gate and
stub registry, over a scripted client (`tests/replay_fakes.py`). No model call.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from henk.agent.markers import PRIOR_HANDOFFS_HEADER, RECALL_BEGIN
from henk.replay import run as run_mod
from henk.replay.case import (
    UNAVAILABLE,
    CaptureIndex,
    ReconstructedQueries,
    capture_drift,
    load_case,
)
from henk.replay.harness import NOT_RECORDED, REPLAY_MARK
from henk.replay.recorder import RECORDING_SCHEMA_PATH, list_cases
from henk.tools.homelab_query import HomelabQueryTool
from henk.tools.query_registry import (
    QUERY_REGISTRY,
    QueryBackend,
    plan_query,
    with_range_end,
)
from henk.tools.query_renderers import render_scrape_targets
from tests.replay_fakes import (
    CAPTURE_DIR_NAME,
    INTERVAL,
    OTHER_MODEL,
    PROMETHEUS,
    T,
    FakePrometheus,
    SessionMaker,
    capture_records,
    edit_capture,
    live_recording,
    make_config,
    reconstructed_recording,
    write_capture,
    write_case,
    write_recording,
)

SCHEMA = json.loads(RECORDING_SCHEMA_PATH.read_text())

QUERY = {"query_name": "node_resource_trend", "node": "vps", "resource": "swap_used",
         "window": "1h"}
QUERY_RESULT = "canary: swap_used on host-a.example peaked at 98.39 (192.0.2.10)"


async def _replay(config, ident: str, script, **kw):
    source = run_mod.resolve_source(config, ident)
    maker = SessionMaker(script, **kw)
    outcome = await run_mod.run_replay(
        config, source, model=OTHER_MODEL, effort="max", thinking=None,
        create_session=maker, clock=lambda: float(T + 3600),
    )
    return outcome, maker


def _results(maker: SessionMaker) -> list[tuple[str, str, bool | None]]:
    [client] = maker.clients
    return client.results


# --- 11.1 A recorded call is served its recorded result -------------------------


async def test_a_recorded_call_is_served_its_recorded_result(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([("homelab_query", QUERY, QUERY_RESULT)]))
    # Key order differs from the recording's: arguments are canonicalized.
    reordered = dict(reversed(list(QUERY.items())))
    outcome, maker = await _replay(config, rid, [("homelab_query", reordered)])
    [(name, text, is_error)] = _results(maker)
    assert text == QUERY_RESULT
    assert is_error is None
    [call] = outcome.record["tool_calls"]
    assert call["served"] == "recorded"
    assert outcome.record["unrecorded_count"] == 0


async def test_a_recorded_error_is_served_as_an_error(tmp_path):
    config = make_config(tmp_path)
    error_text = "ERROR: Prometheus timed out after 10s"
    rid = write_recording(config, live_recording([
        ("homelab_query", QUERY, error_text, None),
        ("homelab_health", {}, "the gate said no", True),
    ]))
    outcome, maker = await _replay(
        config, rid, [("homelab_query", QUERY), ("homelab_health", {})])
    (_, first, _), (_, second, _) = _results(maker)
    # A tool failure is received byte-identical to what the model saw live, and a
    # recorded error flag is served as a failure, which the adapter marks ERROR.
    assert first == error_text
    assert second == "ERROR: the gate said no"
    served = [c["served"] for c in outcome.record["tool_calls"]]
    assert served == ["recorded", "recorded"]


async def test_repeated_identical_calls_are_served_in_recorded_order(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([
        ("homelab_query", QUERY, "first answer"),
        ("homelab_query", QUERY, "second answer"),
    ]))
    outcome, maker = await _replay(
        config, rid, [("homelab_query", QUERY)] * 3)
    texts = [text for _, text, _ in _results(maker)]
    assert texts[:2] == ["first answer", "second answer"]
    # The third has no unconsumed recorded match: honest, not a reuse.
    assert NOT_RECORDED in texts[2]
    assert outcome.record["unrecorded_count"] == 1


# --- 11.1 An unrecorded call is answered honestly --------------------------------


async def test_an_unrecorded_call_is_answered_honestly_and_counted(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([("homelab_query", QUERY, QUERY_RESULT)]))
    other = dict(QUERY, window="24h")
    outcome, maker = await _replay(
        config, rid, [("homelab_query", other), ("homelab_health", {})])
    for _, text, _ in _results(maker):
        assert text.startswith("ERROR: ")
        assert NOT_RECORDED in text
        assert QUERY_RESULT not in text
    assert f'homelab_query({json.dumps(dict(sorted(other.items())), separators=(",", ":"))})' \
        in _results(maker)[0][1]
    record = outcome.record
    assert record["unrecorded_count"] == 2
    assert [c["name"] for c in record["unrecorded_calls"]] == ["homelab_query",
                                                               "homelab_health"]
    assert [c["served"] for c in record["tool_calls"]] == ["not-recorded", "not-recorded"]


async def test_a_recorded_call_without_an_answer_is_not_served_as_empty(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([("homelab_query", QUERY, None)]))
    outcome, maker = await _replay(config, rid, [("homelab_query", QUERY)])
    [(_, text, _)] = _results(maker)
    assert NOT_RECORDED in text
    assert outcome.record["unrecorded_count"] == 1


# --- 11.1 A handoff in replay is captured, not published -------------------------


async def test_a_handoff_in_replay_is_captured_not_published(tmp_path):
    config = make_config(tmp_path)
    recorded = "handoff published (id: msg-0001)"
    rid = write_recording(config, live_recording([
        ("publish_handoff", {"document": "the original handoff"}, recorded),
    ]))
    document = "replayed handoff: example-a.service page-cache burst"
    outcome, maker = await _replay(config, rid, [
        ("publish_handoff", {"document": document}),
        ("notify", {"message": "replayed note"}),
    ])
    (_, handoff_text, _), (_, notify_text, _) = _results(maker)
    assert handoff_text.startswith(REPLAY_MARK)
    assert "not published" in handoff_text
    assert recorded not in handoff_text
    assert notify_text.startswith(REPLAY_MARK) and "not sent" in notify_text
    record = outcome.record
    assert [h["document"] for h in record["captured_handoffs"]] == [document]
    assert [n["message"] for n in record["captured_notifications"]] == ["replayed note"]
    assert record["isolation"]["refused_tool_requests"] == 0


# --- 11.4 Serving reconstructed cases from a capture -----------------------------


@pytest.fixture
def case_env(tmp_path):
    config = make_config(tmp_path)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    write_case(config, "2026-09-23-swap-T062958Z", capture_dir=capture_dir)
    return config, capture_dir


def _queries(capture_dir: Path, max_points: int = 60) -> ReconstructedQueries:
    return ReconstructedQueries(CaptureIndex.load(capture_dir), t=T, max_points=max_points)


def _prometheus_combinations():
    for name, entry in sorted(QUERY_REGISTRY.items()):
        if entry.backend is not QueryBackend.PROMETHEUS:
            continue
        names = [p.name for p in entry.parameters]
        for values in itertools.product(*(p.domain for p in entry.parameters)):
            yield {"query_name": name, **dict(zip(names, values))}


def test_the_case_layout_holds_a_valid_reconstructed_recording(case_env):
    config, _ = case_env
    case = load_case(config.audit.triage_cases_dir, "2026-09-23-swap-T062958Z")
    jsonschema.validate(case.recording, SCHEMA)
    assert case.recording["reconstructed"] is True
    assert case.t == T
    assert (case.interval_start, case.interval_end) == INTERVAL


async def test_any_in_domain_query_is_answerable_from_the_capture(case_env):
    """Each Prometheus-backed combination is served the captured answer rendered
    through the current renderer: byte-equal to what the live tool renders from
    the same Prometheus answers with its clock at T."""
    _, capture_dir = case_env
    queries = _queries(capture_dir)
    live = HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(FakePrometheus())),
        gatus_url="http://192.0.2.11:8080", prometheus_url=PROMETHEUS,
        max_points=60, clock=lambda: T,
    )
    checked = 0
    for arguments in _prometheus_combinations():
        if arguments["query_name"] == "scrape_targets":
            continue  # its targets part differs by design; tested below
        served = queries.serve(dict(arguments))
        expected = await live.run(**dict(arguments))
        assert served == expected, arguments
        assert UNAVAILABLE not in (served.content or served.error or "")
        checked += 1
    assert checked > 100
    assert queries.drift == []


def test_a_range_query_is_rendered_with_its_window_ending_at_T(case_env):
    _, capture_dir = case_env
    served = _queries(capture_dir).serve(dict(QUERY))
    plan = with_range_end(
        plan_query("node_resource_trend", {k: v for k, v in QUERY.items()
                                           if k != "query_name"}), T, 60)
    assert served.ok
    assert plan.range_window.end == T
    records = [r for r in capture_records(capture_dir).values()
               if r["query"] == "node_resource_trend" and r["role"] == "swap_used"
               and r["arguments"]["window"] == "1h" and r["arguments"]["node"] == "vps"]
    [record] = records
    assert served.content == plan.entry.renderer(plan, {"swap_used": record["body"]})


def test_scrape_targets_is_rendered_from_up_with_the_targets_part_unavailable(case_env):
    _, capture_dir = case_env
    served = _queries(capture_dir).serve({"query_name": "scrape_targets"})
    assert served.ok
    text = served.content
    assert "2 targets, 1 down" in text
    assert "last scrape error: unavailable in reconstruction" in text
    assert "no scrape error recorded by the backend" not in text
    assert "not up at any point within the last 24h" in text


def test_a_missing_targets_payload_is_not_read_as_no_error():
    plan = plan_query("scrape_targets", {})
    payloads = {
        "up": {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"job": "cadvisor-vps"}, "value": [T, "0"]}]}},
        "up_over_window": {"status": "success", "data": {"resultType": "vector",
                                                         "result": []}},
    }
    text = render_scrape_targets(plan, payloads, targets_unavailable=True)
    assert "last scrape error: unavailable in reconstruction" in text
    assert "no scrape error recorded by the backend" not in text
    # Live dispatch never sets it, so live output is unchanged.
    assert "no scrape error recorded by the backend" in render_scrape_targets(
        plan, payloads)


def test_a_gatus_backed_query_is_unavailable(case_env):
    _, capture_dir = case_env
    served = _queries(capture_dir).serve(
        {"query_name": "endpoint_history", "endpoint": "core_example-a", "window": "24h"})
    assert not served.ok
    assert served.error.startswith(UNAVAILABLE)
    assert "Gatus" in served.error


def test_a_drifted_template_is_not_served(case_env):
    _, capture_dir = case_env
    changed = edit_capture(
        capture_dir,
        lambda r: r["query"] == "node_resource_trend" and r["role"] == "swap_used"
        and r["arguments"] == {"node": "vps", "resource": "swap_used", "window": "1h"},
        lambda r: r.__setitem__("expression", r["expression"] + " "),
    )
    assert changed == 1
    queries = _queries(capture_dir)
    served = queries.serve(dict(QUERY))
    assert not served.ok
    assert served.error.startswith(UNAVAILABLE)
    assert "expression" in served.error and "differs" in served.error
    [listed] = queries.drift
    assert listed["query"] == "node_resource_trend" and listed["role"] == "swap_used"
    # The whole-capture scan lists it too, and nothing else.
    scanned = capture_drift(CaptureIndex.load(capture_dir), t=T, max_points=60)
    assert [(d["query"], d["role"], d["arguments"]) for d in scanned] == [
        ("node_resource_trend", "swap_used",
         {"node": "vps", "resource": "swap_used", "window": "1h"})]


def test_a_captured_record_of_the_wrong_kind_is_drift(case_env):
    _, capture_dir = case_env
    edit_capture(
        capture_dir,
        lambda r: r["query"] == "node_resource_trend" and r["role"] == "swap_used"
        and r["arguments"] == {"node": "vps", "resource": "swap_used", "window": "1h"},
        lambda r: r.__setitem__("kind", "instant"),
    )
    queries = _queries(capture_dir)
    served = queries.serve(dict(QUERY))
    assert not served.ok and "kind" in served.error
    assert queries.drift and "kind" in " ".join(queries.drift[0]["mismatches"])


def test_a_point_budget_change_is_drift(case_env):
    _, capture_dir = case_env
    # Captured at 60 points; the current configuration says 30.
    queries = _queries(capture_dir, max_points=30)
    served = queries.serve(dict(QUERY))
    assert not served.ok and served.error.startswith(UNAVAILABLE)
    assert "max_points" in served.error
    # An instant query is untouched by the range budget.
    assert queries.serve({"query_name": "freshness_check"}).ok


def test_a_captured_step_that_differs_is_drift(case_env):
    _, capture_dir = case_env
    edit_capture(
        capture_dir,
        lambda r: r["query"] == "node_resource_trend" and r["role"] == "swap_used"
        and r["arguments"] == {"node": "vps", "resource": "swap_used", "window": "1h"},
        lambda r: r.__setitem__("step", r["step"] + 1),
    )
    served = _queries(capture_dir).serve(dict(QUERY))
    assert not served.ok and "step" in served.error


def test_a_capture_taken_at_another_time_is_drift(case_env):
    _, capture_dir = case_env
    queries = ReconstructedQueries(CaptureIndex.load(capture_dir), t=T + 60, max_points=60)
    served = queries.serve({"query_name": "freshness_check"})
    assert not served.ok and "T" in served.error


def test_capture_lookup_keys_on_query_role_and_arguments_not_sequence_or_source(case_env):
    """The rp5 capture says `source: written-out` for the D5 roles and its files are
    numbered in another order; neither may change what is served."""
    _, capture_dir = case_env
    before = _queries(capture_dir).serve(dict(QUERY))
    records = capture_records(capture_dir)
    for path in records:
        path.chmod(0o600)
    # Shuffle names (reverse the sequence) and rewrite every source.
    paths = list(records)
    contents = [records[p] for p in reversed(paths)]
    for path, record in zip(paths, contents):
        record["source"] = "written-out"
        path.write_text(json.dumps(record))
    after = _queries(capture_dir).serve(dict(QUERY))
    assert after == before and after.ok


def test_two_answers_for_one_key_are_not_served(case_env):
    _, capture_dir = case_env
    [(path, record)] = [
        (p, r) for p, r in capture_records(capture_dir).items()
        if r["query"] == "freshness_check"]
    (capture_dir / "9999-duplicate.json").write_text(json.dumps(record))
    queries = _queries(capture_dir)
    served = queries.serve({"query_name": "freshness_check"})
    assert not served.ok and served.error.startswith(UNAVAILABLE)


def test_a_failed_capture_answer_is_unavailable_not_empty(case_env):
    _, capture_dir = case_env
    edit_capture(capture_dir, lambda r: r["query"] == "freshness_check",
                 lambda r: r.update(http_status=503, body={"status": "error"}))
    served = _queries(capture_dir).serve({"query_name": "freshness_check"})
    assert not served.ok and served.error.startswith(UNAVAILABLE)
    assert "503" in served.error


def test_a_refused_argument_is_refused_as_live(case_env):
    _, capture_dir = case_env
    served = _queries(capture_dir).serve(
        {"query_name": "node_resource_trend", "node": "nas", "resource": "cpu",
         "window": "1h"})
    assert not served.ok and "outside this query's domain" in served.error


async def test_uncaptured_tools_are_unavailable_not_guessed(tmp_path):
    config = make_config(tmp_path, everything=True)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    recording = reconstructed_recording(("homelab_health", "homelab_docs"))
    # Even a result sitting in the reconstructed sequence is never served.
    recording["transcript"][0]["result"] = "canary: recorded health"
    write_case(config, "case-a", recording=recording, capture_dir=capture_dir)
    outcome, maker = await _replay(config, "case-a", [
        ("homelab_health", {}),
        ("homelab_docs", {"action": "search", "query": "swap"}),
        ("todo_read", {}),
    ])
    for name, text, _ in _results(maker):
        assert text.startswith(f"ERROR: {UNAVAILABLE}"), (name, text)
        assert "has no captured data for this case" in text
        assert "canary" not in text
        assert NOT_RECORDED not in text
    assert outcome.record["unavailable_count"] == 3
    # A harness limit, not a model's unrecorded miss.
    assert outcome.record["unrecorded_count"] == 0


async def test_a_reconstructed_case_says_what_it_is(case_env, tmp_path):
    config, _ = case_env
    outcome, maker = await _replay(
        config, "2026-09-23-swap-T062958Z", [("homelab_query", dict(QUERY))])
    record = outcome.record
    assert record["reconstructed"] is True
    case = record["case"]
    assert case["case_id"] == "2026-09-23-swap-T062958Z"
    assert case["capture_time"] == T
    assert case["capture_time_iso"] == "2026-09-23T06:29:58Z"
    assert case["capture_interval"]["start"] == INTERVAL[0]
    assert case["capture_interval"]["end"] == INTERVAL[1]
    statement = case["statement"]
    assert "reconstructed" in statement.lower()
    assert "2026-09-23T06:29:58Z" in statement
    assert "current renderers" in statement
    assert "not the evidence the original triage saw" in statement
    [(_, text, _)] = _results(maker)
    assert not text.startswith("ERROR")


async def test_a_reconstructed_case_lists_its_drift_in_the_run(case_env):
    config, capture_dir = case_env
    edit_capture(
        capture_dir,
        lambda r: r["query"] == "freshness_check",
        lambda r: r.__setitem__("expression", "up"),
    )
    outcome, _ = await _replay(config, "2026-09-23-swap-T062958Z", [])
    listed = outcome.record["drift"]["capture"]
    assert [(d["query"], d["role"]) for d in listed] == [("freshness_check", "timestamps")]
    assert outcome.record["drift"]["any"] is True


async def test_the_case_cannot_leak_the_answer_through_memory_or_history(case_env):
    config, _ = case_env
    case = load_case(config.audit.triage_cases_dir, "2026-09-23-swap-T062958Z")
    content = case.recording["content"]
    assert RECALL_BEGIN not in content
    assert PRIOR_HANDOFFS_HEADER not in content
    outcome, maker = await _replay(config, "2026-09-23-swap-T062958Z", [])
    # The replay adds nothing: what is sent is the case's content, byte for byte.
    [client] = maker.clients
    assert client.queries == [content]
    assert outcome.record["content_sha256"] == (
        "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest())


@pytest.mark.parametrize("leak", ["recall", "digest", "prior_ids"])
def test_a_case_carrying_recall_or_history_is_refused(tmp_path, leak):
    config = make_config(tmp_path)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    recording = reconstructed_recording()
    if leak == "recall":
        recording["content"] = f"{RECALL_BEGIN}\n- a remembered fact\n" + recording["content"]
    elif leak == "digest":
        recording["content"] += f"\n{PRIOR_HANDOFFS_HEADER}\n- an earlier handoff"
    else:
        recording["prior_handoff_ids"] = [4]
    write_case(config, "case-leak", recording=recording, capture_dir=capture_dir)
    with pytest.raises(run_mod.ReplayRefused, match="recall|digest|prior handoff"):
        run_mod.resolve_source(config, "case-leak")


async def test_original_arguments_are_not_invented(case_env):
    config, _ = case_env
    outcome, _ = await _replay(config, "2026-09-23-swap-T062958Z", [])
    original = outcome.record["original_calls"]
    assert [c["name"] for c in original] == ["homelab_query", "homelab_health",
                                             "publish_handoff"]
    assert all(c["arguments"] == "unknown" for c in original)


async def test_unknown_arguments_never_match_a_call(tmp_path):
    """A reconstructed sequence entry with arguments `unknown` is not a recorded
    call: a model calling that tool is not served its (absent) recorded answer."""
    config = make_config(tmp_path)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    recording = reconstructed_recording(("homelab_query",))
    recording["transcript"][0]["result"] = "canary: invented"
    write_case(config, "case-b", recording=recording, capture_dir=capture_dir)
    _, maker = await _replay(config, "case-b", [("homelab_query", dict(QUERY))])
    [(_, text, _)] = _results(maker)
    assert "canary" not in text


async def test_an_unknown_argument_entry_never_matches_in_a_live_recording(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([("homelab_health", {}, "canary: invented health")])
    recording["transcript"][0]["arguments"] = "unknown"  # malformed for a live one
    rid = write_recording(config, recording)
    outcome, maker = await _replay(config, rid, [("homelab_health", {})])
    [(_, text, _)] = _results(maker)
    assert "canary" not in text and NOT_RECORDED in text
    assert outcome.record["original_calls"][0]["arguments"] == "unknown"


def test_only_case_json_directories_are_cases(tmp_path, caplog):
    config = make_config(tmp_path)
    cases_dir = config.audit.triage_cases_dir
    capture_dir = write_capture(cases_dir)
    write_case(config, "case-one", capture_dir=capture_dir)
    write_case(config, "case-two", capture_dir=capture_dir)
    bad = cases_dir / "case-bad"
    bad.mkdir()
    (bad / "case.json").write_text("{not json")
    assert list_cases(cases_dir) == ["case-one", "case-two"]
    for ident in (CAPTURE_DIR_NAME, "case-bad", "case-missing"):
        with pytest.raises(run_mod.ReplayRefused, match="raw and capture material"):
            run_mod.resolve_source(config, ident)
    assert run_mod.resolve_source(config, "case-one").kind == "case"


def test_a_case_capture_outside_the_cases_directory_is_refused(tmp_path):
    config = make_config(tmp_path)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    path = write_case(config, "case-escape", capture_dir=capture_dir)
    case = json.loads(path.read_text())
    case["capture"]["directory"] = "../triage-recordings"
    path.write_text(json.dumps(case))
    with pytest.raises(run_mod.ReplayRefused, match="outside"):
        run_mod.resolve_source(config, "case-escape")


def test_a_reconstructed_recording_is_replayed_only_as_a_case(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, reconstructed_recording())
    with pytest.raises(run_mod.ReplayRefused, match="case"):
        run_mod.resolve_source(config, rid)


async def test_a_live_recording_kept_as_a_case_is_served_its_recorded_calls(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([("homelab_query", QUERY, QUERY_RESULT)])
    write_case(config, "case-live", recording=recording)
    outcome, maker = await _replay(config, "case-live", [("homelab_query", dict(QUERY))])
    [(_, text, _)] = _results(maker)
    assert text == QUERY_RESULT
    assert outcome.record["reconstructed"] is False
    assert outcome.record["case"]["case_id"] == "case-live"
