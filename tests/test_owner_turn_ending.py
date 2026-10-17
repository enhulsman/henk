"""Owner turns are classified like event turns (triage-quality carry-forward).

An owner turn's ending goes through the same classifier as a triage
(``henk.agent.ending.classify_ending``): structured SDK signals first, the
"API Error" text heuristic last. When the turn ended in an ``error``, the owner
gets the generic error reply, never the raw text, and the session's audit record
carries the classified outcome. ``refused`` keeps delivering the model's own
refusal text and ``no-reply`` keeps sending nothing, as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from henk.agent.core import DEFAULT_ERROR_REPLY, RESET_COMMAND, AgentCore
from henk.agent.ending import COMPLETED, ERROR, NO_REPLY, REFUSED
from henk.agent.session import TurnEnding
from henk.audit import AuditLog
from tests.conftest import FakeChannel, make_clock

API_ERROR_TEXT = "API Error: 500 placeholder internal error"
NORMAL_REPLY = "Everything on rp5 looks healthy."


class _OwnerSession:
    """An AgentSession fake: scripted replies and endings, one per turn."""

    def __init__(self, script: list[tuple[str, TurnEnding | None]]) -> None:
        self._script = list(script)
        self._ending: TurnEnding | None = None
        self.turns: list[str] = []

    async def run_turn(self, text: str) -> str:
        self.turns.append(text)
        reply, self._ending = self._script.pop(0)
        return reply

    def ending(self) -> TurnEnding | None:
        return self._ending

    async def close(self) -> None:
        pass


class _Factory:
    def __init__(self, script) -> None:
        self._script = script

    def create(self):
        return _OwnerSession(self._script)


def _session_records(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("record_type") == "session"]


async def _owner_turns(tmp_path, script, messages):
    """Run owner messages, then reset so the session's record is written."""
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = AgentCore(_Factory(script), channel, clock=make_clock([0]), audit=audit)
    for message in messages:
        await core.process(message)
    sent = list(channel.sent)
    await core.process(RESET_COMMAND)
    (record,) = _session_records(tmp_path / "a.jsonl")
    return sent, record


async def test_an_api_error_reply_is_replaced_by_the_error_reply(tmp_path):
    sent, record = await _owner_turns(
        tmp_path, [(API_ERROR_TEXT, None)], ["is everything up?"])
    assert sent == [DEFAULT_ERROR_REPLY]
    assert not any("API Error" in s for s in sent)
    assert record["outcome"] == ERROR


@pytest.mark.parametrize("signals", [
    TurnEnding(result_is_error=True),
    TurnEnding(assistant_error="server_error"),
    TurnEnding(api_error_status=529),
    TurnEnding(terminal_reason="max_turns"),
])
async def test_a_structured_error_signal_withholds_a_normal_looking_reply(
        tmp_path, signals):
    sent, record = await _owner_turns(
        tmp_path, [(NORMAL_REPLY, signals)], ["is everything up?"])
    assert sent == [DEFAULT_ERROR_REPLY]
    assert NORMAL_REPLY not in sent
    assert record["outcome"] == ERROR


async def test_a_completed_turn_is_delivered_unchanged(tmp_path):
    sent, record = await _owner_turns(
        tmp_path, [(NORMAL_REPLY, TurnEnding(terminal_reason="completed"))],
        ["is everything up?"])
    assert sent == [NORMAL_REPLY]
    assert record["outcome"] == COMPLETED


async def test_a_completed_turn_without_ending_signals_is_delivered_unchanged(tmp_path):
    sent, record = await _owner_turns(tmp_path, [(NORMAL_REPLY, None)], ["hi"])
    assert sent == [NORMAL_REPLY]
    assert record["outcome"] == COMPLETED


async def test_a_refusal_keeps_delivering_the_model_text(tmp_path):
    refusal = "I can't help with that one."
    sent, record = await _owner_turns(
        tmp_path, [(refusal, TurnEnding(refusal=True))], ["do the thing"])
    assert sent == [refusal]
    assert record["outcome"] == REFUSED


async def test_an_empty_reply_still_sends_nothing(tmp_path):
    sent, record = await _owner_turns(tmp_path, [("", None)], ["hi"])
    assert sent == []
    assert record["outcome"] == NO_REPLY


async def test_a_later_completed_turn_does_not_mask_an_earlier_error(tmp_path):
    sent, record = await _owner_turns(
        tmp_path,
        [(API_ERROR_TEXT, None), (NORMAL_REPLY, TurnEnding(terminal_reason="completed"))],
        ["first", "again"])
    assert sent == [DEFAULT_ERROR_REPLY, NORMAL_REPLY]
    assert record["outcome"] == ERROR
    assert record["turn_count"] == 2


# --- Mixed signals: an error signal outranks a refusal (review 028, finding 2) ---


@pytest.mark.parametrize("reply, signals", [
    ("I can't help with that one.", TurnEnding(refusal=True, result_is_error=True)),
    ("I can't help with that one.", TurnEnding(refusal=True, api_error_status=500)),
    (API_ERROR_TEXT, TurnEnding(refusal=True)),
])
async def test_a_refusal_never_masks_an_error_signal(tmp_path, reply, signals):
    sent, record = await _owner_turns(tmp_path, [(reply, signals)], ["do the thing"])
    assert sent == [DEFAULT_ERROR_REPLY]
    assert reply not in sent
    assert not any("API Error" in s for s in sent)
    assert record["outcome"] == ERROR


# --- Session setup failure still answers the owner (review 028, finding 1) ---


class _RaisingFactory:
    """A factory whose create() raises the first ``failures`` times."""

    def __init__(self, failures: int, script=()) -> None:
        self._failures = failures
        self._script = list(script)
        self.calls = 0

    def create(self):
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("placeholder session build failure")
        return _OwnerSession(self._script)


async def test_a_session_setup_failure_sends_the_error_reply(tmp_path):
    channel = FakeChannel()
    core = AgentCore(_RaisingFactory(failures=1), channel, clock=make_clock([0]),
                     audit=AuditLog(tmp_path / "a.jsonl"))
    await core.process("is everything up?")  # must not raise
    assert channel.sent == [DEFAULT_ERROR_REPLY]
    assert not any("placeholder session build failure" in s for s in channel.sent)


async def test_a_session_setup_failure_completes_the_queued_turn(tmp_path):
    import asyncio

    channel = FakeChannel()
    factory = _RaisingFactory(failures=1, script=[(NORMAL_REPLY, None)])
    core = AgentCore(factory, channel, clock=make_clock([0]),
                     audit=AuditLog(tmp_path / "a.jsonl"))
    runner = asyncio.create_task(core.run())
    try:
        await core.submit("is everything up?")
        await asyncio.wait_for(core._queue.join(), timeout=5)
        assert channel.sent == [DEFAULT_ERROR_REPLY]
        # The next owner turn builds a fresh session and is answered normally.
        await core.submit("and now?")
        await asyncio.wait_for(core._queue.join(), timeout=5)
        assert channel.sent == [DEFAULT_ERROR_REPLY, NORMAL_REPLY]
        assert factory.calls == 2
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
