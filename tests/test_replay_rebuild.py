"""`cases` and `rebuild`: the first reconstructed case, built from preserved
material (triage-quality group 12b, tasks 12.4 and 12.5; design D15).

From `specs/triage-replay`:
- *Reconstructed cases are built from captured backend data and say what they
  are*: the rebuild's inputs (the preserved event composed with no recall and no
  digest, the capture, the audit record's tool names with arguments `unknown`,
  the original handoff plus the audit record's diagnosis and confidence), *A
  drifted template is not served* and *A point-budget change is drift* at rebuild
  time, *The case cannot leak the answer through memory or history*, *Original
  arguments are not invented*, and the statement that the case grades the current
  renderers;
- *Recordings are bounded and are not audit records*: *Only case.json
  directories are cases* (the `cases` listing) and *Reference cases survive
  recording retention* (the refusal past 20).

Every input is the committed placeholder fixture
(`tests/fixtures/replay/case-2026-09-23/`, see `tests/replay_case_fixture.py`).
No model is called and no network is reached: `rebuild` spends nothing.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
from pathlib import Path

import httpx
import jsonschema
import pytest

from henk.agent.markers import PRIOR_HANDOFFS_HEADER, RECALL_BEGIN, RECALL_END_PREFIX
from henk.agent.triage import compose_event_turn_content
from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.replay import __main__ as cli
from henk.replay import harness
from henk.replay import rebuild as rebuild_mod
from henk.replay.capture import time_label
from henk.replay.case import (
    CASE_SCHEMA,
    CaptureIndex,
    ReconstructedQueries,
    capture_drift,
    load_case,
)
from henk.store.handoffs import format_handoff_result
from henk.replay.recorder import (
    MAX_REFERENCE_CASES,
    RECORDING_SCHEMA_PATH,
    add_case,
    list_cases,
)
from tests import replay_case_fixture as fx
from tests.replay_fakes import (
    OTHER_MODEL,
    SessionMaker,
    edit_capture,
    forbidden_session_maker,
    make_config,
    reconstructed_recording,
)

SCHEMA = json.loads(RECORDING_SCHEMA_PATH.read_text())
CAPTURE_NAME = "2026-09-23-capture"
INPUTS_NAME = "2026-09-23-inputs"
PREFIX = "2026-09-23-swap"
SWAP_QUERY = {"query_name": "node_resource_trend", "node": "vps",
              "resource": "swap_used", "window": "1h"}


# --- Harness ------------------------------------------------------------------------


def _main(config, argv, *, create_session=forbidden_session_maker, clock=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, load_config=lambda: config, create_session=create_session,
                    clock=clock or (lambda: float(fx.TRIAGE_AT + 86400)),
                    stdout=out, stderr=err,
                    environ={"CLAUDE_CONFIG_DIR": "/tmp/henk-replay"})
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def env(tmp_path):
    """The rp5 layout, placeholder data: the capture and a copy of the raw inputs
    inside `triage-cases/`, neither of them a case."""
    config = make_config(tmp_path)
    cases = config.audit.triage_cases_dir
    cases.mkdir(mode=0o700, parents=True)
    shutil.copytree(fx.CAPTURE_DIR, cases / CAPTURE_NAME)
    shutil.copytree(fx.INPUTS_DIR, cases / INPUTS_NAME)
    return config


def _inputs(config) -> Path:
    return config.audit.triage_cases_dir / INPUTS_NAME


def _capture(config) -> Path:
    return config.audit.triage_cases_dir / CAPTURE_NAME


def _argv(config, **overrides) -> list[str]:
    inputs = _inputs(config)
    args = {
        "--events": str(inputs / fx.EVENTS_FILE.name),
        "--audit-records": str(inputs / fx.AUDIT_FILE.name),
        "--handoffs": str(inputs / fx.HANDOFFS_FILE.name),
        "--capture": str(_capture(config)),
        "--reference": str(inputs / fx.REFERENCE_FILE.name),
        "--case-prefix": PREFIX,
    }
    args.update(overrides)
    argv = ["rebuild"]
    for key, value in args.items():
        if value is None:
            continue
        if value is True:
            argv.append(key)
        else:
            argv += [key, value]
    return argv


def _rebuild(config, **overrides):
    return _main(config, _argv(config, **overrides))


def _case(config, case_id: str) -> dict:
    return json.loads((config.audit.triage_cases_dir / case_id / "case.json").read_text())


def _cases_written(config) -> list[str]:
    return list_cases(config.audit.triage_cases_dir)


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --- The fixture itself -----------------------------------------------------------


def test_the_fixture_has_the_real_case_shape():
    """Three T values in the capture's own layout, the rp5 capture's
    `written-out` sources, and one event, one triage record, one handoff."""
    labels = sorted(p.name for p in fx.CAPTURE_DIR.iterdir())
    assert labels == ["20260923T062958Z", "20260923T063012Z", "20260923T063026Z"]
    sources = set()
    for label in labels:
        manifest = json.loads((fx.CAPTURE_DIR / label / "manifest.json").read_text())
        files = sorted(p.name for p in (fx.CAPTURE_DIR / label).glob("*.json")
                       if p.name != "manifest.json")
        assert sorted(f["file"] for f in manifest["files"]) == files
        assert manifest["request_count"] == len(files)
        for name in files:
            record = json.loads((fx.CAPTURE_DIR / label / name).read_text())
            assert record["schema"] == "henk.triage-capture.v1"
            assert record["T"] == manifest["T"]
            sources.add(record["source"])
    assert sources == {"registry", "written-out", "named-follow-up"}
    triages = [r for r in _jsonl(fx.AUDIT_FILE) if r.get("trigger") == "event"]
    assert len(triages) == 1
    assert all(set(c) <= {"name", "tool_class", "result_id", "executed"}
               for c in triages[0]["tool_calls"])
    reference = json.loads(fx.REFERENCE_FILE.read_text())
    assert reference["branch"].startswith("fullness")
    assert "page-cache" in reference["culprit"]
    assert "package upgrade" in reference["mechanism"]
    assert "not a leak" in reference["mechanism"]
    assert "recurs" in reference["notes"]


def test_the_fixture_capture_is_current_with_the_registry():
    """The committed capture drifts from nothing at the default budget, so the
    end-to-end tests serve it. After a template change, regenerate it
    (`python -m tests.replay_case_fixture`)."""
    for t in fx.T_VALUES:
        index = CaptureIndex.load(fx.CAPTURE_DIR / time_label(t))
        assert index.records and not index.duplicates and index.skipped == 0
        assert capture_drift(index, t=t, max_points=60) == []


# --- One case per captured T --------------------------------------------------------


def test_rebuild_writes_one_case_per_captured_t(env):
    code, out, err = _rebuild(env)
    assert code == 0, err
    assert _cases_written(env) == sorted(fx.CASE_IDS)
    for case_id, t in zip(fx.CASE_IDS, fx.T_VALUES):
        assert case_id in out
        case = _case(env, case_id)
        assert case["schema"] == CASE_SCHEMA
        assert case["case_id"] == case_id
        assert case["capture"]["T"] == t
        assert (env.audit.triage_cases_dir / case["capture"]["directory"]).resolve() == (
            _capture(env) / time_label(t)
        ).resolve()
    # The ids are printed one per line, in T order, and nothing else is a case.
    printed = [line.split()[0] for line in out.splitlines() if line.startswith(PREFIX)]
    assert printed == list(fx.CASE_IDS)


def test_the_case_id_names_the_capture_time(env):
    assert rebuild_mod.case_id_for(PREFIX, 1790144998) == "2026-09-23-swap-T062958Z"
    assert rebuild_mod.case_id_for(PREFIX, 1790145026) == "2026-09-23-swap-T063026Z"


def test_the_interval_is_the_notification_plus_debounce_to_the_triage_record(env):
    _rebuild(env)
    for case_id in fx.CASE_IDS:
        interval = _case(env, case_id)["capture"]["interval"]
        assert (interval["start"], interval["end"]) == fx.INTERVAL


def test_a_capture_time_outside_the_interval_is_refused(env):
    audit = _inputs(env) / fx.AUDIT_FILE.name
    rows = _jsonl(audit)
    for row in rows:
        if row.get("trigger") == "event":
            row["at"] = float(fx.T_VALUES[1])  # the last T now falls after the triage
    _write_jsonl(audit, rows)
    code, _, err = _rebuild(env)
    assert code == 2
    assert "outside" in err and "20260923T063026Z" in err
    assert _cases_written(env) == []


# --- The composed content: no recall, no digest ---------------------------------------


def _expected_content(config) -> str:
    frame = next(r for r in _jsonl(fx.EVENTS_FILE) if r["id"] == fx.FIRING_ID)
    event = Event(id=frame["id"], title=frame["title"], message=frame["message"],
                  arrival_time=float(frame["time"]), raw=frame)
    turn = EventTurn(items=(EventTurnItem(event=event, identity=derive_identity(event)),))
    client = httpx.AsyncClient(transport=harness.RefusingTransport())
    names = harness.build_definition_registry(config, client).names()
    return compose_event_turn_content(turn, recall=None, digest=None, tool_names=names)


def test_the_content_is_the_preserved_event_composed_with_no_recall_and_no_digest(env):
    _rebuild(env)
    expected = _expected_content(env)
    for case_id in fx.CASE_IDS:
        recording = _case(env, case_id)["recording"]
        content = recording["content"]
        assert content == expected
        assert RECALL_BEGIN not in content and RECALL_END_PREFIX not in content
        assert PRIOR_HANDOFFS_HEADER not in content
        assert recording["prior_handoff_ids"] == []
        assert "HenkSwapPressure" in content and "A=98.39" in content
        [incident] = recording["incidents"]
        assert incident["event_id"] == fx.FIRING_ID
        assert incident["identity_key"] == "grafana:HenkSwapPressure"
        assert incident["notified"] == fx.NOTIFIED


def test_the_framing_names_the_live_registry_tools(tmp_path):
    """With docs enabled (as on rp5), the live core's framing names the docs
    step, so the rebuilt content must too: the composer gets the registry's names."""
    config = make_config(tmp_path, everything=True)
    (tmp_path / "docs").mkdir()
    cases = config.audit.triage_cases_dir
    cases.mkdir(mode=0o700, parents=True)
    shutil.copytree(fx.CAPTURE_DIR, cases / CAPTURE_NAME)
    shutil.copytree(fx.INPUTS_DIR, cases / INPUTS_NAME)
    code, _, err = _rebuild(config)
    assert code == 0, err
    content = _case(config, fx.CASE_IDS[0])["recording"]["content"]
    assert "`homelab_docs`" in content
    assert content == _expected_content(config)


def test_the_triaged_event_is_chosen_by_the_triage_record_not_by_position(env):
    """The resolved frame and an unrelated frame share the cache; only the frame
    the triage record names is composed."""
    _rebuild(env)
    content = _case(env, fx.CASE_IDS[0])["recording"]["content"]
    assert "state=firing" in content
    assert "example-web" not in content and "RESOLVED" not in content


# --- The original call sequence: names, arguments unknown -----------------------------


def test_the_original_calls_are_the_audit_names_with_arguments_unknown(env):
    _rebuild(env)
    triage = next(r for r in _jsonl(fx.AUDIT_FILE) if r.get("trigger") == "event")
    names = [c["name"] for c in triage["tool_calls"]]
    for case_id in fx.CASE_IDS:
        transcript = _case(env, case_id)["recording"]["transcript"]
        assert [c["name"] for c in transcript] == names
        assert all(c["arguments"] == "unknown" for c in transcript)
        assert all(c["result"] is None and c["is_error"] is None for c in transcript)


# --- The original candidate ---------------------------------------------------------


def test_the_original_candidate_is_the_handoff_plus_the_audit_diagnosis(env):
    _rebuild(env)
    triage = next(r for r in _jsonl(fx.AUDIT_FILE) if r.get("trigger") == "event")
    # A real record carries the tool's whole result string, not the bare id
    # (henk/store/handoffs.py:43-45); the 2026-09-23 rebuild on rp5 refused on it.
    assert triage["handoff_message_id"] == format_handoff_result(fx.HANDOFF_ID)
    frame = next(r for r in _jsonl(fx.HANDOFFS_FILE) if r["id"] == fx.HANDOFF_ID)
    for case_id in fx.CASE_IDS:
        original = _case(env, case_id)["original_candidate"]
        assert original["handoff_message_id"] == fx.HANDOFF_ID
        assert original["handoff_document"] == frame["message"].removeprefix("[AI] ")
        assert original["diagnosis"] == triage["diagnosis"]
        assert original["confidence"] == "moderate"
        assert original["triage_arc_complete"] is True
        assert original["model"] == triage["model"]


def test_a_handoff_sent_as_an_attachment_needs_the_document_given(env):
    handoffs = _inputs(env) / fx.HANDOFFS_FILE.name
    rows = _jsonl(handoffs)
    for row in rows:
        if row["id"] == fx.HANDOFF_ID:
            row["message"] = "You received a file: attachment.txt"
            row["attachment"] = {"name": "attachment.txt", "size": 5000}
    _write_jsonl(handoffs, rows)
    code, _, err = _rebuild(env)
    assert code == 2 and "--handoff-document" in err
    assert _cases_written(env) == []
    document = _inputs(env) / "handoff.txt"
    document.write_text("[AI] Diagnosis: from the attachment (confidence: low)\n")
    code, _, err = _rebuild(env, **{"--handoff-document": str(document)})
    assert code == 0, err
    original = _case(env, fx.CASE_IDS[0])["original_candidate"]
    assert original["handoff_document"] == "Diagnosis: from the attachment (confidence: low)\n"


def test_a_missing_handoff_is_refused_not_left_empty(env):
    handoffs = _inputs(env) / fx.HANDOFFS_FILE.name
    _write_jsonl(handoffs, [r for r in _jsonl(handoffs) if r["id"] != fx.HANDOFF_ID])
    code, _, err = _rebuild(env)
    assert code == 2 and fx.HANDOFF_ID in err
    assert _cases_written(env) == []


# --- The reference comes from the owner's file --------------------------------------


def test_the_reference_comes_from_the_owner_file(env):
    _rebuild(env)
    reference = json.loads(fx.REFERENCE_FILE.read_text())
    for case_id in fx.CASE_IDS:
        assert _case(env, case_id)["recording"]["reference"] == reference


@pytest.mark.parametrize("change", ["missing-fix", "empty-culprit", "extra-key", "not-json"])
def test_a_malformed_reference_is_refused_and_nothing_is_written(env, change):
    path = _inputs(env) / fx.REFERENCE_FILE.name
    reference = json.loads(path.read_text())
    if change == "missing-fix":
        del reference["fix"]
    elif change == "empty-culprit":
        reference["culprit"] = ""
    elif change == "extra-key":
        reference["answer"] = "x"
    path.write_text("{broken" if change == "not-json" else json.dumps(reference))
    code, _, err = _rebuild(env)
    assert code == 2 and "reference" in err
    assert _cases_written(env) == []


# --- Drift, before each case is written ---------------------------------------------


def test_no_drift_is_recorded_for_a_current_capture(env):
    _rebuild(env)
    for case_id in fx.CASE_IDS:
        assert _case(env, case_id)["drift"] == []


def test_a_drifted_expression_is_recorded_in_the_case(env):
    first = _capture(env) / time_label(fx.T_VALUES[0])
    assert edit_capture(first, lambda r: r["role"] == "swap_used" and
                        r["arguments"]["window"] == "1h",
                        lambda r: r.__setitem__("expression", "node_memory_SwapFree_bytes")) == 1
    code, out, _ = _rebuild(env)
    assert code == 0
    [entry] = _case(env, fx.CASE_IDS[0])["drift"]
    assert (entry["query"], entry["role"]) == ("node_resource_trend", "swap_used")
    assert "expression" in entry["mismatches"][0]
    assert _case(env, fx.CASE_IDS[1])["drift"] == []
    assert "DRIFT" in out and fx.CASE_IDS[0] in out


def test_a_point_budget_change_is_drift_recorded_in_the_case(tmp_path):
    config = make_config(tmp_path, homelab_query__query_range_max_points=30)
    cases = config.audit.triage_cases_dir
    cases.mkdir(mode=0o700, parents=True)
    shutil.copytree(fx.CAPTURE_DIR, cases / CAPTURE_NAME)
    shutil.copytree(fx.INPUTS_DIR, cases / INPUTS_NAME)
    code, _, err = _rebuild(config)
    assert code == 0, err
    drift = _case(config, fx.CASE_IDS[0])["drift"]
    ranged = [d for d in drift if d["role"] in ("swap_used", "swap_io", "memory",
                                                "movers_series", "bad_states")]
    assert ranged and len(ranged) == len(drift)
    for entry in ranged:
        text = " ".join(entry["mismatches"])
        assert "max_points" in text and "step" in text


def test_a_captured_step_that_differs_is_drift_recorded_in_the_case(env):
    first = _capture(env) / time_label(fx.T_VALUES[0])
    edit_capture(first, lambda r: r["role"] == "swap_io" and r["arguments"]["window"] == "15m",
                 lambda r: r.__setitem__("step", r["step"] + 1))
    _rebuild(env)
    [entry] = _case(env, fx.CASE_IDS[0])["drift"]
    assert entry["role"] == "swap_io"
    assert any("step" in m for m in entry["mismatches"])


def test_a_captured_kind_that_differs_is_drift_recorded_in_the_case(env):
    first = _capture(env) / time_label(fx.T_VALUES[0])
    edit_capture(first, lambda r: r["role"] == "unit_count",
                 lambda r: r.__setitem__("kind", "range"))
    _rebuild(env)
    [entry] = _case(env, fx.CASE_IDS[0])["drift"]
    assert entry["role"] == "unit_count"
    assert any("kind" in m for m in entry["mismatches"])


# --- The case layout and what it says -----------------------------------------------


def test_every_rebuilt_case_is_valid_for_load_case(env):
    _rebuild(env)
    for case_id, t in zip(fx.CASE_IDS, fx.T_VALUES):
        case = load_case(env.audit.triage_cases_dir, case_id)
        assert case.reconstructed
        assert case.t == t
        assert (case.interval_start, case.interval_end) == fx.INTERVAL
        assert case.capture_dir.resolve() == (_capture(env) / time_label(t)).resolve()
        jsonschema.validate(case.recording, SCHEMA)
        raw = _case(env, case_id)
        assert not Path(raw["capture"]["directory"]).is_absolute()


def test_the_case_states_that_it_grades_the_current_renderers(env):
    _rebuild(env)
    for case_id in fx.CASE_IDS:
        raw = _case(env, case_id)
        assert raw["grades"] == "current-renderers"
        statement = raw["statement"]
        assert "current renderers" in statement
        assert "not the evidence the original triage saw" in statement
        assert raw["recording"]["reconstructed"] is True


def test_a_capture_outside_the_cases_directory_is_refused(env, tmp_path):
    outside = tmp_path / "elsewhere"
    shutil.copytree(fx.CAPTURE_DIR, outside)
    code, _, err = _rebuild(env, **{"--capture": str(outside)})
    assert code == 2 and "inside" in err
    assert _cases_written(env) == []


def test_a_capture_with_no_t_directories_is_refused(env):
    empty = env.audit.triage_cases_dir / "empty-capture"
    empty.mkdir()
    code, _, err = _rebuild(env, **{"--capture": str(empty)})
    assert code == 2 and "no captured T" in err


def test_an_existing_case_is_refused_unless_replaced(env):
    assert _rebuild(env)[0] == 0
    code, _, err = _rebuild(env)
    assert code == 2 and "already exists" in err
    code, _, err = _rebuild(env, **{"--replace": True})
    assert code == 0, err


def test_one_existing_case_refuses_the_whole_batch(env):
    """Checked before the first write: the batch is not written half-way."""
    add_case(env.audit.triage_cases_dir, fx.CASE_IDS[1],
             {"schema": CASE_SCHEMA, "case_id": fx.CASE_IDS[1],
              "recording": reconstructed_recording()})
    code, _, err = _rebuild(env)
    assert code == 2 and fx.CASE_IDS[1] in err
    assert _cases_written(env) == [fx.CASE_IDS[1]]


def test_a_triage_record_that_makes_no_valid_recording_is_refused(env):
    audit = _inputs(env) / fx.AUDIT_FILE.name
    rows = _jsonl(audit)
    for row in rows:
        if row.get("trigger") == "event":
            row["approvals"] = [{"tool": "remember", "tier": "standing", "outcome": 5}]
    _write_jsonl(audit, rows)
    code, _, err = _rebuild(env)
    assert code == 2 and "not a valid recording" in err
    assert _cases_written(env) == []


def test_an_unknown_triage_outcome_is_refused(env):
    audit = _inputs(env) / fx.AUDIT_FILE.name
    rows = _jsonl(audit)
    for row in rows:
        if row.get("trigger") == "event":
            row["outcome"] = "exploded"
    _write_jsonl(audit, rows)
    code, _, err = _rebuild(env)
    assert code == 2 and "outcome" in err


# --- The bound of 20 -----------------------------------------------------------------


def _fill(config, count: int) -> None:
    for n in range(count):
        add_case(config.audit.triage_cases_dir, f"case-{n:02d}",
                 {"schema": CASE_SCHEMA, "case_id": f"case-{n:02d}",
                  "recording": reconstructed_recording()})


def test_a_rebuild_past_the_case_bound_is_refused_and_writes_nothing(env):
    _fill(env, MAX_REFERENCE_CASES - 2)
    code, _, err = _rebuild(env)
    assert code == 2
    assert str(MAX_REFERENCE_CASES) in err
    assert len(_cases_written(env)) == MAX_REFERENCE_CASES - 2
    assert not any(c.startswith(PREFIX) for c in _cases_written(env))


def test_a_rebuild_that_reaches_the_bound_exactly_is_written(env):
    _fill(env, MAX_REFERENCE_CASES - 3)
    code, _, err = _rebuild(env)
    assert code == 0, err
    assert len(_cases_written(env)) == MAX_REFERENCE_CASES


def test_a_bare_handoff_id_in_the_record_is_accepted_too(env):
    audit = _inputs(env) / fx.AUDIT_FILE.name
    rows = _jsonl(audit)
    for row in rows:
        if row.get("trigger") == "event":
            row["handoff_message_id"] = fx.HANDOFF_ID
    _write_jsonl(audit, rows)
    code, _, err = _rebuild(env)
    assert code == 0, err
    original = _case(env, fx.CASE_IDS[0])["original_candidate"]
    assert original["handoff_message_id"] == fx.HANDOFF_ID
    assert original["handoff_document"]


def test_a_handoff_missing_from_the_cache_is_refused_naming_the_bare_id(env):
    handoffs = _inputs(env) / fx.HANDOFFS_FILE.name
    _write_jsonl(handoffs, [r for r in _jsonl(handoffs) if r["id"] != fx.HANDOFF_ID])
    code, _, err = _rebuild(env)
    assert code == 2
    assert f"holds no message {fx.HANDOFF_ID}" in err
    assert "handoff published" not in err
    assert _cases_written(env) == []


# --- Selecting the triage -----------------------------------------------------------


def test_two_event_triages_need_an_event_id(env):
    audit = _inputs(env) / fx.AUDIT_FILE.name
    rows = _jsonl(audit)
    twin = dict(next(r for r in rows if r.get("trigger") == "event"))
    twin["at"] = 1790131260.0
    twin["event"] = [{"identity_key": "gatus:core/example-web", "source": "gatus",
                      "name": "core/example-web", "state": "firing",
                      "event_id": "fxGatusOld01"}]
    twin["handoff_message_id"] = "fxHandoffOld"
    _write_jsonl(audit, [twin] + rows)
    code, _, err = _rebuild(env)
    assert code == 2 and "--event-id" in err and fx.FIRING_ID in err
    code, _, err = _rebuild(env, **{"--event-id": fx.FIRING_ID})
    assert code == 0, err
    assert "HenkSwapPressure" in _case(env, fx.CASE_IDS[0])["recording"]["content"]


def test_an_event_missing_from_the_cache_is_refused(env):
    events = _inputs(env) / fx.EVENTS_FILE.name
    _write_jsonl(events, [r for r in _jsonl(events) if r["id"] != fx.FIRING_ID])
    code, _, err = _rebuild(env)
    assert code == 2 and fx.FIRING_ID in err
    assert _cases_written(env) == []


# --- Spending nothing ---------------------------------------------------------------


def test_rebuild_spends_nothing(env, monkeypatch):
    """No session is created (the seam fails the test), and no request or socket
    leaves the process."""
    def refuse(*args, **kwargs):
        raise AssertionError("rebuild reached the network")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    code, _, err = _rebuild(env)
    assert code == 0, err
    assert len(_cases_written(env)) == 3


def test_rebuild_refuses_a_wrong_compose_project(tmp_path):
    config = make_config(tmp_path, audit=False)
    code, _, err = _main(config, _argv(config))
    assert code == 2 and "henk.old" in err


# --- cases ---------------------------------------------------------------------------


def test_cases_lists_only_case_json_directories(env, caplog):
    _rebuild(env)
    caplog.clear()
    bad = env.audit.triage_cases_dir / "case-bad"
    bad.mkdir()
    (bad / "case.json").write_text("{not json")
    (env.audit.triage_cases_dir / "loose-file.json").write_text("{}")
    code, out, err = _main(env, ["cases"])
    assert code == 0
    listed = [line.split("\t")[0] for line in out.splitlines()]
    assert listed == sorted(fx.CASE_IDS)
    assert CAPTURE_NAME not in out and INPUTS_NAME not in out
    assert "case-bad" in err and "case-bad" not in out
    first = next(line for line in out.splitlines() if line.startswith(fx.CASE_IDS[0]))
    assert "reconstructed" in first and "2026-09-23T06:29:58Z" in first
    # Raw and capture material is skipped quietly; only the unreadable entry warns.
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert CAPTURE_NAME not in warned and INPUTS_NAME not in warned


def test_cases_lists_an_invalid_case_as_invalid_because_it_still_counts(env):
    add_case(env.audit.triage_cases_dir, "case-odd", {"schema": "something-else"})
    code, out, _ = _main(env, ["cases"])
    assert code == 0
    [line] = [line for line in out.splitlines() if line.startswith("case-odd")]
    assert "invalid" in line


def test_cases_with_none_prints_nothing(env):
    code, out, err = _main(env, ["cases"])
    assert (code, out) == (0, "")


# --- End to end: rebuild, cases, then replay one case -------------------------------


def test_rebuild_then_cases_then_a_replay_of_one_case(env):
    code, _, err = _rebuild(env)
    assert code == 0, err
    code, out, _ = _main(env, ["cases"])
    assert [line.split("\t")[0] for line in out.splitlines()] == sorted(fx.CASE_IDS)

    maker = SessionMaker([("homelab_query", dict(SWAP_QUERY)),
                          ("homelab_query", {"query_name": "scrape_targets"}),
                          ("homelab_health", {}),
                          ("publish_handoff", {"document": "Diagnosis: page cache"})])
    case_id = fx.CASE_IDS[0]
    code, out, err = _main(env, ["run", case_id, "--model", OTHER_MODEL,
                                 "--effort", "high"], create_session=maker)
    assert code == 0, err
    assert "current renderers" in out
    [client] = maker.clients
    assert client.queries == [_case(env, case_id)["recording"]["content"]]
    (swap, _, _), (targets, _, _), (health, _, _), (handoff, _, _) = [
        (text, name, e) for name, text, e in client.results]
    assert not swap.startswith("ERROR") and "98.39" in swap
    # Served from the capture, rendered through the current renderer.
    queries = ReconstructedQueries(CaptureIndex.load(_capture(env) / time_label(fx.T_VALUES[0])),
                                   t=fx.T_VALUES[0], max_points=60)
    assert swap == queries.serve(dict(SWAP_QUERY)).content
    assert "last scrape error" not in targets or "unavailable in reconstruction" in targets
    assert health.startswith("ERROR") and "unavailable in reconstruction" in health
    assert "not published" in handoff
    [run_file] = sorted((env.audit.triage_replays_dir / case_id).glob("*.json"))
    run = json.loads(run_file.read_text())
    assert run["reconstructed"] is True
    assert run["case"]["capture_time"] == fx.T_VALUES[0]
    assert run["drift"]["capture"] == [] and run["drift"]["capture_served"] == []
    assert [c["name"] for c in run["original_calls"]][-1] == "publish_handoff"
    assert all(c["arguments"] == "unknown" for c in run["original_calls"])
    assert run["isolation"] == {"refused_tool_requests": 0, "channel_attempts": 0}
