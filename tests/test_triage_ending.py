"""The triage ending classifier and the incomplete-triage notice (group 9).

From `specs/incident-triage` (*An incomplete triage is reported, never silent*: *An
API error rendered as text is not delivered as the triage*, *A refusal produces an
honest notice*, *A result-level API error is reported with its status*, *An empty
reply is no longer silent*, *An errored triage is reported*, *The notice carries the
suppressed count*, *A suppressed incomplete triage stays silent*; and *Every incident
message ends with the triage arc*: *The incomplete-triage notice carries a pickup path
and no invented diagnosis*) and design D12. Tasks 9.2 and 9.3.

The fake SDK stream mirrors the field paths recorded in `notes/evidence-probe.md`
(section 1.2 and the 0.2.157 re-read) and checked against the unpacked 0.2.157 wheel:
`AssistantMessage.error` and `.stop_reason` (`types.py:1146,1149`),
`ResultMessage.is_error`, `.stop_reason`, `.api_error_status` and `.terminal_reason`
(`types.py:1346,1349,1361,1363`). One failing turn yields the `ResultMessage` with
`is_error` AND then raises, in that order (`_internal/query.py:390-455`); the live
1.3 probe saw `subtype="success"` with `is_error=True`.

Placeholders only (standing rule 1). No model call (standing rule 5).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from henk.agent.core import AgentCore, TRIAGE_FAILURE_NOTICE
from henk.agent.ending import (
    ASSISTANT_ERROR_CLASSES,
    COMPLETED,
    ERROR,
    INCIDENT_NAME_MAX,
    NO_REPLY,
    NOTICE_MAX_CHARS,
    REFUSED,
    TriageEnding,
    classify_ending,
    incomplete_triage_notice,
)
from henk.agent.sdk_session import _SdkAgentSession
from henk.agent.session import TurnEnding
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog
from henk.events.identity import derive_identity
from henk.events.types import Event
from tests.conftest import (
    TRIAGE_REPLY,
    EventSession,
    FakeChannel,
    handoff_stats,
    make_clock,
)

TITLE = "[FIRING:1] HenkDiskFull henk (host-a.example)"
#: Placeholder CLI-rendered error text; never a real payload.
API_ERROR_TEXT = "API Error: 400 placeholder error body from the example CLI"
#: A model-text canary: it must never reach the notice.
CANARY = "canary-model-text-7f3a"


# --- Fake SDK message shapes (the 0.2.157 field sets) ------------------------


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list
    model: str
    parent_tool_use_id: str | None = None
    error: str | None = None
    usage: dict | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    stop_reason: str | None = None
    total_cost_usd: float | None = None
    usage: dict | None = None
    result: str | None = None
    structured_output: Any = None
    model_usage: dict | None = None
    permission_denials: list | None = None
    deferred_tool_use: Any = None
    errors: list | None = None
    api_error_status: int | None = None
    uuid: str | None = None
    terminal_reason: str | None = None
    origin: Any = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict = field(default_factory=dict)


@dataclass
class MirrorErrorMessage(SystemMessage):
    """A SystemMessage subclass carrying an ``error`` string (types.py:1284-1297).

    It is not an assistant error, and must not be read as one."""

    key: Any = None
    error: str = ""


def _assistant(text: str = TRIAGE_REPLY, *, error=None, stop_reason="end_turn",
               model="claude-sonnet-5") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)] if text else [], model=model,
                            error=error, stop_reason=stop_reason)


def _result(*, is_error=False, stop_reason="end_turn", api_error_status=None,
            terminal_reason="completed", subtype="success", errors=None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s-1", stop_reason=stop_reason, total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1}, errors=errors,
        api_error_status=api_error_status, terminal_reason=terminal_reason,
    )


def test_the_fake_shapes_match_the_installed_sdk():
    sdk = pytest.importorskip("claude_agent_sdk")
    for fake, real in ((AssistantMessage, sdk.AssistantMessage),
                       (ResultMessage, sdk.ResultMessage)):
        assert [f.name for f in dataclasses.fields(fake)] == [
            f.name for f in dataclasses.fields(real)
        ]


class _FakeClient:
    """A ClaudeSDKClient double: each turn is a list of messages, where an
    Exception instance is raised at that point in the stream."""

    def __init__(self, turns: list[list]) -> None:
        self._turns = list(turns)

    async def connect(self) -> None:
        pass

    async def query(self, text: str) -> None:
        if self._turns and isinstance(self._turns[0], Exception):
            raise self._turns.pop(0)

    async def receive_response(self):
        for message in self._turns.pop(0):
            if isinstance(message, Exception):
                raise message
            yield message

    async def disconnect(self) -> None:
        pass


# --- The classifier -------------------------------------------------------


def _signals(**kw) -> TurnEnding:
    return TurnEnding(**kw)


GOOD = TRIAGE_REPLY


@pytest.mark.parametrize(
    "signals, raised, reply, expected",
    [
        # 1. an assistant error wins over everything below it, text included.
        (_signals(assistant_error="rate_limit", refusal=True, result_is_error=True,
                  api_error_status=529), True, GOOD, ERROR),
        (_signals(assistant_error="server_error"), False, GOOD, ERROR),
        # 2. a refusal wins over a result error, a raise and the text.
        (_signals(refusal=True, result_is_error=True, api_error_status=500), True,
         GOOD, REFUSED),
        (_signals(refusal=True), False, GOOD, REFUSED),
        (_signals(refusal=True), False, "", REFUSED),
        # 3. a result-level error wins over a raise and the text.
        (_signals(result_is_error=True), True, GOOD, ERROR),
        (_signals(result_is_error=True), False, GOOD, ERROR),
        (_signals(api_error_status=529), False, GOOD, ERROR),
        (_signals(terminal_reason="max_turns"), False, GOOD, ERROR),
        (_signals(terminal_reason="aborted_streaming"), False, GOOD, ERROR),
        # 4. a raise wins over the text.
        (None, True, GOOD, ERROR),
        (None, True, "", ERROR),
        # 5. then the text heuristic: a CLI-rendered API error with no signal.
        (None, False, API_ERROR_TEXT, ERROR),
        (None, False, "  " + API_ERROR_TEXT, ERROR),
        # 6. empty reply.
        (None, False, "", NO_REPLY),
        (None, False, None, NO_REPLY),
        (_signals(terminal_reason="completed"), False, "   \n", NO_REPLY),
        # 7. otherwise completed.
        (None, False, GOOD, COMPLETED),
        (_signals(), False, GOOD, COMPLETED),
        (_signals(terminal_reason="completed"), False, GOOD, COMPLETED),
        (_signals(terminal_reason=None), False, GOOD, COMPLETED),
    ],
)
def test_the_classifier_checks_structured_signals_first(signals, raised, reply, expected):
    assert classify_ending(signals, raised=raised, reply=reply).outcome == expected


def test_the_live_probe_shape_is_an_error_not_a_success():
    # 1.3: subtype "success" with is_error True, api_error_status 400, empty errors.
    ending = classify_ending(
        _signals(assistant_error="unknown", result_is_error=True, api_error_status=400),
        raised=True, reply=API_ERROR_TEXT,
    )
    assert ending == TriageEnding(ERROR, error_class="unknown", http_status=400)


def test_the_error_class_is_the_closed_enum_or_unknown():
    for value in sorted(ASSISTANT_ERROR_CLASSES):
        assert classify_ending(_signals(assistant_error=value), raised=False,
                               reply="").error_class == value
    # Anything outside the closed set is content the CLI sent, never rendered.
    ending = classify_ending(_signals(assistant_error=CANARY), raised=False, reply="")
    assert ending.outcome == ERROR and ending.error_class == "unknown"


def test_the_status_is_carried_only_when_it_is_an_http_status():
    for status, shown in ((529, 529), (400, 400), (True, None), ("529", None),
                          (99, None), (600, None), (None, None)):
        ending = classify_ending(_signals(result_is_error=True, api_error_status=status),
                                 raised=False, reply="")
        assert ending.outcome == ERROR
        assert ending.http_status == shown, status


def test_a_raise_after_a_result_error_keeps_the_status():
    ending = classify_ending(_signals(result_is_error=True, api_error_status=529),
                             raised=True, reply="")
    assert ending == TriageEnding(ERROR, error_class=None, http_status=529)


def test_a_non_error_ending_carries_no_class_or_status():
    for signals, raised, reply in (
        (_signals(refusal=True, api_error_status=None), False, GOOD),
        (None, False, ""),
        (None, False, GOOD),
    ):
        ending = classify_ending(signals, raised=raised, reply=reply)
        assert ending.error_class is None and ending.http_status is None


# --- The session's ending() ---------------------------------------------------


async def _run(turns: list[list]) -> tuple[_SdkAgentSession, list]:
    session = _SdkAgentSession(_FakeClient(turns))
    outcomes = []
    for _ in turns:
        try:
            outcomes.append(await session.run_turn("x"))
        except Exception as exc:  # noqa: BLE001 - recording what the turn did
            outcomes.append(exc)
    return session, outcomes


async def test_ending_reads_the_assistant_error_and_stop_reason_placements():
    session, _ = await _run([[_assistant(API_ERROR_TEXT, error="rate_limit"), _result()]])
    assert session.ending().assistant_error == "rate_limit"
    session, _ = await _run([[_assistant(stop_reason="refusal"), _result()]])
    assert session.ending().refusal is True
    session, _ = await _run([[_assistant(), _result(stop_reason="refusal")]])
    assert session.ending().refusal is True
    session, _ = await _run([[_assistant(), _result()]])
    assert session.ending() == TurnEnding(terminal_reason="completed")


async def test_ending_records_the_result_error_before_the_raise():
    # The ordering note: ResultMessage(is_error) is yielded, THEN the stream raises.
    boom = Exception("Claude Code returned an error result: placeholder")
    session, outcomes = await _run([[
        _assistant(API_ERROR_TEXT, error="unknown", stop_reason="stop_sequence",
                   model="<synthetic>"),
        _result(is_error=True, stop_reason="stop_sequence", api_error_status=400,
                errors=[]),
        boom,
    ]])
    assert outcomes == [boom]
    assert session.ending() == TurnEnding(
        assistant_error="unknown", result_is_error=True, api_error_status=400,
        terminal_reason="completed",
    )


async def test_ending_reads_terminal_reason():
    session, _ = await _run([[_assistant(), _result(terminal_reason="max_turns")]])
    assert session.ending().terminal_reason == "max_turns"


async def test_ending_reflects_the_last_turn_only():
    session, _ = await _run([
        [_assistant(API_ERROR_TEXT, error="server_error"),
         _result(is_error=True, api_error_status=529, stop_reason="refusal")],
        [_assistant(), _result()],
    ])
    assert session.ending() == TurnEnding(terminal_reason="completed")


async def test_ending_is_reset_even_when_the_next_turn_fails_before_any_message():
    session, outcomes = await _run([
        [_assistant(error="rate_limit"), _result(is_error=True, api_error_status=429)],
        RuntimeError("placeholder query failure"),
    ])
    assert isinstance(outcomes[1], RuntimeError)
    assert session.ending() == TurnEnding()


async def test_a_mirror_error_system_message_is_not_an_assistant_error():
    session, _ = await _run([[
        MirrorErrorMessage(subtype="mirror_error", error="placeholder store failure"),
        SystemMessage(subtype="init"),
        _assistant(),
        _result(),
    ]])
    assert session.ending() == TurnEnding(terminal_reason="completed")


async def test_a_session_ending_before_any_turn_reports_nothing():
    assert _SdkAgentSession(_FakeClient([])).ending() == TurnEnding()


# --- The notice -------------------------------------------------------------


def _turn(*titles: str, announceable: bool = True, suppressed: int = 0) -> EventTurn:
    items = []
    for i, title in enumerate(titles or (TITLE,)):
        # No message: the identity's name then comes from the title, so each
        # item's name is the one the test wrote.
        event = Event(id=f"e{i}", title=title, message="", arrival_time=0.0)
        items.append(EventTurnItem(event=event, identity=derive_identity(event)))
    return EventTurn(items=tuple(items), announceable=announceable,
                     suppressed_count=suppressed)


def _lines(text: str) -> list[str]:
    return text.splitlines()


@pytest.mark.parametrize(
    "ending, phrase",
    [
        (TriageEnding(REFUSED), "the model declined the request"),
        (TriageEnding(ERROR), "the triage failed with an error."),
        (TriageEnding(ERROR, error_class="rate_limit"),
         "the triage failed with an error (rate_limit)."),
        (TriageEnding(ERROR, http_status=529), "the triage failed with an error (HTTP 529)."),
        (TriageEnding(ERROR, error_class="unknown", http_status=400),
         "the triage failed with an error (unknown, HTTP 400)."),
        (TriageEnding(NO_REPLY), "the model produced no reply"),
    ],
)
def test_the_notice_states_how_the_triage_ended(ending, phrase):
    turn = _turn()
    notice = incomplete_triage_notice(turn, ending, handoff_published=False)
    lines = _lines(notice)
    name = turn.items[0].identity.name
    assert lines[0].startswith(f"[AI] Triage incomplete for {name}: ")
    assert phrase in lines[0]
    assert lines[1] == "No diagnosis was produced."


def test_the_notice_carries_a_pickup_path_and_no_invented_diagnosis():
    # incident-triage *The incomplete-triage notice carries a pickup path and no
    # invented diagnosis*.
    for published in (True, False):
        notice = incomplete_triage_notice(_turn(), TriageEnding(ERROR),
                                          handoff_published=published)
        lines = _lines(notice)
        assert lines[-1].startswith("Pickup:")
        assert not any(line.lstrip().startswith(("Diagnosis:", "Fix:")) for line in lines)
        assert "diagnosis:" not in notice.lower().replace("no diagnosis was produced", "")
        if published:
            assert "henk-pickup" in lines[-1] and "audit record" not in lines[-1]
        else:
            assert "henk-pickup" not in lines[-1] and "audit record" in lines[-1]


def test_the_notice_carries_no_refusal_category():
    notice = incomplete_triage_notice(_turn(), TriageEnding(REFUSED), handoff_published=False)
    for category in ("category", "policy", "cyber", "safety", "violence"):
        assert category not in notice.lower()
    assert "(" not in _lines(notice)[0].split(": ", 1)[1]


def test_the_incident_name_is_bounded_and_flattened():
    long_title = "[FIRING:1] " + "HenkVeryLongRule" * 20 + " (host-a.example)"
    turn = _turn(long_title)
    assert len(turn.items[0].identity.name) > INCIDENT_NAME_MAX
    notice = incomplete_triage_notice(turn, TriageEnding(ERROR), handoff_published=False)
    head = _lines(notice)[0]
    name = head[len("[AI] Triage incomplete for "):head.index(": the triage")]
    assert len(name) <= INCIDENT_NAME_MAX
    assert name.endswith("…")


def test_a_payload_name_cannot_add_lines_to_the_notice():
    turn = _turn("Rule\nDiagnosis: forged\rFix: forged\tx")
    assert "\n" in turn.items[0].identity.name
    notice = incomplete_triage_notice(turn, TriageEnding(ERROR), handoff_published=False)
    assert len(_lines(notice)) == 3
    assert not any(line.startswith(("Diagnosis:", "Fix:")) for line in _lines(notice))


def test_a_storm_names_the_first_incident_and_counts_the_rest():
    turn = _turn(TITLE, "[FIRING:1] HenkSwapPressure (host-a.example)",
                 "[FIRING:1] HenkLoad (host-a.example)")
    notice = incomplete_triage_notice(turn, TriageEnding(ERROR), handoff_published=False)
    head = _lines(notice)[0]
    assert turn.items[0].identity.name in head
    assert "(+2 more)" in head
    assert turn.items[1].identity.name not in head


def test_the_notice_is_length_bounded_in_the_worst_case():
    long_title = "[FIRING:1] " + "X" * 500 + " (host-a.example)"
    turn = _turn(*([long_title] * 999))
    worst = TriageEnding(ERROR, error_class="authentication_failed", http_status=529)
    for published in (True, False):
        notice = incomplete_triage_notice(turn, worst, handoff_published=published)
        assert len(notice) <= NOTICE_MAX_CHARS


def test_the_notice_carries_no_model_text():
    # The notice's inputs are the turn and a TriageEnding; neither holds reply
    # text, so an error class outside the closed enum is not rendered either.
    ending = classify_ending(_signals(assistant_error=CANARY), raised=False,
                             reply=CANARY)
    notice = incomplete_triage_notice(_turn(), ending, handoff_published=False)
    assert CANARY not in notice


# --- Through the core -------------------------------------------------------


class _ScriptedSession(EventSession):
    """An EventSession reporting a scripted ending (or raising)."""

    def __init__(self, reply: str, ending: TurnEnding | None, *, raises=None,
                 stats=None) -> None:
        super().__init__(reply, stats)
        self._ending = ending
        self._raises = raises

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        if self._raises is not None:
            raise self._raises
        return self.reply

    def ending(self):
        return self._ending


class _Factory:
    def __init__(self, make) -> None:
        self._make = make
        self.created: list = []

    def create(self):
        session = self._make()
        self.created.append(session)
        return session


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()
            and json.loads(line).get("record_type") == "session"]


async def _triage(tmp_path, make, *, announceable=True, suppressed=0, stats=None):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = AgentCore(_Factory(make), channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(announceable=announceable, suppressed=suppressed))
    (record,) = _records(tmp_path / "a.jsonl")
    return channel, record


async def test_an_api_error_rendered_as_text_is_not_delivered_as_the_triage(tmp_path):
    # incident-triage *An API error rendered as text is not delivered as the triage*,
    # through the real session adapter over the fake stream.
    def make():
        return _SdkAgentSession(_FakeClient([[
            _assistant(API_ERROR_TEXT, error="unknown", stop_reason="stop_sequence",
                       model="<synthetic>"),
            _result(is_error=True, stop_reason="stop_sequence", api_error_status=400,
                    errors=[]),
            Exception("Claude Code returned an error result: placeholder"),
        ]]))

    channel, record = await _triage(tmp_path, make)
    (sent,) = channel.sent
    assert "API Error" not in sent and "placeholder" not in sent
    assert sent.startswith("[AI] Triage incomplete for ")
    assert "(unknown, HTTP 400)" in sent
    assert record["outcome"] == ERROR
    assert record["triage_arc_complete"] is False
    assert record["diagnosis"] is None and record["confidence"] is None


async def test_an_assistant_error_with_a_well_formed_reply_is_still_withheld(tmp_path):
    # Structured signals first: a reply that LOOKS like a full triage is not
    # considered when the turn carries an assistant error.
    channel, record = await _triage(
        tmp_path, lambda: _ScriptedSession(TRIAGE_REPLY + CANARY,
                                           TurnEnding(assistant_error="server_error")))
    (sent,) = channel.sent
    assert CANARY not in sent and "Diagnosis:" not in sent
    assert record["outcome"] == ERROR and record["triage_arc_complete"] is False
    assert record["diagnosis"] is None


@pytest.mark.parametrize("placement", ["assistant", "result"])
async def test_a_refusal_produces_an_honest_notice(tmp_path, placement):
    # incident-triage *A refusal produces an honest notice*, both placements.
    assistant_stop = "refusal" if placement == "assistant" else "end_turn"
    result_stop = "refusal" if placement == "result" else "end_turn"

    def make():
        return _SdkAgentSession(_FakeClient([[
            _assistant("I can't help with that. " + CANARY, stop_reason=assistant_stop),
            _result(stop_reason=result_stop),
        ]]))

    channel, record = await _triage(tmp_path, make)
    (sent,) = channel.sent
    assert "the model declined the request" in sent
    assert "No diagnosis was produced." in sent
    assert CANARY not in sent and "can't help" not in sent
    assert "category" not in sent.lower()
    assert record["outcome"] == REFUSED
    assert record["triage_arc_complete"] is False


async def test_a_result_level_api_error_is_reported_with_its_status(tmp_path):
    # incident-triage *A result-level API error is reported with its status*.
    def make():
        return _SdkAgentSession(_FakeClient([[
            _assistant(TRIAGE_REPLY),
            _result(is_error=True, api_error_status=529),
        ]]))

    channel, record = await _triage(tmp_path, make)
    (sent,) = channel.sent
    assert "the triage failed with an error (HTTP 529)." in sent
    assert "Diagnosis:" not in sent
    assert record["outcome"] == ERROR


async def test_an_empty_reply_is_no_longer_silent(tmp_path):
    # incident-triage *An empty reply is no longer silent*.
    channel, record = await _triage(tmp_path, lambda: _ScriptedSession("", TurnEnding()))
    (sent,) = channel.sent
    assert "the model produced no reply" in sent
    assert record["outcome"] == NO_REPLY
    assert record["triage_arc_complete"] is False


async def test_an_errored_triage_is_reported(tmp_path):
    # incident-triage *An errored triage is reported*.
    channel, record = await _triage(
        tmp_path,
        lambda: _ScriptedSession("", None, raises=RuntimeError("placeholder " + CANARY)))
    (sent,) = channel.sent
    assert "the triage failed with an error." in sent
    assert CANARY not in sent and "RuntimeError" not in sent
    assert record["outcome"] == ERROR
    assert record["triage_arc_complete"] is False
    assert record["turn_count"] == 0


async def test_the_notice_carries_the_suppressed_count(tmp_path):
    # incident-triage *The notice carries the suppressed count*, via
    # _with_suppressed_note, after the Pickup line.
    channel, _ = await _triage(tmp_path, lambda: _ScriptedSession("", TurnEnding()),
                               suppressed=3)
    (sent,) = channel.sent
    lines = sent.splitlines()
    pickup = next(i for i, line in enumerate(lines) if line.startswith("Pickup:"))
    assert "3 earlier incidents were suppressed" in "\n".join(lines[pickup + 1:])
    assert sent == AgentCore._with_suppressed_note(
        incomplete_triage_notice(_turn(), TriageEnding(NO_REPLY), handoff_published=False),
        _turn(suppressed=3),
    )


async def test_the_notice_goes_on_the_proactive_path_with_the_triage_failure_notice(tmp_path):
    channel, _ = await _triage(tmp_path, lambda: _ScriptedSession("", TurnEnding()))
    ((kind, _text, failure_notice),) = channel.calls
    assert (kind, failure_notice) == ("proactive", TRIAGE_FAILURE_NOTICE)


@pytest.mark.parametrize(
    "ending, reply, raises, outcome",
    [
        (TurnEnding(assistant_error="rate_limit"), API_ERROR_TEXT, None, ERROR),
        (TurnEnding(refusal=True), "declined", None, REFUSED),
        (TurnEnding(result_is_error=True, api_error_status=529), "", None, ERROR),
        (TurnEnding(), "", None, NO_REPLY),
        (None, "", RuntimeError("placeholder"), ERROR),
    ],
)
async def test_a_suppressed_incomplete_triage_stays_silent(tmp_path, ending, reply,
                                                           raises, outcome):
    # incident-triage *A suppressed incomplete triage stays silent*: no send, and
    # the record carries the ending's outcome.
    channel, record = await _triage(
        tmp_path, lambda: _ScriptedSession(reply, ending, raises=raises),
        announceable=False, suppressed=2)
    assert channel.sent == []
    assert record["outcome"] == outcome
    assert record["triage_arc_complete"] is False


async def test_a_published_handoff_points_the_notice_at_henk_pickup(tmp_path):
    channel, record = await _triage(
        tmp_path,
        lambda: _ScriptedSession("", TurnEnding(), stats=handoff_stats("hf-9")))
    (sent,) = channel.sent
    assert record["handoff_message_id"] == "hf-9"
    pickup = [line for line in sent.splitlines() if line.startswith("Pickup:")]
    assert pickup and "henk-pickup" in pickup[0]


async def test_a_completed_triage_is_delivered_unchanged(tmp_path):
    channel, record = await _triage(
        tmp_path, lambda: _ScriptedSession(TRIAGE_REPLY, TurnEnding(terminal_reason="completed")))
    assert channel.sent == [TRIAGE_REPLY]
    assert record["outcome"] == COMPLETED
    assert record["triage_arc_complete"] is True
    assert record["diagnosis"]


# --- 9.3: sessions without ending() -----------------------------------------


class _NoEndingSession(EventSession):
    """The conftest shape: run_turn/close/stats, no ending()."""


async def test_a_session_without_ending_reports_no_signal(tmp_path):
    assert not hasattr(_NoEndingSession(), "ending")
    channel, record = await _triage(tmp_path, lambda: _NoEndingSession(TRIAGE_REPLY))
    assert channel.sent == [TRIAGE_REPLY]
    assert record["outcome"] == COMPLETED


async def test_a_session_without_ending_still_gets_the_text_and_raise_checks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    channel, record = await _triage(tmp_path / "a", lambda: _NoEndingSession(""))
    assert record["outcome"] == NO_REPLY and len(channel.sent) == 1
    channel, record = await _triage(tmp_path / "b",
                                    lambda: _NoEndingSession(API_ERROR_TEXT))
    assert record["outcome"] == ERROR
    assert "API Error" not in channel.sent[0]


async def test_an_ending_that_raises_or_returns_none_is_no_signal(tmp_path):
    class Broken(_NoEndingSession):
        def ending(self):
            raise RuntimeError("placeholder")

    class Empty(_NoEndingSession):
        def ending(self):
            return None

    class Foreign(_NoEndingSession):
        def ending(self):
            return {"assistant_error": "rate_limit"}  # not a TurnEnding

    for i, cls in enumerate((Broken, Empty, Foreign)):
        d = tmp_path / str(i)
        d.mkdir()
        channel, record = await _triage(d, lambda cls=cls: cls(TRIAGE_REPLY))
        assert channel.sent == [TRIAGE_REPLY]
        assert record["outcome"] == COMPLETED

