"""The original candidate of a rebuilt case, in `grade` and `compare`
(triage-quality groups 12a and 12b together; tasks 12.2, 12.3 and 12.5).

`rebuild` writes the case's recording with `reply: null`, because the owner's
message was not preserved, and puts the original candidate at the top level of
`case.json` as `original_candidate`: the original handoff (without its `[AI]`
label), the audit record's diagnosis and confidence, `triage_arc_complete`,
model and `at`. The recorded calls carry `arguments: "unknown"`, so the
transcript holds no handoff document. From `specs/triage-replay` *Reconstructed
cases are built from captured backend data and say what they are* ("the
original handoff from the preserved henk-handoffs cache, plus the audit record's
diagnosis and confidence", as the original candidate) and *The judge is blind to
models*:

- the original candidate the judge grades is that handoff plus the recorded
  diagnosis and confidence, in the usual candidate shape, with a reply line the
  app composes and that cannot pass for model text;
- an empty original is never graded silently: `grade` refuses before any
  session when the original has no reply, no handoff and no recorded diagnosis
  and its ending does not say why;
- no model or profile reaches the judge.

Standing rule 5: no real model. Placeholders only (standing rule 1).
"""

from __future__ import annotations

import copy

import pytest

from henk.replay import __main__ as cli
from henk.replay import compare as compare_mod
from henk.replay import grade as grade_mod
from henk.replay.case import CaseInvalid, load_case
from tests.replay_fakes import (
    MODEL,
    OTHER_MODEL,
    forbidden_session_maker,
    live_recording,
    make_config,
    reconstructed_recording,
    write_capture,
    write_case,
    write_recording,
)
from tests.test_replay_grade import (
    MODEL_WORDS,
    REPLY_A,
    JudgeMaker,
    _grade,
    _grades,
    _main,
    _replay,
    data_block,
    valid_reply,
)

CASE_ID = "2026-09-23-swap-T062958Z"
HANDOFF = ("Trigger: swap pressure on vps (host-a.example).\n"
           "Evidence: swap_used at 98.39% and rising.\n"
           "Diagnosis: sustained swap I/O, cause not identified (confidence: moderate)")
DIAGNOSIS = "Swap on vps is rising under sustained swap I/O; no culprit named"
ORIGINAL = {
    "handoff_message_id": "fxHandoff001",
    "handoff_document": HANDOFF,
    "diagnosis": DIAGNOSIS,
    "confidence": "moderate",
    "triage_arc_complete": True,
    "model": MODEL,
    "at": 1790145026.0,
}


def _rebuilt_recording() -> dict:
    """As `rebuild` writes it: reconstructed, reply not preserved."""
    recording = reconstructed_recording()
    recording["reply"] = None
    recording["profile"] = {**recording["profile"], "model": MODEL, "effort": "high"}
    return recording


def _case(tmp_path, *, recording=None, original=ORIGINAL):
    config = make_config(tmp_path)
    capture_dir = write_capture(config.audit.triage_cases_dir)
    extra = {} if original is None else {"original_candidate": copy.deepcopy(original)}
    write_case(config, CASE_ID, recording=recording or _rebuilt_recording(),
               capture_dir=capture_dir, extra=extra)
    return config


def _with_run(tmp_path, **kw):
    config = _case(tmp_path, **kw)
    run = _replay(config, CASE_ID, OTHER_MODEL, reply=REPLY_A,
                  script=[("homelab_health", {})])
    return config, run


def _original_in(prompt: str, grade: dict) -> dict:
    [label] = [c["label"] for c in grade["candidates"] if c["kind"] == "original"]
    [candidate] = [c for c in data_block(prompt)["candidates"] if c["label"] == label]
    return candidate


# --- The case carries its original candidate -----------------------------------------


def test_load_case_carries_the_original_candidate(tmp_path):
    config = _case(tmp_path)
    case = load_case(config.audit.triage_cases_dir, CASE_ID)
    assert case.original_candidate == ORIGINAL


def test_a_case_without_one_has_none(tmp_path):
    config = _case(tmp_path, original=None)
    assert load_case(config.audit.triage_cases_dir, CASE_ID).original_candidate is None


@pytest.mark.parametrize("original", [
    "the handoff",
    {**ORIGINAL, "handoff_document": 3},
    {**ORIGINAL, "diagnosis": ["x"]},
    {**ORIGINAL, "confidence": 2},
])
def test_a_malformed_original_candidate_makes_the_case_invalid(tmp_path, original):
    config = _case(tmp_path, original=original)
    with pytest.raises(CaseInvalid, match="original_candidate"):
        load_case(config.audit.triage_cases_dir, CASE_ID)


# --- grade: the original candidate is the handoff plus the recorded diagnosis ------------


def test_a_rebuilt_case_grades_its_handoff_and_recorded_diagnosis(tmp_path):
    config, run = _with_run(tmp_path)
    maker = JudgeMaker(reply=valid_reply)
    code, out, err = _grade(config, CASE_ID, [run], maker, "--seed", "4")
    assert code == 0, err
    [grade] = _grades(config, CASE_ID)
    original = _original_in(maker.prompt, grade)
    assert original["handoffs"] == [HANDOFF]
    assert DIAGNOSIS in original["reply"] and "moderate" in original["reply"]
    assert "not preserved" in original["reply"]
    assert original["ending"] == "completed"
    assert [c["arguments"] for c in original["tool_calls"]] == ["unknown"] * 3
    assert grade["original"] == {"source": "case-original-candidate",
                                 "reply_preserved": False}


def test_the_composed_reply_cannot_pass_for_model_text(tmp_path):
    config, run = _with_run(tmp_path)
    maker = JudgeMaker(reply=valid_reply)
    _grade(config, CASE_ID, [run], maker, "--seed", "4")
    [grade] = _grades(config, CASE_ID)
    reply = _original_in(maker.prompt, grade)["reply"]
    # Bracketed, and saying who wrote it; never an arc line a triage would write.
    assert reply.startswith("[") and reply.endswith("]")
    assert "not written by the triage" in reply
    assert not any(compare_mod._ARC_LINE.match(line) for line in reply.splitlines())
    assert reply == compare_mod.REPLY_NOT_PRESERVED.format(
        diagnosis=DIAGNOSIS, confidence="moderate")
    # The judge is told what the line is, outside the data block.
    outside = maker.prompt.split(grade_mod.DATA_BEGIN)[0]
    assert grade_mod.NOT_PRESERVED_INSTRUCTION in outside


def test_without_a_composed_reply_the_judge_gets_no_such_instruction(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    run = _replay(config, rid, OTHER_MODEL, reply=REPLY_A)
    maker = JudgeMaker(reply=valid_reply)
    _grade(config, rid, [run], maker)
    assert grade_mod.NOT_PRESERVED_INSTRUCTION not in maker.prompt
    [grade] = _grades(config, rid)
    assert grade["original"] == {"source": "recording", "reply_preserved": True}


def test_a_preserved_reply_is_kept_and_the_handoff_still_added(tmp_path):
    recording = _rebuilt_recording()
    recording["reply"] = "Diagnosis: the kept reply (confidence: low)"
    config, run = _with_run(tmp_path, recording=recording)
    maker = JudgeMaker(reply=valid_reply)
    code, _, err = _grade(config, CASE_ID, [run], maker)
    assert code == 0, err
    [grade] = _grades(config, CASE_ID)
    original = _original_in(maker.prompt, grade)
    assert original["reply"] == "Diagnosis: the kept reply (confidence: low)"
    assert original["handoffs"] == [HANDOFF]
    assert grade["original"] == {"source": "case-original-candidate",
                                 "reply_preserved": True}


def test_a_missing_diagnosis_and_confidence_are_said_to_be_missing(tmp_path):
    config, run = _with_run(tmp_path, original={**ORIGINAL, "diagnosis": None,
                                                "confidence": None})
    maker = JudgeMaker(reply=valid_reply)
    code, _, err = _grade(config, CASE_ID, [run], maker)
    assert code == 0, err
    [grade] = _grades(config, CASE_ID)
    reply = _original_in(maker.prompt, grade)["reply"]
    assert reply == compare_mod.REPLY_NOT_PRESERVED.format(
        diagnosis="none recorded", confidence="none recorded")


# --- grade: an empty original is refused, before any session -------------------------------


def test_a_candidate_with_only_a_recorded_diagnosis_is_graded(tmp_path):
    config, run = _with_run(tmp_path, original={**ORIGINAL, "handoff_document": None})
    maker = JudgeMaker(reply=valid_reply)
    code, _, err = _grade(config, CASE_ID, [run], maker)
    assert code == 0, err
    [grade] = _grades(config, CASE_ID)
    original = _original_in(maker.prompt, grade)
    assert original["handoffs"] == [] and DIAGNOSIS in original["reply"]


def test_a_case_with_neither_a_reply_nor_a_candidate_spends_nothing(tmp_path):
    config, run = _with_run(tmp_path, original=None)
    code, _, err = _grade(config, CASE_ID, [run], forbidden_session_maker)
    assert code == cli.REFUSED
    assert "original" in err and "empty" in err and "No model was called" in err
    assert _grades(config, CASE_ID) == []


def test_a_candidate_with_no_handoff_and_no_diagnosis_spends_nothing(tmp_path):
    config, run = _with_run(tmp_path, original={**ORIGINAL, "handoff_document": None,
                                                "diagnosis": None})
    code, _, err = _grade(config, CASE_ID, [run], forbidden_session_maker)
    assert code == cli.REFUSED and "empty" in err
    assert _grades(config, CASE_ID) == []


def test_a_completed_live_original_with_nothing_in_it_spends_nothing(tmp_path):
    config = make_config(tmp_path)
    recording = live_recording([])
    recording["reply"] = None
    rid = write_recording(config, recording)
    run = _replay(config, rid, OTHER_MODEL, reply=REPLY_A)
    code, _, err = _grade(config, rid, [run], forbidden_session_maker)
    assert code == cli.REFUSED and "empty" in err
    assert _grades(config, rid) == []


def test_a_live_original_whose_ending_says_why_is_still_graded(tmp_path):
    # An errored triage left no reply; its ending is the explicit record (12a).
    config = make_config(tmp_path)
    recording = live_recording([])
    recording["reply"] = None
    recording["ending"] = {**recording["ending"], "outcome": "error"}
    rid = write_recording(config, recording)
    run = _replay(config, rid, OTHER_MODEL, reply=REPLY_A)
    maker = JudgeMaker(reply=valid_reply)
    code, _, err = _grade(config, rid, [run], maker)
    assert code == 0, err
    [grade] = _grades(config, rid)
    assert _original_in(maker.prompt, grade)["ending"] == "error"


# --- grade: blindness holds for the original candidate -------------------------------------


def test_the_original_candidate_names_no_model_or_profile(tmp_path):
    config, run = _with_run(tmp_path)
    maker = JudgeMaker(reply=valid_reply)
    _grade(config, CASE_ID, [run], maker, "--seed", "2")
    prompt = maker.prompt
    assert MODEL_WORDS.search(prompt) is None, MODEL_WORDS.search(prompt)
    for word in ('"model"', '"effort"', '"profile"', '"handoff_message_id"',
                 '"triage_arc_complete"', "fxHandoff001", "1790145026", run):
        assert word not in prompt, word
    for candidate in data_block(prompt)["candidates"]:
        assert set(candidate) == {"label", "ending", "reply", "handoffs", "tool_calls"}
    # The mapping, model included, is in the grade file only.
    [grade] = _grades(config, CASE_ID)
    [entry] = [c for c in grade["candidates"] if c["kind"] == "original"]
    assert entry["model"] == MODEL


# --- compare: the original's recorded diagnosis, confidence and handoff ---------------------


def test_compare_shows_a_rebuilt_case_original(tmp_path):
    config, run = _with_run(tmp_path)
    code, out, err = _main(config, ["compare", CASE_ID, run])
    assert code == 0, err
    original = out.split("\n== ")[1]
    assert original.startswith("original (reconstructed")
    assert "reply: not preserved" in original
    assert f"recorded diagnosis: {DIAGNOSIS}" in original
    assert "confidence: moderate" in original
    assert "arc: complete" in original
    assert f"handoff: {compare_mod.terminal_safe(HANDOFF)}" in original
    summary = out.split("\n== ")[0]
    assert "confidence moderate" in summary


def test_compare_says_so_when_a_case_has_no_original_candidate(tmp_path):
    config, run = _with_run(tmp_path, original=None)
    code, out, err = _main(config, ["compare", CASE_ID, run])
    assert code == 0, err
    original = out.split("\n== ")[1]
    assert "reply: not preserved" in original
    assert "no original candidate" in original
    assert "handoff: none captured" in original
