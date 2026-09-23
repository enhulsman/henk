"""``python -m henk.replay``: list recordings, or replay one (triage-quality D14).

Run it only as a one-shot container of the henk service, from the henk checkout
directory on rp5 (README, "Replaying a triage"):

    docker compose run --rm --no-deps -e CLAUDE_CONFIG_DIR=/tmp/henk-replay \\
        henk python -m henk.replay <command>

Commands:

- ``list`` shows each recording's id, time, incident identities, ending and
  whether it is complete. It reads recordings only.
- ``run <id> --model M --effort E [--thinking T]`` replays one recording, or one
  reference case, on the chosen profile and writes a run file under the replays
  directory. ``M`` must be a Claude model identifier and ``E`` an SDK effort
  level; both are checked before the configuration is read, so a typo spends
  nothing. Thinking left out is left unset (the CLI's default).

- ``compare <id> [run ...]`` prints the original triage beside the named runs
  (every run of the source when none is named). A pure file read: no session,
  no request, nothing written.
- ``grade <id> [run ...] [--judge-model M] [--judge-effort E] [--seed N]`` has
  the no-tool judge score the original and the runs against the committed
  rubric, and writes a grade file under the replays directory. The judge runs
  on ``replay.judge_model``/``replay.judge_effort`` unless overridden, with
  thinking unset; overrides are checked before the configuration is read. It
  spends real tokens, so it needs ``CLAUDE_CONFIG_DIR`` like ``run``. Exit 1
  means the grade was written without scores (unparseable, refused, error).

``cases`` and ``rebuild`` belong to a later task group (12b) and are not
commands yet.

Guards, each before any model call:

- the audit log must exist: a missing one means compose resolved the project to a
  copy of the checkout (``henk.old`` becomes ``henkold``) with empty volumes;
- ``run`` needs ``CLAUDE_CONFIG_DIR``, so the bundled CLI's state stays apart
  from the live process's.

Exit codes: 0 done, 2 refused (nothing was run).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any, Callable, Mapping, Sequence, TextIO

from henk.config import Config
from henk.replay import compare as compare_mod
from henk.replay import grade as grade_mod
from henk.replay.case import iso
from henk.replay.recorder import list_recordings, load_recording
from henk.replay.run import (
    ReplayRefused,
    _default_create_session,
    check_audit_log,
    resolve_source,
    run_replay,
    validate_effort,
    validate_model,
    validate_thinking,
    write_run,
)

logger = logging.getLogger("henk.replay")

REFUSED = 2


def _load_config_from_env() -> Config:  # pragma: no cover - the container path
    return Config.load(os.environ.get("HENK_CONFIG", "config.yaml"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m henk.replay",
        description="Replay a recorded event triage against a chosen model. Run it "
        "only as `docker compose run --rm --no-deps -e CLAUDE_CONFIG_DIR=... henk` "
        "from the henk checkout directory.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the triage recordings")
    run = commands.add_parser("run", help="replay one recording or reference case")
    run.add_argument("id", help="a recording id, or a reference case id")
    run.add_argument("--model", required=True, help="a Claude model identifier")
    run.add_argument("--effort", required=True, help="an SDK effort level")
    run.add_argument("--thinking", default=None, help="a thinking mode (default: unset)")
    _add_compare_parser(commands)
    _add_grade_parser(commands)
    return parser


def _add_compare_parser(commands) -> None:
    compare = commands.add_parser(
        "compare", help="show the original triage beside its replay runs (reads files only)")
    compare.add_argument("id", help="a recording id, or a reference case id")
    compare.add_argument("runs", nargs="*", help="run ids (default: every run of it)")
    compare.set_defaults(handler=compare_mod.command)


def _add_grade_parser(commands) -> None:
    grade = commands.add_parser(
        "grade", help="have the no-tool judge score the original and its runs")
    grade.add_argument("id", help="a recording id, or a reference case id")
    grade.add_argument("runs", nargs="*", help="run ids (default: every run of it)")
    grade.add_argument("--judge-model", default=None,
                       help="a Claude model identifier (default: replay.judge_model)")
    grade.add_argument("--judge-effort", default=None,
                       help="an SDK effort level (default: replay.judge_effort)")
    grade.add_argument("--seed", type=int, default=None,
                       help="the candidate-order seed (default: drawn and recorded)")
    grade.set_defaults(handler=grade_mod.command, precheck=grade_mod.precheck)


def _list(config: Config, stdout: TextIO, stderr: TextIO) -> int:
    directory = config.audit.triage_recordings_dir
    for recording_id in list_recordings(directory):
        try:
            recording = load_recording(directory, recording_id)
            identities = ",".join(
                str(i.get("identity_key")) for i in recording.get("incidents") or ()
            )
            outcome = (recording.get("ending") or {}).get("outcome")
            complete = "complete" if recording.get("complete") else "incomplete"
            at = iso(recording.get("at"))
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            print(f"warning: skipping unreadable recording {recording_id} "
                  f"({type(exc).__name__})", file=stderr)
            continue
        print(f"{recording_id}\t{at}\t{identities}\t{outcome}\t{complete}", file=stdout)
    return 0


def _drift_lines(drift: Mapping[str, Any]) -> list[str]:
    lines = []
    for key, words in (("system_prompt", "system prompt"),
                       ("tool_definitions", "tool definitions")):
        status = drift[key]["status"]
        if status == "differs":
            lines.append(f"DRIFT: the {words} differ from the recording's")
        elif status == "unknown":
            lines.append(f"drift: the recording holds no {words} hash, so they cannot "
                         "be compared")
    for entry in drift.get("capture") or ():
        lines.append(
            f"DRIFT: captured {entry['query']}/{entry['role']} "
            f"{entry['arguments']} is not served: {'; '.join(entry['mismatches'])}"
        )
    return lines


def _run(config: Config, args, *, create_session, clock, stdout, stderr) -> int:
    source = resolve_source(config, args.id)
    outcome = asyncio.run(run_replay(
        config, source, model=args.model, effort=args.effort, thinking=args.thinking,
        create_session=create_session, clock=clock,
    ))
    record = outcome.record
    path = write_run(config, source, record)
    ending = record["ending"]["outcome"]
    print(f"run {record['run_id']} of {source.kind} {source.source_id}", file=stdout)
    print(f"  profile: {args.model}, effort {args.effort}, thinking "
          f"{args.thinking or 'unset'}", file=stdout)
    print(f"  ending: {ending}; tool calls: {len(record['tool_calls'])}; unrecorded: "
          f"{record['unrecorded_count']}; unavailable: {record['unavailable_count']}; "
          f"captured handoffs: {len(record['captured_handoffs'])}", file=stdout)
    for line in _drift_lines(record["drift"]):
        print(f"  {line}", file=stdout)
    if record["case"] and record["case"]["statement"]:
        print(f"  {record['case']['statement']}", file=stdout)
    print(f"  written to {path}", file=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    load_config: Callable[[], Config] = _load_config_from_env,
    create_session: Callable | None = None,
    clock: Callable[[], float] = time.time,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    environ = os.environ if environ is None else environ
    args = _parser().parse_args(argv)
    try:
        precheck = getattr(args, "precheck", None)
        if precheck is not None:
            precheck(args, environ)
        if args.command == "run":
            # Checked before the configuration is even read: nothing can spend.
            validate_model(args.model)
            validate_effort(args.effort)
            validate_thinking(args.thinking)
            if not environ.get("CLAUDE_CONFIG_DIR"):
                raise ReplayRefused(
                    "CLAUDE_CONFIG_DIR is not set. A replay keeps the bundled CLI's "
                    "state apart from the live process's: run it with "
                    "`-e CLAUDE_CONFIG_DIR=/tmp/henk-replay`. No model was called."
                )
        config = load_config()
        check_audit_log(config)
        handler = getattr(args, "handler", None)
        if handler is not None:
            return handler(config, args, clock=clock, stdout=stdout, stderr=stderr,
                           create_session=create_session or _default_create_session)
        if args.command == "list":
            return _list(config, stdout, stderr)
        return _run(config, args, clock=clock, stdout=stdout, stderr=stderr,
                    create_session=create_session or _default_create_session)
    except ReplayRefused as exc:
        print(f"replay refused: {exc}", file=stderr)
        return REFUSED


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=os.environ.get("HENK_LOG_LEVEL", "WARNING"))
    raise SystemExit(main())
