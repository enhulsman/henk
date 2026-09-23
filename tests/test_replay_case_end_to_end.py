"""The 2026-09-23 case end to end: `rebuild`, `cases`, `run`, `compare`, `grade`
(triage-quality groups 12a and 12b together; tasks 12.2, 12.3 and 12.5).

One chain over the committed placeholder fixture
(`tests/fixtures/replay/case-2026-09-23/`), entirely under `tmp_path`, through
the owner's entry point (`python -m henk.replay`, via `cli.main`):

1. `rebuild` writes the three reconstructed cases;
2. `cases` lists exactly those three ids;
3. `run` replays one of them on a fake session;
4. `compare` shows the original's handoff beside the replay;
5. `grade` has a fake judge score them, and the grade names the reference.

It proves the seam between the two groups: a rebuilt case's recording has
`reply: null` and calls with `arguments: "unknown"`, so the original candidate
must come from the case's `original_candidate`, and never be graded empty.

Standing rule 5: no real model. Both sessions are fakes over the production
factories. Placeholders only (standing rule 1).
"""

from __future__ import annotations

import io
import json
import shutil

from henk.replay import __main__ as cli
from henk.replay import compare as compare_mod
from henk.replay import grade as grade_mod
from henk.tools.notify import AI_LABEL
from tests import replay_case_fixture as fx
from tests.replay_fakes import OTHER_MODEL, SessionMaker, forbidden_session_maker, make_config
from tests.test_replay_grade import MODEL_WORDS, JudgeMaker, data_block, valid_reply

CAPTURE_NAME = "2026-09-23-capture"
INPUTS_NAME = "2026-09-23-inputs"
PREFIX = "2026-09-23-swap"
SWAP_QUERY = {"query_name": "node_resource_trend", "node": "vps",
              "resource": "swap_used", "window": "1h"}
REPLAY_REPLY = ("Diagnosis: page-cache burst in example-a.service filled swap "
                "(confidence: high)\nFix: run it at boot only and grow swap\n"
                "Pickup: run henk-pickup")
STATEMENT_WORDS = "grades the current renderers"


def _main(config, argv, *, create_session=forbidden_session_maker):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, load_config=lambda: config, create_session=create_session,
                    clock=lambda: float(fx.TRIAGE_AT + 86400), stdout=out, stderr=err,
                    environ={"CLAUDE_CONFIG_DIR": "/tmp/henk-replay"})
    return code, out.getvalue(), err.getvalue()


def _fixture_handoff() -> str:
    [frame] = [json.loads(line) for line in fx.HANDOFFS_FILE.read_text().splitlines()
               if line.strip() and json.loads(line).get("id") == fx.HANDOFF_ID]
    message = frame["message"]
    assert message.startswith(f"{AI_LABEL} ")
    return message[len(AI_LABEL) + 1:]


def test_rebuild_cases_run_compare_grade_over_the_fixture(tmp_path):
    config = make_config(tmp_path)
    cases_dir = config.audit.triage_cases_dir
    cases_dir.mkdir(mode=0o700, parents=True)
    shutil.copytree(fx.CAPTURE_DIR, cases_dir / CAPTURE_NAME)
    shutil.copytree(fx.INPUTS_DIR, cases_dir / INPUTS_NAME)
    inputs = cases_dir / INPUTS_NAME
    handoff = _fixture_handoff()
    reference = json.loads(fx.REFERENCE_FILE.read_text())

    # 1. rebuild: three cases, spending nothing.
    code, out, err = _main(config, [
        "rebuild",
        "--events", str(inputs / fx.EVENTS_FILE.name),
        "--audit-records", str(inputs / fx.AUDIT_FILE.name),
        "--handoffs", str(inputs / fx.HANDOFFS_FILE.name),
        "--capture", str(cases_dir / CAPTURE_NAME),
        "--reference", str(inputs / fx.REFERENCE_FILE.name),
        "--case-prefix", PREFIX,
    ])
    assert code == 0, err
    assert STATEMENT_WORDS in out
    case_id = fx.CASE_IDS[0]
    case = json.loads((cases_dir / case_id / "case.json").read_text())
    assert case["recording"]["reply"] is None
    assert case["original_candidate"]["handoff_document"] == handoff

    # 2. cases: exactly the three ids.
    code, out, err = _main(config, ["cases"])
    assert code == 0, err
    assert sorted(line.split("\t")[0] for line in out.splitlines()) == sorted(fx.CASE_IDS)
    assert len(out.splitlines()) == 3

    # 3. run: one case, on a fake session.
    maker = SessionMaker([("homelab_query", dict(SWAP_QUERY))], reply=REPLAY_REPLY)
    code, out, err = _main(config, ["run", case_id, "--model", OTHER_MODEL,
                                    "--effort", "high"], create_session=maker)
    assert code == 0, err
    assert STATEMENT_WORDS in out
    [run_file] = sorted((config.audit.triage_replays_dir / case_id).glob("*.json"))
    run_id = run_file.stem

    # 4. compare: the original's handoff and recorded diagnosis beside the replay.
    code, out, err = _main(config, ["compare", case_id, run_id])
    assert code == 0, err
    assert STATEMENT_WORDS in out
    _, original, replay = out.split("\n== ")
    shown = compare_mod.terminal_safe(handoff)[:compare_mod.HANDOFF_START_CHARS]
    assert f"handoff: {shown}" in original
    assert "reply: not preserved" in original
    assert f"recorded diagnosis: {case['original_candidate']['diagnosis']}" in original
    assert f"confidence: {case['original_candidate']['confidence']}" in original
    assert run_id in replay
    assert "| Diagnosis: page-cache burst in example-a.service" in replay
    assert "1. homelab_query [" in replay and "NOT RECORDED" not in replay

    # 5. grade: a fake judge returning valid JSON.
    judge = JudgeMaker(reply=valid_reply)
    code, out, err = _main(config, ["grade", case_id, run_id, "--seed", "5"],
                           create_session=judge)
    assert code == 0, err
    assert STATEMENT_WORDS in out and "reference: used" in out
    [grade_file] = sorted(
        (config.audit.triage_replays_dir / case_id / "grades").glob("*.json"))
    grade = json.loads(grade_file.read_text())
    assert grade["status"] == "scored"
    assert grade["reference_used"] is True
    assert STATEMENT_WORDS in grade["case"]["statement"]
    assert grade["original"] == {"source": compare_mod.FROM_CASE_CANDIDATE,
                                 "reply_preserved": False}

    data = data_block(judge.prompt)
    # Blind: no model or profile reaches the judge, the original's included.
    assert MODEL_WORDS.search(judge.prompt) is None, MODEL_WORDS.search(judge.prompt)
    for candidate in data["candidates"]:
        assert set(candidate) == {"label", "ending", "reply", "handoffs", "tool_calls"}
    # The reference is the fixture's, marked as ground truth.
    assert data["verified_reference"] == {"status": grade_mod.REFERENCE_STATUS, **reference}
    assert STATEMENT_WORDS in data["case_note"]
    # The original candidate is not empty: it holds the fixture's handoff text.
    [label] = [c["label"] for c in grade["candidates"] if c["kind"] == "original"]
    [original_view] = [c for c in data["candidates"] if c["label"] == label]
    assert original_view["handoffs"] == [handoff]
    assert case["original_candidate"]["diagnosis"] in original_view["reply"]
    assert [c["arguments"] for c in original_view["tool_calls"]] == \
        ["unknown"] * len(original_view["tool_calls"]) != []
    [replay_view] = [c for c in data["candidates"] if c["label"] != label]
    assert replay_view["reply"] == REPLAY_REPLY
