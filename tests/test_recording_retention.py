"""Recording bounds, retention, reference cases and the atomic write (group 10).

From `specs/triage-replay` (*Recordings are bounded and are not audit records*: *An
oversized recording is marked, not silently cut*, *Retention holds its bounds*,
*Reference cases survive recording retention*, *Only case.json directories are
cases*, *Rehydration ignores recordings*) and design D13. Task 10.3.

Placeholders only (standing rule 1).
"""

from __future__ import annotations

import builtins
import io
import json
import logging
import os
import stat
from pathlib import Path

import jsonschema
import pytest
import yaml

import henk.replay.recorder as recorder_module
from henk.agent.ending import COMPLETED, TriageEnding
from henk.agent.session import TranscriptCall
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import read_audit_records, session_record
from henk.config import Config
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.replay.recorder import (
    CASE_FILE,
    MAX_RECORDING_AGE_SECONDS,
    MAX_RECORDING_BYTES,
    MAX_RECORDINGS,
    MAX_REFERENCE_CASES,
    RECORDING_SCHEMA_PATH,
    CaseBoundReached,
    CaseRefused,
    TriageRecorder,
    add_case,
    list_cases,
    list_recordings,
    load_recording,
    new_recording_id,
)
from henk.runtime import build_runtime
from tests.test_config import SAMPLE

SCHEMA = json.loads(RECORDING_SCHEMA_PATH.read_text())
DAY = 86400.0
NOW = 1_790_144_043.0  # 2026-09-23T06:14:03Z
TITLE = "[FIRING:1] HenkDiskFull henk (host-a.example)"
MESSAGE = "alertname = HenkDiskFull\nhost = host-a.example\nnode = vps"
MARKER = "[truncated by the recorder: {n} bytes removed]"


def _turn() -> EventTurn:
    event = Event(id="e1", title=TITLE, message=MESSAGE, arrival_time=NOW,
                  raw={"time": int(NOW) - 60})
    return EventTurn(items=(EventTurnItem(event=event, identity=derive_identity(event)),))


def _recorder(tmp_path: Path, now: float = NOW) -> TriageRecorder:
    return TriageRecorder(tmp_path / "triage-recordings",
                          replays_dir=tmp_path / "triage-replays", clock=lambda: now)


def _record(recorder: TriageRecorder, *, content: str = "composed content",
            transcript=(), reply: str | None = "reply") -> str | None:
    return recorder.record(
        turn=_turn(), content=content, reply=reply, ending=TriageEnding(COMPLETED),
        transcript=tuple(transcript),
    )


def _plant(directory: Path, at: float, *, replay: bool = False,
           replays: Path | None = None) -> str:
    """A pre-existing recording file (retention reads names, not contents)."""
    directory.mkdir(parents=True, exist_ok=True)
    rid = new_recording_id(at)
    (directory / f"{rid}.json").write_text("{}")
    if replay and replays is not None:
        (replays / rid).mkdir(parents=True, exist_ok=True)
        (replays / rid / "run-1.json").write_text("{}")
    return rid


def test_the_bounds_are_the_design_constants():
    assert MAX_RECORDING_BYTES == 256 * 1024
    assert MAX_RECORDINGS == 200
    assert MAX_RECORDING_AGE_SECONDS == 30 * DAY
    assert MAX_REFERENCE_CASES == 20
    assert CASE_FILE == "case.json"


# --- Retention holds its bounds ------------------------------------------------


def test_retention_holds_the_count_bound(tmp_path: Path):
    """*Retention holds its bounds*, by count: oldest first, replay output with it."""
    recordings, replays = tmp_path / "triage-recordings", tmp_path / "triage-replays"
    planted = [
        _plant(recordings, NOW - (MAX_RECORDINGS - i) * 60, replay=True, replays=replays)
        for i in range(MAX_RECORDINGS)
    ]
    (recordings / "README.txt").write_text("not a recording")
    (replays / "not-a-recording-id").mkdir()
    new = _record(_recorder(tmp_path))
    assert new is not None
    remaining = list_recordings(recordings)
    assert len(remaining) == MAX_RECORDINGS
    assert planted[0] not in remaining and not (replays / planted[0]).exists()
    assert remaining == planted[1:] + [new]
    assert all((replays / rid).is_dir() for rid in planted[1:])
    # Only what the recorder names is ever deleted.
    assert (recordings / "README.txt").exists()
    assert (replays / "not-a-recording-id").is_dir()


def test_retention_holds_the_age_bound(tmp_path: Path):
    """*Retention holds its bounds*, by age: none older than 30 days remains."""
    recordings, replays = tmp_path / "triage-recordings", tmp_path / "triage-replays"
    too_old = _plant(recordings, NOW - MAX_RECORDING_AGE_SECONDS - 1, replay=True,
                     replays=replays)
    much_too_old = _plant(recordings, NOW - 90 * DAY)
    at_bound = _plant(recordings, NOW - MAX_RECORDING_AGE_SECONDS, replay=True,
                      replays=replays)
    young = _plant(recordings, NOW - DAY)
    new = _record(_recorder(tmp_path))
    assert list_recordings(recordings) == [at_bound, young, new]
    assert not (replays / too_old).exists()
    assert (replays / at_bound).is_dir()
    assert much_too_old not in list_recordings(recordings)


def test_the_count_bound_applies_after_the_age_bound(tmp_path: Path):
    recordings = tmp_path / "triage-recordings"
    for i in range(MAX_RECORDINGS + 5):
        _plant(recordings, NOW - DAY - i)
    _record(_recorder(tmp_path))
    assert len(list_recordings(recordings)) == MAX_RECORDINGS


def test_a_replay_symlink_is_removed_without_following_it(tmp_path: Path):
    recordings, replays = tmp_path / "triage-recordings", tmp_path / "triage-replays"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.json").write_text("{}")
    rid = _plant(recordings, NOW - 60 * DAY)
    replays.mkdir()
    (replays / rid).symlink_to(outside, target_is_directory=True)
    _record(_recorder(tmp_path))
    assert not (replays / rid).exists() and not (replays / rid).is_symlink()
    assert (outside / "keep.json").exists()


def test_a_prune_failure_does_not_unlink_the_new_recording(tmp_path: Path, monkeypatch):
    recordings = tmp_path / "triage-recordings"
    _plant(recordings, NOW - 60 * DAY)
    real_unlink = Path.unlink

    def failing_unlink(self, *a, **kw):
        if self.parent == recordings:
            raise OSError("placeholder unlink failure")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    new = _record(_recorder(tmp_path))
    assert new is not None and new in list_recordings(recordings)


def test_a_stale_temp_file_is_cleaned_and_a_fresh_one_kept(tmp_path: Path):
    recordings = tmp_path / "triage-recordings"
    recordings.mkdir()
    stale, fresh = recordings / ".stale.tmp", recordings / ".fresh.tmp"
    stale.write_text("partial")
    fresh.write_text("partial")
    os.utime(stale, (NOW - 2 * 3600, NOW - 2 * 3600))
    os.utime(fresh, (NOW - 60, NOW - 60))
    _record(_recorder(tmp_path))
    assert not stale.exists() and fresh.exists()


# --- Reference cases survive recording retention ---------------------------------


def _case(cases: Path, case_id: str, *, old: bool = True) -> Path:
    directory = cases / case_id
    directory.mkdir(parents=True)
    (directory / CASE_FILE).write_text(json.dumps({"case_id": case_id}))
    if old:
        for path in (directory / CASE_FILE, directory):
            os.utime(path, (NOW - 60 * DAY, NOW - 60 * DAY))
    return directory


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, float]]:
    out = {}
    for path in sorted(root.rglob("*")):
        out[str(path.relative_to(root))] = (
            path.read_bytes() if path.is_file() else None, path.stat().st_mtime)
    return out


def test_reference_cases_survive_recording_retention(tmp_path: Path):
    """*Reference cases survive recording retention*: by count and by age, every
    case older than 30 days remains; past the case bound, adding is refused."""
    cases = tmp_path / "triage-cases"
    for n in range(3):
        _case(cases, f"2026-08-0{n + 1}-example-T06000{n}Z")
    raw = cases / "2026-09-23-raw"
    raw.mkdir()
    (raw / "prom-index.tsv").write_text("placeholder\n")
    # A case directory that happens to be named like a recording id.
    _case(cases, new_recording_id(NOW - 90 * DAY))
    # The directory itself untouched for 60 days, as a kept archive would be.
    os.utime(cases, (NOW - 60 * DAY, NOW - 60 * DAY))
    before = _snapshot(cases)
    recordings = tmp_path / "triage-recordings"
    for i in range(MAX_RECORDINGS + 3):
        _plant(recordings, NOW - 40 * DAY - i if i % 2 else NOW - DAY - i)
    _record(_recorder(tmp_path))
    assert len(list_recordings(recordings)) <= MAX_RECORDINGS
    assert _snapshot(cases) == before
    assert cases.stat().st_mtime == NOW - 60 * DAY


def test_adding_a_case_past_the_bound_is_refused_and_nothing_is_evicted(tmp_path: Path):
    cases = tmp_path / "triage-cases"
    for n in range(MAX_REFERENCE_CASES - 1):
        add_case(cases, f"case-{n:02d}", {"case_id": f"case-{n:02d}"})
    # Raw and capture material does not count toward the bound.
    for name in ("2026-09-23-raw", "2026-09-23-capture"):
        (cases / name).mkdir()
    add_case(cases, "case-19", {"case_id": "case-19"})
    assert len(list_cases(cases)) == MAX_REFERENCE_CASES
    before = _snapshot(cases)
    with pytest.raises(CaseBoundReached) as refused:
        add_case(cases, "case-20", {"case_id": "case-20"})
    assert str(MAX_REFERENCE_CASES) in str(refused.value)
    assert _snapshot(cases) == before
    assert not (cases / "case-20").exists()


def test_add_case_writes_an_atomic_case_json(tmp_path: Path):
    cases = tmp_path / "triage-cases"
    path = add_case(cases, "2026-09-23-swap-T062958Z", {"case_id": "x", "n": 1})
    assert path == cases / "2026-09-23-swap-T062958Z" / CASE_FILE
    assert json.loads(path.read_text()) == {"case_id": "x", "n": 1}
    assert list_cases(cases) == ["2026-09-23-swap-T062958Z"]
    assert not list(cases.rglob("*.tmp"))


def test_a_case_is_never_overwritten_or_planted_in_raw_material(tmp_path: Path):
    cases = tmp_path / "triage-cases"
    add_case(cases, "case-a", {"v": 1})
    with pytest.raises(CaseRefused):
        add_case(cases, "case-a", {"v": 2})
    assert json.loads((cases / "case-a" / CASE_FILE).read_text()) == {"v": 1}
    add_case(cases, "case-a", {"v": 2}, replace=True)
    assert json.loads((cases / "case-a" / CASE_FILE).read_text()) == {"v": 2}
    (cases / "2026-09-23-capture").mkdir()
    with pytest.raises(CaseRefused):
        add_case(cases, "2026-09-23-capture", {"v": 1})
    assert not (cases / "2026-09-23-capture" / CASE_FILE).exists()


@pytest.mark.parametrize("bad", ["", ".", "..", "../escape", "a/b", ".hidden",
                                 "x" * 200, "with space"])
def test_a_case_id_cannot_escape_the_directory(tmp_path: Path, bad: str):
    with pytest.raises(CaseRefused):
        add_case(tmp_path / "triage-cases", bad, {})
    assert not (tmp_path / "escape").exists()


def test_only_case_json_directories_are_cases(tmp_path: Path, caplog):
    """*Only case.json directories are cases*: two cases, one raw directory, one
    unreadable entry skipped with a warning."""
    cases = tmp_path / "triage-cases"
    _case(cases, "case-a")
    _case(cases, "case-b")
    (cases / "2026-09-23-raw").mkdir()
    broken = cases / "case-broken"
    broken.mkdir()
    (broken / CASE_FILE).write_text("{not json")
    (cases / "stray-file.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="henk.replay.recorder"):
        assert list_cases(cases) == ["case-a", "case-b"]
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("case-broken" in r.getMessage() for r in warned)
    assert not any("2026-09-23-raw" in r.getMessage() for r in warned)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_an_unreadable_case_is_skipped_with_a_warning(tmp_path: Path, caplog):
    cases = tmp_path / "triage-cases"
    _case(cases, "case-a")
    locked = _case(cases, "case-locked")
    (locked / CASE_FILE).chmod(0)
    try:
        with caplog.at_level(logging.WARNING, logger="henk.replay.recorder"):
            assert list_cases(cases) == ["case-a"]
        assert any("case-locked" in r.getMessage() for r in caplog.records)
    finally:
        (locked / CASE_FILE).chmod(0o600)


def test_a_missing_case_directory_lists_nothing(tmp_path: Path):
    assert list_cases(tmp_path / "absent") == []
    assert list_recordings(tmp_path / "absent") == []


# --- An oversized recording is marked, not silently cut --------------------------


def _big(n: int, char: str = "x") -> str:
    return char * n


def test_an_oversized_recording_is_marked_not_silently_cut(tmp_path: Path):
    """*An oversized recording is marked, not silently cut*."""
    small = "small result that must survive untouched"
    originals = [_big(150_000, "a"), _big(120_000, "b"), small, _big(90_000, "c")]
    transcript = [
        TranscriptCall("homelab_query", {"n": i}, text, None, f"tu-{i}")
        for i, text in enumerate(originals)
    ]
    rid = _record(_recorder(tmp_path), transcript=transcript)
    assert rid is not None
    path = tmp_path / "triage-recordings" / f"{rid}.json"
    assert path.stat().st_size <= MAX_RECORDING_BYTES
    recording = json.loads(path.read_text())
    jsonschema.validate(recording, SCHEMA)
    assert recording["complete"] is False
    assert recording["incomplete_reasons"] == ["truncated"]
    calls = recording["transcript"]
    assert calls[2]["result"] == small and "truncated_bytes" not in calls[2]
    shortened = [c for c in calls if "truncated_bytes" in c]
    assert shortened, "the largest results are the ones shortened"
    for call in shortened:
        original = originals[call["arguments"]["n"]]
        removed = call["truncated_bytes"]
        marker = MARKER.format(n=removed)
        assert call["result"].endswith(marker)
        kept = call["result"][: -len(marker)].rstrip("\n")
        assert original.startswith(kept)
        assert len(original.encode()) - len(kept.encode()) == removed
    # Largest first: the largest result is shortened at least as far as the others.
    assert calls[0]["truncated_bytes"] >= max(c["truncated_bytes"] for c in shortened[1:])


def test_multibyte_results_are_cut_on_a_character_boundary(tmp_path: Path):
    text = "é" * 200_000  # 2 bytes each, 400 KB
    rid = _record(_recorder(tmp_path), transcript=[
        TranscriptCall("homelab_query", {}, text, None, "tu-1")])
    path = tmp_path / "triage-recordings" / f"{rid}.json"
    assert path.stat().st_size <= MAX_RECORDING_BYTES
    [call] = json.loads(path.read_text())["transcript"]
    marker = MARKER.format(n=call["truncated_bytes"])
    kept = call["result"][: -len(marker)].rstrip("\n")
    assert set(kept) == {"é"}
    assert len(text.encode()) - len(kept.encode()) == call["truncated_bytes"]


def test_a_recording_within_the_bound_is_complete_and_unmarked(tmp_path: Path):
    text = _big(200_000)
    rid = _record(_recorder(tmp_path), transcript=[
        TranscriptCall("homelab_query", {}, text, None, "tu-1")])
    recording = load_recording(tmp_path / "triage-recordings", rid)
    assert recording["complete"] is True and recording["incomplete_reasons"] == []
    assert recording["transcript"][0]["result"] == text
    assert "truncated_bytes" not in recording["transcript"][0]


def test_a_recording_that_cannot_fit_is_refused_not_cut(tmp_path: Path, caplog):
    # The composed content is kept byte-exact, so it is never what gets cut.
    content = _big(MAX_RECORDING_BYTES + 10)
    with caplog.at_level(logging.ERROR, logger="henk.replay.recorder"):
        rid = _record(_recorder(tmp_path), content=content, transcript=[
            TranscriptCall("homelab_query", {}, _big(1000), None, "tu-1")])
    assert rid is None
    assert list_recordings(tmp_path / "triage-recordings") == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert not any(content[:50] in r.getMessage() for r in caplog.records)


# --- The atomic write -------------------------------------------------------------


def test_the_write_is_a_same_directory_temp_file_and_a_rename(tmp_path: Path, monkeypatch):
    renames: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst):
        renames.append((Path(src), Path(dst)))
        assert Path(src).exists() and not Path(dst).exists()
        return real_replace(src, dst)

    monkeypatch.setattr(recorder_module.os, "replace", spy)
    rid = _record(_recorder(tmp_path))
    [(src, dst)] = renames
    assert dst == tmp_path / "triage-recordings" / f"{rid}.json"
    assert src.parent == dst.parent and src.name.startswith(".")
    assert src.name.endswith(".tmp") and not src.exists()
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert stat.S_IMODE(dst.parent.stat().st_mode) == 0o700


def test_a_failed_write_leaves_no_file_and_no_temp(tmp_path: Path, monkeypatch):
    def fail(fd):
        raise OSError("placeholder fsync failure")

    monkeypatch.setattr(recorder_module.os, "fsync", fail)
    assert _record(_recorder(tmp_path)) is None
    directory = tmp_path / "triage-recordings"
    assert list_recordings(directory) == []
    assert not list(directory.glob(".*"))


def test_the_recording_on_disk_is_the_bytes_that_were_bounded(tmp_path: Path):
    rid = _record(_recorder(tmp_path), content="exacté content\n")
    raw = (tmp_path / "triage-recordings" / f"{rid}.json").read_bytes()
    assert json.loads(raw)["content"] == "exacté content\n"
    raw.decode("ascii")  # serialised ASCII-safe: a lone surrogate cannot fail the write


def test_a_lone_surrogate_does_not_fail_the_write(tmp_path: Path):
    rid = _record(_recorder(tmp_path), transcript=[
        TranscriptCall("homelab_query", {}, "bad \ud800 text" * 30_000, None, "tu-1")])
    assert rid is not None


def test_load_recording_refuses_a_path(tmp_path: Path):
    for bad in ("../audit", "x/y", "20260923T061403Z-zzzzzzzz", ""):
        with pytest.raises(ValueError):
            load_recording(tmp_path, bad)


# --- Rehydration ignores recordings ------------------------------------------------


def _sample_raw(tmp_path: Path) -> dict:
    raw = yaml.safe_load(SAMPLE.read_text())
    audit = str(tmp_path / "henk-audit.jsonl")
    raw["audit"]["path"] = audit
    raw["events"]["audit_path"] = audit
    raw["store"]["path"] = str(tmp_path / "henk-store.db")
    return raw


def _triage_line(identity: str, at: float, handoff: str) -> str:
    record = session_record(
        trigger="event",
        event=[{"identity_key": identity, "source": "gatus", "name": identity,
                "state": "firing", "recurrence": False, "event_id": "e1"}],
        handoff_message_id=handoff, announceable=True, at=at,
    )
    return json.dumps(record)


def _pipeline_state(app) -> dict:
    pipe = app._coordinator._pipeline
    return {
        "cooldown": dict(pipe._last_triaged),
        "recurrence": dict(pipe._last_handoff_ref),
        "cap_times": list(pipe._announce_times),
        "cap_suppressed": pipe._cap_suppressed_since_announce,
    }


async def test_rehydration_ignores_recordings(tmp_path: Path, monkeypatch):
    """*Rehydration ignores recordings*: only the audit log is read, and the result is
    unchanged by the recordings."""
    import time as time_module

    now = time_module.time()
    states = {}
    for label in ("without", "with"):
        base = tmp_path / label
        base.mkdir()
        (base / "henk-audit.jsonl").write_text(
            _triage_line("gatus:svc/example-a", now - 60, "hf-a") + "\n")
        if label == "with":
            recordings = base / "triage-recordings"
            recordings.mkdir()
            # Shaped like a triage record, so reading it WOULD change the state.
            (recordings / f"{new_recording_id(now - 30)}.json").write_text(
                _triage_line("gatus:svc/example-b", now - 30, "hf-b") + "\n")
            (base / "triage-cases").mkdir()
        opened: list[str] = []
        for module, name in ((builtins, "open"), (io, "open"), (os, "open"),
                             (os, "scandir"), (os, "listdir")):
            real = getattr(module, name)

            def spy(path, *args, _real=real, **kwargs):
                opened.append(os.fspath(path) if not isinstance(path, int) else "")
                return _real(path, *args, **kwargs)

            monkeypatch.setattr(module, name, spy)
        try:
            app, client = build_runtime(Config.from_dict(_sample_raw(base), env={}))
        finally:
            monkeypatch.undo()
        try:
            states[label] = _pipeline_state(app)
        finally:
            await client.aclose()
        assert not any("triage-recordings" in p for p in opened), label
        assert not any("triage-cases" in p for p in opened), label
    assert states["with"] == states["without"]
    assert "gatus:svc/example-a" in states["with"]["cooldown"]
    assert states["with"]["recurrence"] == {"gatus:svc/example-a": "hf-a"}


def test_the_audit_reader_reads_only_the_log(tmp_path: Path):
    log = tmp_path / "henk-audit.jsonl"
    log.write_text(_triage_line("gatus:svc/example-a", NOW, "hf-a") + "\n")
    before = read_audit_records(log)
    recordings = tmp_path / "triage-recordings"
    recordings.mkdir()
    (recordings / f"{new_recording_id(NOW)}.json").write_text(
        _triage_line("gatus:svc/example-b", NOW, "hf-b") + "\n")
    assert read_audit_records(log) == before
