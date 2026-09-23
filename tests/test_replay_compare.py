"""`compare`: the side-by-side view of an original triage and its replay runs
(triage-quality group 12a, task 12.2).

From `specs/triage-replay` *Replays are graded side by side and by a no-tool
judge against a versioned rubric*: *Side-by-side comparison*. Design D15.

`compare` is a pure file read. It is observed, not assumed: the session seam
fails the test if called, every socket raises, the store refuses construction,
and the replays directory is byte-identical before and after.

Placeholders only (standing rule 1).
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
from pathlib import Path

import pytest

from henk.replay import __main__ as cli
from tests.replay_fakes import (
    MODEL,
    OTHER_MODEL,
    T,
    SessionMaker,
    forbidden_session_maker,
    live_recording,
    make_config,
    write_recording,
)
from tests.test_replay_isolation import sealed  # noqa: F401 - the fixture

QUERY = {"query_name": "freshness_check"}
UNRECORDED = {"query_name": "disk_usage"}


def _main(config, argv, *, create_session=forbidden_session_maker, env=None):
    out, err = io.StringIO(), io.StringIO()
    environ = {"CLAUDE_CONFIG_DIR": "/tmp/henk-replay"} if env is None else env
    code = cli.main(argv, load_config=lambda: config, create_session=create_session,
                    clock=lambda: float(T + 3600), stdout=out, stderr=err,
                    environ=environ)
    return code, out.getvalue(), err.getvalue()


def _replay(config, rid, model, *, script, reply, effort="high") -> str:
    directory = config.audit.triage_replays_dir / rid
    before = set(directory.glob("*.json")) if directory.exists() else set()
    code, _, err = _main(config, ["run", rid, "--model", model, "--effort", effort],
                         create_session=SessionMaker(script, reply=reply))
    assert code == 0, err
    [path] = set(directory.glob("*.json")) - before
    return path.stem


ORIGINAL_HANDOFF = "Original handoff: swap filled on host-a.example after an upgrade."


def _setup(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([
        ("homelab_query", QUERY, "recorded freshness"),
        ("publish_handoff", {"document": ORIGINAL_HANDOFF}, "published: msg-0001"),
    ])
    rid = write_recording(config, recording)
    run_a = _replay(
        config, rid, OTHER_MODEL, effort="max",
        script=[("homelab_query", QUERY),
                ("publish_handoff", {"document": "Replay A handoff: " + "x" * 900})],
        reply="Diagnosis: fullness branch (confidence: high)\nFix: cap the journal\n"
              "Pickup: run henk-pickup",
    )
    run_b = _replay(
        config, rid, MODEL, effort="low",
        script=[("homelab_query", UNRECORDED)],
        reply="Some text without the arc.",
    )
    return config, rid, run_a, run_b


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# --- Side-by-side comparison --------------------------------------------------------


def test_compare_shows_each_column(tmp_path):
    config, rid, run_a, run_b = _setup(tmp_path)
    code, out, err = _main(config, ["compare", rid, run_a, run_b])
    assert code == 0, err
    sections = out.split("\n== ")
    # One section per candidate: the original, then each run in the order given.
    headers = [s.splitlines()[0] for s in sections[1:]]
    assert headers[0].startswith("original")
    assert run_a in headers[1] and run_b in headers[2]
    original, a, b = sections[1], sections[2], sections[3]

    # Model and effort.
    assert f"model: {MODEL}" in original and "effort: high" in original
    assert f"model: {OTHER_MODEL}" in a and "effort: max" in a
    assert f"model: {MODEL}" in b and "effort: low" in b
    # Ending, arc completeness and confidence.
    assert "ending: completed" in a and "arc: complete" in a
    assert "confidence: high" in a
    assert "arc: incomplete" in b and "confidence: none" in b
    assert "confidence: low" in original
    # The arc lines.
    assert "| Diagnosis: fullness branch (confidence: high)" in a
    assert "| Fix: cap the journal" in a
    assert "| Pickup: run henk-pickup" in a
    assert "| Diagnosis: original (confidence: low)" in original
    # The start of the handoff, bounded.
    assert "handoff: Replay A handoff: xxx" in a
    assert "x" * 900 not in a
    assert f"handoff: {ORIGINAL_HANDOFF}" in original
    assert "handoff: none captured" in b
    # Tool calls in order, with unrecorded ones marked.
    assert "1. homelab_query [recorded]" in a
    assert "2. publish_handoff [captured]" in a
    assert "1. homelab_query [NOT RECORDED]" in b
    assert "1. homelab_query" in original and "2. publish_handoff" in original
    # Tokens.
    assert "tokens: in 1, out 1, cache read -" in a
    assert "tokens: in 1, out 1" in b


def test_compare_summary_table_lists_every_candidate(tmp_path):
    config, rid, run_a, run_b = _setup(tmp_path)
    _, out, _ = _main(config, ["compare", rid, run_a, run_b])
    table = out.split("\n== ")[0]
    rows = [line for line in table.splitlines() if line.startswith(("original", "run "))]
    assert len(rows) == 3
    assert OTHER_MODEL in rows[1] and "max" in rows[1] and "completed" in rows[1]
    assert "1 unrecorded" in rows[2]


def test_compare_without_run_ids_shows_every_run_of_the_source(tmp_path):
    config, rid, run_a, run_b = _setup(tmp_path)
    code, out, err = _main(config, ["compare", rid])
    assert code == 0, err
    assert run_a in out and run_b in out


def test_compare_sends_nothing_and_writes_nothing(tmp_path, sealed):
    config, rid, run_a, run_b = _setup(tmp_path)
    before = _snapshot(tmp_path)

    def no_socket(*args, **kwargs):
        raise AssertionError("compare opened a socket")

    # `sealed` fails the test on any store, audit writer, Signal or ntfy touch.
    sealed.setattr(socket, "socket", no_socket)
    code, out, err = _main(config, ["compare", rid, run_a, run_b],
                           create_session=forbidden_session_maker, env={})
    assert code == 0, err
    assert _snapshot(tmp_path) == before


def test_compare_needs_no_agent_cli_state_directory(tmp_path):
    config, rid, run_a, _ = _setup(tmp_path)
    code, _, err = _main(config, ["compare", rid, run_a], env={})
    assert code == 0, err


def test_compare_refuses_an_unknown_run(tmp_path):
    config, rid, run_a, _ = _setup(tmp_path)
    code, _, err = _main(config, ["compare", rid, "20260923T070000Z-deadbeef"])
    assert code == cli.REFUSED
    assert "20260923T070000Z-deadbeef" in err


def test_compare_refuses_a_run_id_that_is_a_path(tmp_path):
    config, rid, _, _ = _setup(tmp_path)
    code, _, err = _main(config, ["compare", rid, "../../escape"])
    assert code == cli.REFUSED
    assert "is not a run id" in err


def test_compare_refuses_a_run_of_another_source(tmp_path):
    config, rid, run_a, _ = _setup(tmp_path)
    other = write_recording(config, live_recording([], at=T + 90.0))
    source_dir = config.audit.triage_replays_dir / rid
    target_dir = config.audit.triage_replays_dir / other
    target_dir.mkdir(mode=0o700)
    (target_dir / f"{run_a}.json").write_text((source_dir / f"{run_a}.json").read_text())
    code, _, err = _main(config, ["compare", other, run_a])
    assert code == cli.REFUSED
    assert "another" in err or rid in err


def test_compare_skips_an_unreadable_run_with_a_warning(tmp_path):
    config, rid, run_a, run_b = _setup(tmp_path)
    (config.audit.triage_replays_dir / rid / f"{run_b}.json").write_text("{not json")
    code, out, err = _main(config, ["compare", rid])
    assert code == 0, err
    assert run_a in out and run_b not in out
    assert "warning" in err and run_b in err


def test_compare_ignores_grade_files_and_files_that_are_not_runs(tmp_path):
    config, rid, run_a, _ = _setup(tmp_path)
    grades = config.audit.triage_replays_dir / rid / "grades"
    grades.mkdir(mode=0o700)
    (grades / "20260923T080000Z-00000001.json").write_text(json.dumps({"schema": "x"}))
    (config.audit.triage_replays_dir / rid / "notes.json").write_text("{}")
    code, out, err = _main(config, ["compare", rid])
    assert code == 0, err
    assert "20260923T080000Z-00000001" not in out and "warning" not in err


def test_compare_neutralises_terminal_control_characters(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    run = _replay(config, rid, OTHER_MODEL, script=[],
                  reply="Diagnosis: \x1b[31mred\x1b[0m (confidence: low)\nFix: a\nPickup: b")
    _, out, _ = _main(config, ["compare", rid, run])
    assert "\x1b" not in out
    assert "red" in out


def test_compare_of_a_recording_with_no_runs_shows_the_original(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    code, out, err = _main(config, ["compare", rid])
    assert code == 0, err
    assert "original" in out and "no replay runs" in out


def test_compare_is_refused_without_the_audit_log(tmp_path):
    config = make_config(tmp_path, audit=False)
    code, _, err = _main(config, ["compare", "20260923T062830Z-00000001"])
    assert code == cli.REFUSED and "does not exist" in err


def test_compare_shows_an_ending_that_is_not_completed(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    directory = config.audit.triage_replays_dir / rid
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high"],
                         create_session=SessionMaker([], raise_after=RuntimeError("x")))
    assert code == 0, err
    [path] = list(directory.glob("*.json"))
    _, out, _ = _main(config, ["compare", rid, path.stem])
    run_section = out.split("\n== ")[2]
    assert "ending: error" in run_section and "arc: incomplete" in run_section
