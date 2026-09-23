"""The replay entry point: `list`, `run`, pre-spend validation, drift, the
compose-project guard and the run writer (triage-quality group 11, tasks 11.3 and
11.5).

From `specs/triage-replay` *The owner can replay a recorded triage against a
chosen profile*: *A replay runs a recording on another model*, *Drift is
reported, not hidden*, *An invalid effort spends nothing*, *A malformed model
identifier spends nothing*; and `specs/secure-deployment` *Triage recordings and
replay add no surface and stay on the audit volume*: *A wrong compose project is
refused*, *Recording paths are derived from the audit path*. Design D14.

"Spends nothing" is observed: the `create_session` seam fails the test if it is
ever called, so a refusal that happened after a session existed is caught.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from henk.config import EFFORT_LEVELS
from henk.replay import __main__ as cli
from henk.replay import run as run_mod
from henk.replay import recorder as recorder_mod
from henk.replay.recorder import system_prompt_hash, tool_definitions_hash
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

QUERY = {"query_name": "freshness_check"}


def _main(config, argv, *, create_session=forbidden_session_maker, env=None):
    out, err = io.StringIO(), io.StringIO()
    environ = {"CLAUDE_CONFIG_DIR": "/tmp/henk-replay"} if env is None else env
    code = cli.main(argv, load_config=lambda: config, create_session=create_session,
                    clock=lambda: float(T + 3600), stdout=out, stderr=err,
                    environ=environ)
    return code, out.getvalue(), err.getvalue()


def _current_hashes(config) -> dict:
    import httpx

    from henk.replay import harness

    client = httpx.AsyncClient(transport=harness.RefusingTransport())
    registry = harness.build_definition_registry(config, client)
    return {"system_prompt": system_prompt_hash(config.agent.system_prompt),
            "tool_definitions": tool_definitions_hash(registry.tools())}


def _runs(config, source_id: str) -> list[dict]:
    directory = config.audit.triage_replays_dir / source_id
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


# --- A replay runs a recording on another model -----------------------------------


def test_a_replay_runs_a_recording_on_another_model(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([("homelab_query", QUERY, "recorded freshness")],
                               hashes=_current_hashes(config))
    rid = write_recording(config, recording)
    maker = SessionMaker([("homelab_query", QUERY)])
    code, out, err = _main(config, ["run", rid, "--model", OTHER_MODEL,
                                    "--effort", "max", "--thinking", "adaptive"],
                           create_session=maker)
    assert code == 0, err
    [factory] = maker.factories
    assert factory.config.model == OTHER_MODEL
    assert factory.config.effort == "max"
    assert factory.config.thinking == "adaptive"
    assert factory.profile == "event"
    assert factory.config.system_prompt == config.agent.system_prompt
    [client] = maker.clients
    assert client.queries == [recording["content"]]
    [run] = _runs(config, rid)
    assert run["schema"] == "henk.triage-replay-run.v1"
    assert run["source"] == {"kind": "recording", "id": rid, "recording_id": rid}
    assert run["profile"] == {"name": "event", "model": OTHER_MODEL, "effort": "max",
                              "thinking": "adaptive"}
    assert run["recorded_profile"]["model"] == MODEL
    assert run["reply"].startswith("Diagnosis:")
    assert run["ending"]["outcome"] == "completed"
    assert run["arc"]["complete"] is True
    assert [c["name"] for c in run["tool_calls"]] == ["homelab_query"]
    assert run["tool_calls"][0]["result"] == "recorded freshness"
    assert run["usage"] == {"input_tokens": 1, "output_tokens": 1,
                            "cache_read_input_tokens": None}
    assert run["drift"]["any"] is False
    assert run["drift"]["system_prompt"]["status"] == "same"
    assert run["drift"]["tool_definitions"]["status"] == "same"
    assert run["run_id"] in out and OTHER_MODEL in out


def test_thinking_is_unset_when_not_given(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    maker = SessionMaker()
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "low"],
                         create_session=maker)
    assert code == 0, err
    assert maker.factories[0].config.thinking is None
    assert _runs(config, rid)[0]["profile"]["thinking"] is None


def test_a_replay_that_raises_still_writes_its_run(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    maker = SessionMaker([], raise_after=RuntimeError("stream broke"))
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high"],
                         create_session=maker)
    assert code == 0, err
    [run] = _runs(config, rid)
    assert run["raised"] is True
    assert run["ending"]["outcome"] == "error"
    assert run["arc"] is None


# --- Drift is reported, not hidden ----------------------------------------------


@pytest.mark.parametrize("field", ["system_prompt", "tool_definitions"])
def test_drift_is_reported_not_hidden(tmp_path, field):
    config = make_config(tmp_path)
    hashes = _current_hashes(config)
    hashes[field] = "sha256:" + "0" * 64
    rid = write_recording(config, live_recording([], hashes=hashes))
    code, out, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort",
                                    "high"], create_session=SessionMaker())
    assert code == 0, err
    [run] = _runs(config, rid)
    assert run["drift"][field]["status"] == "differs"
    other = "tool_definitions" if field == "system_prompt" else "system_prompt"
    assert run["drift"][other]["status"] == "same"
    assert run["drift"]["any"] is True
    words = "tool definitions" if field == "tool_definitions" else "system prompt"
    assert f"the {words} differ from the recording's" in out


def test_an_unrecorded_hash_is_unknown_not_same(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))  # null hashes
    code, out, _ = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high"],
                         create_session=SessionMaker())
    [run] = _runs(config, rid)
    assert run["drift"]["system_prompt"]["status"] == "unknown"
    assert run["drift"]["tool_definitions"]["status"] == "unknown"
    assert "cannot be compared" in out


# --- An invalid effort / a malformed model spends nothing -------------------------


@pytest.mark.parametrize("effort", ["extreme", "HIGH", "", "none"])
def test_an_invalid_effort_spends_nothing(tmp_path, effort):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", effort])
    assert code == 2
    for level in EFFORT_LEVELS:
        assert level in err
    assert not config.audit.triage_replays_dir.exists()


@pytest.mark.parametrize("model", [
    "claude opus", "claude-opus/5", "anthropic/claude-opus-5-5", "gpt-5", "claude-",
    "Claude-Opus-5", "claude-opus-5-5[2m]", "claude-opus-5-5\n", "claude-opus-5-5 ",
])
def test_a_malformed_model_identifier_spends_nothing(tmp_path, model):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    code, _, err = _main(config, ["run", rid, "--model", model, "--effort", "high"])
    assert code == 2
    assert "claude-" in err and "[1m]" in err
    assert not config.audit.triage_replays_dir.exists()


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-5-5[1m]",
                                   "claude-fable-5-1"])
def test_well_formed_model_identifiers_are_accepted(model):
    run_mod.validate_model(model)


def test_an_invalid_thinking_mode_spends_nothing(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high",
                                  "--thinking", "enabled"])
    assert code == 2 and "adaptive" in err


@pytest.mark.parametrize("extra, environ", [
    (["--model", "gpt-5", "--effort", "high"], {"CLAUDE_CONFIG_DIR": "/tmp/x"}),
    (["--model", OTHER_MODEL, "--effort", "huge"], {"CLAUDE_CONFIG_DIR": "/tmp/x"}),
    (["--model", OTHER_MODEL, "--effort", "high", "--thinking", "on"],
     {"CLAUDE_CONFIG_DIR": "/tmp/x"}),
    (["--model", OTHER_MODEL, "--effort", "high"], {}),
])
def test_validation_runs_before_the_config_is_even_loaded(extra, environ):
    def load_config():  # pragma: no cover - failing is the point
        raise AssertionError("config loaded before the arguments were validated")

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["run", "20260923T061500Z-0000000a", *extra], load_config=load_config,
                    create_session=forbidden_session_maker, stdout=out, stderr=err,
                    environ=environ)
    assert code == 2


async def test_run_replay_validates_again_before_any_session(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    source = run_mod.resolve_source(config, rid)
    with pytest.raises(run_mod.ReplayRefused):
        await run_mod.run_replay(config, source, model="gpt-5", effort="high",
                                 thinking=None, create_session=forbidden_session_maker)
    with pytest.raises(run_mod.ReplayRefused):
        await run_mod.run_replay(config, source, model=OTHER_MODEL, effort="huge",
                                 thinking=None, create_session=forbidden_session_maker)


def test_run_requires_a_separate_agent_cli_state_directory(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    code, _, err = _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high"],
                         env={})
    assert code == 2 and "CLAUDE_CONFIG_DIR" in err


# --- A wrong compose project is refused ---------------------------------------------


@pytest.mark.parametrize("argv", [
    ["list"],
    ["run", "20260923T061500Z-0000000a", "--model", OTHER_MODEL, "--effort", "high"],
])
def test_a_wrong_compose_project_is_refused(tmp_path, argv):
    config = make_config(tmp_path, audit=False)
    code, out, err = _main(config, argv)
    assert code == 2
    assert config.audit.path in err
    assert "compose project" in err and "empty volumes" in err
    assert not config.audit.triage_replays_dir.exists()


def test_an_audit_path_that_is_a_directory_is_refused(tmp_path):
    config = make_config(tmp_path, audit=False)
    Path(config.audit.path).mkdir()
    code, _, err = _main(config, ["list"])
    assert code == 2 and "compose project" in err


def test_an_unknown_id_is_refused_without_a_session(tmp_path):
    config = make_config(tmp_path)
    code, _, err = _main(config, ["run", "20260923T061500Z-0000000a", "--model",
                                  OTHER_MODEL, "--effort", "high"])
    assert code == 2 and "no recording" in err


# --- list ----------------------------------------------------------------------


def test_list_shows_each_recording(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([], at=float(T))
    first = write_recording(config, recording)
    second = write_recording(config, live_recording([], at=float(T + 60)))
    (config.audit.triage_recordings_dir / "not-a-recording.json").write_text("{}")
    code, out, err = _main(config, ["list"])
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.strip()]
    assert [line.split()[0] for line in lines] == [first, second]
    assert "2026-09-23T06:29:58Z" in lines[0]
    assert recording["incidents"][0]["identity_key"] in lines[0]
    assert "HenkSwapPressure" in recording["incidents"][0]["identity_key"]
    assert lines[0].split("\t")[-2:] == ["completed", "complete"]


def test_list_skips_an_unreadable_recording_with_a_warning(tmp_path):
    config = make_config(tmp_path)
    good = write_recording(config, live_recording([]))
    bad = recorder_mod.new_recording_id(float(T - 60))
    (config.audit.triage_recordings_dir / f"{bad}.json").write_text("{broken")
    code, out, err = _main(config, ["list"])
    assert code == 0
    assert good in out and bad not in out
    assert bad in err


def test_only_list_and_run_are_commands(tmp_path):
    config = make_config(tmp_path)
    for command in ("compare", "grade", "cases", "rebuild"):
        with pytest.raises(SystemExit):
            _main(config, [command])


# --- The run writer ---------------------------------------------------------------


def test_run_output_lives_under_the_derived_replays_directory(tmp_path):
    config = make_config(tmp_path)
    assert config.audit.triage_replays_dir == Path(config.audit.path).parent / "triage-replays"
    rid = write_recording(config, live_recording([]))
    _main(config, ["run", rid, "--model", OTHER_MODEL, "--effort", "high"],
          create_session=SessionMaker())
    directory = config.audit.triage_replays_dir / rid
    [path] = list(directory.glob("*.json"))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stem == _runs(config, rid)[0]["run_id"]


def test_the_run_write_is_atomic(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    source = run_mod.resolve_source(config, rid)
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src), Path(dst)))
        assert Path(src).parent == Path(dst).parent
        assert not Path(dst).exists()
        return real_replace(src, dst)

    monkeypatch.setattr(recorder_mod.os, "replace", spy)
    path = run_mod.write_run(config, source, {"run_id": "20260923T073000Z-00000001",
                                              "reply": "x"})
    assert replaced and replaced[0][1] == path
    assert json.loads(path.read_text())["reply"] == "x"
    leftovers = [p for p in path.parent.iterdir() if p != path]
    assert leftovers == []


def test_a_failed_run_write_leaves_no_partial_file(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    source = run_mod.resolve_source(config, rid)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(recorder_mod.os, "replace", boom)
    with pytest.raises(OSError):
        run_mod.write_run(config, source, {"run_id": "20260923T073000Z-00000002"})
    directory = config.audit.triage_replays_dir / rid
    assert list(directory.iterdir()) == []


def test_a_run_id_cannot_name_a_path(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    source = run_mod.resolve_source(config, rid)
    with pytest.raises(ValueError):
        run_mod.write_run(config, source, {"run_id": "../../escape"})
