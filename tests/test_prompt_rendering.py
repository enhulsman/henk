"""approval-reactions group 2: the Signal side of prompts (tasks 2.4, 2.5).

From the channel-adapter delta: *Approval prompts are one styled message, unstyled
by configuration*, the ``send_prompt`` / ``edit_prompt`` half of *Swappable
channel-adapter contract*, *Prompt edit addresses the prompt*, and *Outbound sends
are serialized* for prompts and edits. Design D2, D5; ``notes/bridge-facts.md`` for
the bridge's styled-text grammar.

**The escaping grammar under test** (bridge 0.101, ``TextstyleParser``): a single
``\\`` immediately before ``*``, a backtick, ``|`` or ``~`` is swallowed and that
character is literal; any other backslash is literal; a backslash cannot itself be
escaped. So every content backslash becomes the stand-in ``⧵`` (U+29F5), and each
styling character gets exactly one ``\\`` in front. ``_decode`` below applies those
rules to what the adapter sent, so the property tests check what the owner would
SEE, not merely which bytes went out.

Exact strings are written out in full: they are owner-facing text and the owner
approved them verbatim (proposal, "What you will see").
"""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from henk.channel.base import ApprovalPrompt, PromptOutcome, PromptSent, SendOutcome
from henk.channel.signal import REPLY_FAILURE_NOTICE, SignalAdapter, SignalBridgeError
from henk.gate.approval import ApprovalGate, TurnContext, gated_invoke
from henk.tools.base import TurnType
from tests.conftest import PROMPT_FOOTER, FakeBridge
from tests.test_approval_reactions import LabelledTool
from tests.test_channel_adapter import ACK_ACCOUNT, ACK_BASE, _json, _wire_bridge
from tests.test_gate_authorization import SpyRecorder

OWNER = "+31600000000"
ACCOUNT = "+31611111111"
STAND_IN = "⧵"
FAIL_FAST = 2.0
T = 1761830400123

CAL_PROMPT = ApprovalPrompt(
    label="Add a calendar event",
    tool_name="calendar_create_event",
    fields=(
        ("title", "Homelab: patch round"),
        ("start", "Sat 2026-11-07 14:00"),
        ("end", "Sat 2026-11-07 16:00"),
    ),
)

STYLED_PROMPT_TEXT = "\n".join(
    [
        "**Approval needed**",
        "Add a calendar event (calendar_create_event)",
        "title: `Homelab: patch round`",
        "start: `Sat 2026-11-07 14:00`",
        "end: `Sat 2026-11-07 16:00`",
        "React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it.",
    ]
)
UNSTYLED_PROMPT_TEXT = "\n".join(
    [
        "Approval needed",
        "Add a calendar event (calendar_create_event)",
        "title: <<<Homelab: patch round>>>",
        "start: <<<Sat 2026-11-07 14:00>>>",
        "end: <<<Sat 2026-11-07 16:00>>>",
        "React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it.",
    ]
)
STYLED_VALUES = "`Homelab: patch round`, `Sat 2026-11-07 14:00`, `Sat 2026-11-07 16:00`"
UNSTYLED_VALUES = (
    "<<<Homelab: patch round>>>, <<<Sat 2026-11-07 14:00>>>, <<<Sat 2026-11-07 16:00>>>"
)
VERBS = ["Approved", "Denied", "Cancelled", "Timed out"]


def test_the_shared_footer_is_the_owners_sentence():
    assert PROMPT_FOOTER == (
        "React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it."
    )


# --- Helpers -----------------------------------------------------------------


async def _nosleep(_delay: float) -> None:
    return None


def _adapter(bridge, *, styled: bool, safe_length: int = 2000, **kwargs) -> SignalAdapter:
    kwargs.setdefault("max_send_attempts", 3)
    return SignalAdapter(
        bridge,
        account=ACCOUNT,
        owner=OWNER,
        safe_length=safe_length,
        sleep=_nosleep,
        styled_prompts=styled,
        **kwargs,
    )


def _prompt(*fields: tuple[str, str], label: str = "Add a calendar event",
            tool: str = "calendar_create_event") -> ApprovalPrompt:
    return ApprovalPrompt(label=label, tool_name=tool, fields=tuple(fields))


STYLE_CHARS = "*`|~"


def _decode(line: str) -> tuple[str, list[str]]:
    """What the bridge's styled parser shows for ``line``, and its markers.

    Returns the visible text and the unescaped styling characters in order. A
    single backslash before a styling character makes it literal; every other
    character, a lone backslash included, is literal.
    """
    shown: list[str] = []
    markers: list[str] = []
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line) and line[i + 1] in STYLE_CHARS:
            shown.append(line[i + 1])
            i += 2
            continue
        if char in STYLE_CHARS:
            markers.append(char)
        else:
            shown.append(char)
        i += 1
    return "".join(shown), markers


def _sent(bridge: FakeBridge) -> dict:
    [request] = bridge.requests
    return request


# =============================================================================
# Exact prompt and outcome text
# =============================================================================


async def test_the_styled_prompt_is_verbatim():
    # Scenario: Styled prompt. One styled send to the owner, the exact text.
    bridge = FakeBridge()
    bridge.send_results = [T]
    sent = await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)

    assert sent == PromptSent(SendOutcome.DELIVERED, str(T))
    request = _sent(bridge)
    assert request["recipient"] == OWNER
    assert request["styled"] is True
    assert request["edit_timestamp"] is None
    assert request["text"] == STYLED_PROMPT_TEXT


async def test_the_unstyled_prompt_is_verbatim():
    # Scenario: Styling disabled by configuration (the prompt half).
    bridge = FakeBridge()
    bridge.send_results = [T]
    sent = await _adapter(bridge, styled=False).send_prompt(CAL_PROMPT)

    assert sent == PromptSent(SendOutcome.DELIVERED, str(T))
    request = _sent(bridge)
    assert request["styled"] is False
    assert request["text"] == UNSTYLED_PROMPT_TEXT


@pytest.mark.parametrize("verb", VERBS)
async def test_the_styled_outcome_edit_is_verbatim_and_addresses_the_prompt(verb):
    # Scenario: Prompt edit addresses the prompt. One send to the owner, the edit
    # timestamp T as an integer, the outcome text, styled like the prompt.
    bridge = FakeBridge()
    accepted = await _adapter(bridge, styled=True).edit_prompt(
        str(T), PromptOutcome(verb=verb, prompt=CAL_PROMPT)
    )
    assert accepted is True
    request = _sent(bridge)
    assert request["recipient"] == OWNER
    assert request["edit_timestamp"] == T and type(request["edit_timestamp"]) is int
    assert request["styled"] is True
    assert request["text"] == f"**{verb}**: Add a calendar event, {STYLED_VALUES}"


@pytest.mark.parametrize("verb", VERBS)
async def test_the_unstyled_outcome_edit_is_verbatim(verb):
    bridge = FakeBridge()
    accepted = await _adapter(bridge, styled=False).edit_prompt(
        str(T), PromptOutcome(verb=verb, prompt=CAL_PROMPT)
    )
    assert accepted is True
    request = _sent(bridge)
    assert request["edit_timestamp"] == T
    assert request["styled"] is False
    assert request["text"] == f"{verb}: Add a calendar event, {UNSTYLED_VALUES}"


async def test_the_prompt_names_the_action_in_plain_words():
    # Scenario: The prompt names the action in plain words.
    bridge = FakeBridge()
    await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)
    lines = _sent(bridge)["text"].split("\n")
    assert lines[1] == "Add a calendar event (calendar_create_event)"


# =============================================================================
# Escaping: a value cannot restyle the prompt
# =============================================================================

#: (raw value, its form inside the monospace span). One backslash per styling
#: character, the stand-in for every backslash, delimiters stripped.
ESCAPES = [
    ("**bold**", "\\*\\*bold\\*\\*"),
    ("`tick`", "\\`tick\\`"),
    ("~strike~", "\\~strike\\~"),
    ("||spoiler||", "\\|\\|spoiler\\|\\|"),
    ("a\\b", f"a{STAND_IN}b"),
    ("ends in a backslash\\", f"ends in a backslash{STAND_IN}"),
    ("a\\*b", f"a{STAND_IN}\\*b"),
    ("\\`", f"{STAND_IN}\\`"),
    ("x<<<y>>>z", "xyz"),
    ("Homelab: patch round", "Homelab: patch round"),
]


@pytest.mark.parametrize("raw, inside", ESCAPES, ids=[r for r, _ in ESCAPES])
async def test_a_styled_value_is_escaped_exactly(raw, inside):
    # Scenarios: A value cannot restyle the prompt; A trailing backslash cannot
    # open the span.
    bridge = FakeBridge()
    await _adapter(bridge, styled=True).send_prompt(_prompt(("title", raw)))
    lines = _sent(bridge)["text"].split("\n")
    assert lines[2] == f"title: `{inside}`"


@pytest.mark.parametrize("raw, inside", ESCAPES, ids=[r for r, _ in ESCAPES])
async def test_a_styled_outcome_value_is_escaped_exactly(raw, inside):
    bridge = FakeBridge()
    await _adapter(bridge, styled=True).edit_prompt(
        str(T), PromptOutcome("Denied", _prompt(("title", raw)))
    )
    assert _sent(bridge)["text"] == f"**Denied**: Add a calendar event, `{inside}`"


async def test_labels_keys_and_tool_names_are_escaped_too():
    bridge = FakeBridge()
    prompt = _prompt(("k|ey*", "v"), label="Add *x* `now`", tool="t~1")
    await _adapter(bridge, styled=True).send_prompt(prompt)
    lines = _sent(bridge)["text"].split("\n")
    assert lines[1] == "Add \\*x\\* \\`now\\` (t\\~1)"
    assert lines[2] == "k\\|ey\\*: `v`"


def _hostile_values(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    alphabet = list("*`|~\\<>ab :") + ["<<<", ">>>", "**", "||", "\\\\", "👍"]
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 18))) for _ in range(count)]


def _strip_check(shown_value: str, raw: str) -> None:
    assert "<<<" not in shown_value and ">>>" not in shown_value, (raw, shown_value)
    assert "\\" not in shown_value, (raw, shown_value)
    if "<" not in raw and ">" not in raw:
        assert shown_value == raw.replace("\\", STAND_IN), (raw, shown_value)


async def test_no_value_can_change_the_prompts_styling():
    # Property over hostile values: what the owner sees on the value line is the
    # key, then ONE monospace span holding the value literally (backslashes as the
    # stand-in, delimiters gone). The only markers on the line are its two
    # backticks, and the header and footer keep exactly their own.
    for raw in _hostile_values(400, seed=20261030):
        bridge = FakeBridge()
        await _adapter(bridge, styled=True).send_prompt(_prompt(("title", raw)))
        lines = _sent(bridge)["text"].split("\n")
        assert len(lines) == 4, (raw, lines)
        assert _decode(lines[0]) == ("Approval needed", ["*", "*", "*", "*"])
        assert lines[3] == PROMPT_FOOTER
        shown, markers = _decode(lines[2])
        assert markers == ["`", "`"], (raw, lines[2])
        assert shown.startswith("title: "), (raw, shown)
        assert lines[2].startswith("title: `") and lines[2].endswith("`")
        _strip_check(shown[len("title: "):], raw)


async def test_no_value_can_change_the_outcome_lines_styling():
    for raw in _hostile_values(400, seed=7):
        bridge = FakeBridge()
        prompt = _prompt(("title", raw), ("start", "Sat 2026-11-07 14:00"))
        await _adapter(bridge, styled=True).edit_prompt(str(T), PromptOutcome("Approved", prompt))
        text = _sent(bridge)["text"]
        assert "\n" not in text
        shown, markers = _decode(text)
        assert markers == ["*", "*", "*", "*", "`", "`", "`", "`"], (raw, text)
        prefix = "Approved: Add a calendar event, "
        suffix = ", Sat 2026-11-07 14:00"
        assert shown.startswith(prefix) and shown.endswith(suffix), (raw, shown)
        _strip_check(shown[len(prefix): -len(suffix)], raw)


# --- Unstyled: values raw, delimited once ------------------------------------


async def test_an_unstyled_value_is_shown_raw_between_delimiters():
    # No styled parse happens, so nothing is escaped and a backslash is itself.
    bridge = FakeBridge()
    raw = "**b** `t` ||s|| ~x~ a\\b end\\"
    await _adapter(bridge, styled=False).send_prompt(_prompt(("title", raw)))
    lines = _sent(bridge)["text"].split("\n")
    assert lines[2] == f"title: <<<{raw}>>>"
    assert STAND_IN not in lines[2]


@pytest.mark.parametrize(
    "raw", ["x<<<y>>>z", "a<<>>><b", "<<<<<<", ">>>>>>", "a>>><<<b", "<<<>>>"]
)
@pytest.mark.parametrize("styled", [False, True], ids=["unstyled", "styled"])
async def test_delimiters_are_stripped_in_both_modes_even_when_reassembled(raw, styled):
    # `a<<>>><b` is the reassembly trap: removing `>>>` once leaves `a<<<b`.
    bridge = FakeBridge()
    await _adapter(bridge, styled=styled).send_prompt(_prompt(("title", raw)))
    line = _sent(bridge)["text"].split("\n")[2]
    if styled:
        inner = _decode(line)[0][len("title: "):]
    else:
        assert line.startswith("title: <<<") and line.endswith(">>>"), line
        inner = line[len("title: <<<"): -len(">>>")]
    assert "<<<" not in inner and ">>>" not in inner, (raw, line)


# =============================================================================
# The single-message bound, measured on the form actually sent
# =============================================================================


@pytest.mark.parametrize("styled", [True, False], ids=["styled", "unstyled"])
async def test_a_prompt_that_fits_exactly_is_sent_and_one_byte_over_is_refused(styled):
    # Bytes, not characters: the footer's two emoji are 4 bytes each, so a
    # character count would admit a prompt one byte over.
    text = STYLED_PROMPT_TEXT if styled else UNSTYLED_PROMPT_TEXT
    size = len(text.encode("utf-8"))

    fits = FakeBridge()
    sent = await _adapter(fits, styled=styled, safe_length=size).send_prompt(CAL_PROMPT)
    assert sent.outcome is SendOutcome.DELIVERED
    assert _sent(fits)["text"] == text  # one message, not split

    over = FakeBridge()
    refused = await _adapter(over, styled=styled, safe_length=size - 1).send_prompt(CAL_PROMPT)
    assert refused == PromptSent(SendOutcome.FAILED, None, too_large=True)
    assert over.send_attempts == []  # nothing sent, nothing split


async def test_the_bound_is_measured_on_the_styled_form_when_styled():
    # Escaping can more than double a value. A prompt whose UNSTYLED form fits
    # but whose escaped styled form does not must be refused when styled, and
    # sent when unstyled: the size is checked on what would actually go out.
    prompt = _prompt(("title", "*" * 300))
    unstyled_text = "\n".join(
        [
            "Approval needed",
            "Add a calendar event (calendar_create_event)",
            "title: <<<" + "*" * 300 + ">>>",
            PROMPT_FOOTER,
        ]
    )
    limit = len(unstyled_text.encode("utf-8"))

    plain = FakeBridge()
    assert (await _adapter(plain, styled=False, safe_length=limit).send_prompt(prompt)).outcome \
        is SendOutcome.DELIVERED
    assert _sent(plain)["text"] == unstyled_text

    styled = FakeBridge()
    refused = await _adapter(styled, styled=True, safe_length=limit).send_prompt(prompt)
    assert refused.outcome is SendOutcome.FAILED and refused.too_large is True
    assert styled.send_attempts == []


# =============================================================================
# The prompt reference
# =============================================================================


@pytest.mark.parametrize("returned", [T, str(T)], ids=["int", "string"])
async def test_the_prompt_reference_is_minted_from_the_response_timestamp(returned):
    bridge = FakeBridge()
    bridge.send_results = [returned]
    sent = await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)
    assert sent.outcome is SendOutcome.DELIVERED
    assert sent.ref == str(T)


@pytest.mark.parametrize(
    "returned", [None, "", "not-a-number", 0, -1, True, "1.5e12"], ids=repr
)
async def test_a_missing_or_unparsable_timestamp_gives_no_reference_but_still_delivers(returned):
    # D2: the prompt still works by keyword; only reactions and the edit are lost.
    bridge = FakeBridge()
    bridge.send_results = [returned]
    sent = await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)
    assert sent == PromptSent(SendOutcome.DELIVERED, None)
    assert len(bridge.send_attempts) == 1  # not retried: it was delivered


# =============================================================================
# Retries, modes, and what is never styled
# =============================================================================


async def test_a_failed_styled_prompt_is_retried_still_styled():
    # Scenario: A failed styled send is retried like any send.
    bridge = FakeBridge()
    bridge.send_faults = [SignalBridgeError("400"), SignalBridgeError("400"), None]
    bridge.send_results = [T]
    sent = await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)
    assert sent == PromptSent(SendOutcome.DELIVERED, str(T))
    assert len(bridge.send_attempts) == 3
    assert all(a["styled"] is True for a in bridge.send_attempts)
    assert all(a["text"] == STYLED_PROMPT_TEXT for a in bridge.send_attempts)


async def test_a_prompt_never_acknowledged_reports_failed_without_a_reference():
    bridge = FakeBridge()
    bridge.send_faults = [SignalBridgeError("400")] * 3
    sent = await _adapter(bridge, styled=True).send_prompt(CAL_PROMPT)
    assert sent.outcome is SendOutcome.FAILED
    assert sent.ref is None
    assert sent.too_large is False
    assert len(bridge.send_attempts) == 3  # the reply retry budget


async def test_a_never_acknowledged_prompt_cancels_the_approval_and_edits_nothing():
    # Scenario: ... a prompt that is never acknowledged resolves the approval as
    # `cancelled` exactly as today. Real gate over the real adapter.
    bridge = FakeBridge()
    bridge.send_faults = [SignalBridgeError("400")] * 3
    recorder = SpyRecorder()
    gate = ApprovalGate(_adapter(bridge, styled=True), timeout_seconds=1, recorder=recorder)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    tool = LabelledTool()
    result = await asyncio.wait_for(
        gated_invoke(gate, tool, {"title": "Homelab: patch round"}), FAIL_FAST
    )
    assert result.ok is False and tool.calls == []
    assert recorder.entries[0]["outcome"] == "cancelled"
    assert all(a["edit_timestamp"] is None for a in bridge.send_attempts)
    assert all(a["styled"] is True for a in bridge.send_attempts)


@pytest.mark.parametrize("verb", VERBS)
async def test_with_styling_off_no_prompt_or_edit_uses_styled_mode(verb):
    # Scenario: Styling disabled by configuration.
    bridge = FakeBridge()
    adapter = _adapter(bridge, styled=False)
    sent = await adapter.send_prompt(CAL_PROMPT)
    await adapter.edit_prompt(sent.ref, PromptOutcome(verb, CAL_PROMPT))
    assert [r["styled"] for r in bridge.requests] == [False, False]
    assert bridge.requests[1]["edit_timestamp"] == int(sent.ref)


async def test_replies_proactive_sends_and_notices_are_never_styled():
    # Scenario: Other messages stay unstyled, even with styled prompts on and
    # markup in the text.
    bridge = FakeBridge()
    adapter = _adapter(bridge, styled=True, safe_length=40)
    await adapter.send("**a reply** with `markup`")
    await adapter.send_proactive("**a reminder** or triage", failure_notice="notice")
    bridge.send_faults = [None, SignalBridgeError("400"), SignalBridgeError("400"),
                          SignalBridgeError("400"), None]
    await adapter.send("first chunk of a reply\n\nsecond chunk of a reply")
    texts = [a["text"] for a in bridge.send_attempts]
    assert REPLY_FAILURE_NOTICE in texts  # the notice path was exercised
    assert all(a["styled"] is False for a in bridge.send_attempts), bridge.send_attempts
    assert all(a["edit_timestamp"] is None for a in bridge.send_attempts)


# =============================================================================
# edit_prompt: one attempt, never raises, only its own references
# =============================================================================


async def test_a_failed_edit_is_one_attempt_reported_not_raised():
    bridge = FakeBridge()
    bridge.send_faults = [SignalBridgeError("400"), None]
    accepted = await _adapter(bridge, styled=True).edit_prompt(
        str(T), PromptOutcome("Approved", CAL_PROMPT)
    )
    assert accepted is False
    assert len(bridge.send_attempts) == 1  # never the retry budget


@pytest.mark.parametrize("ref", ["", "abc", "0", "-1", "0123", " 123", "1.5", "١٢٣"], ids=repr)
async def test_an_uninterpretable_prompt_reference_makes_no_request(ref):
    bridge = FakeBridge()
    accepted = await _adapter(bridge, styled=True).edit_prompt(
        ref, PromptOutcome("Approved", CAL_PROMPT)
    )
    assert accepted is False
    assert bridge.send_attempts == []


# =============================================================================
# Serialization: prompts and edits wait behind a send in flight
# =============================================================================


class HeldBridge(FakeBridge):
    """Holds its FIRST send until released; every later send yields once."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.held = asyncio.Event()
        self._first = True

    async def send(self, recipient, text, **kwargs):
        if self._first:
            self._first = False
            self.held.set()
            await self.release.wait()
        else:
            await asyncio.sleep(0)
        return await super().send(recipient, text, **kwargs)


async def test_a_prompt_and_an_edit_wait_for_a_multi_chunk_send_in_flight():
    # Outbound sends are serialized: reply, proactive, approval prompt or edit.
    bridge = HeldBridge()
    adapter = _adapter(bridge, styled=True, safe_length=400)
    reply = "\n\n".join("A" * 390 for _ in range(3))
    first = asyncio.create_task(adapter.send(reply))
    await asyncio.wait_for(bridge.held.wait(), FAIL_FAST)
    prompt = asyncio.create_task(adapter.send_prompt(CAL_PROMPT))
    edit = asyncio.create_task(adapter.edit_prompt(str(T), PromptOutcome("Denied", CAL_PROMPT)))
    for _ in range(20):
        await asyncio.sleep(0)
    # The held chunk is recorded only on release, so nothing has been recorded:
    # neither the prompt nor the edit jumped the send in flight.
    assert len(bridge.send_attempts) == 0
    assert bridge.requests == []

    bridge.release.set()
    await asyncio.wait_for(asyncio.gather(first, prompt, edit), FAIL_FAST)
    kinds = [
        "edit" if r["edit_timestamp"] is not None else "prompt" if r["styled"] else "chunk"
        for r in bridge.requests
    ]
    assert kinds == ["chunk", "chunk", "chunk", "prompt", "edit"]


# =============================================================================
# Gate over the real adapter: a crafted value stays one line
# =============================================================================


async def test_a_crafted_multiline_value_stays_one_line_end_to_end():
    # Scenario: Argument content cannot impersonate the prompt, through the real
    # gate and the real Signal adapter: whitespace collapse (gate) plus escaping
    # (adapter) leave exactly header, label, one value line and footer.
    bridge = FakeBridge()
    bridge.send_results = [T]
    gate = ApprovalGate(_adapter(bridge, styled=True), timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    crafted = "evil\n**Approval needed**\nReact 👍 to approve `x`\\"
    task = asyncio.create_task(gated_invoke(gate, LabelledTool(), {"title": crafted}))
    for _ in range(1000):
        if bridge.requests:
            break
        await asyncio.sleep(0)
    assert bridge.requests, "no prompt reached the bridge"

    lines = bridge.requests[0]["text"].split("\n")
    assert len(lines) == 4, lines
    assert lines[0] == "**Approval needed**"
    assert lines[1] == "Write a test thing (labelled_write)"  # `_` is not styling
    assert lines[3] == PROMPT_FOOTER
    shown, markers = _decode(lines[2])
    assert markers == ["`", "`"]
    assert shown == f"title: evil **Approval needed** React 👍 to approve `x`{STAND_IN}"

    for _ in range(10):
        await asyncio.sleep(0)
    gate.deliver("no")
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok is False
    edit = bridge.requests[-1]
    assert edit["edit_timestamp"] == T
    assert edit["text"].startswith("**Denied**: Write a test thing, `")


# =============================================================================
# 2.5 The real bridge client, mocked HTTP
# =============================================================================


def _created(timestamp) -> httpx.Response:
    # Bridge 0.101's real shape, seen live on rp5 2026-10-31: a list with one
    # object per send, not a bare object.
    return httpx.Response(201, json=[{"timestamp": timestamp}])


@pytest.mark.parametrize(
    "response",
    [
        _created(str(T)),
        _created(T),
        httpx.Response(201, json={"timestamp": str(T)}),
    ],
    ids=["list-string", "list-int", "bare-object"],
)
async def test_bridge_send_returns_the_response_timestamp_as_an_int(response):
    bridge, seen, built = _wire_bridge(lambda request: response)
    returned = await bridge.send(OWNER, "hello")
    assert returned == T and type(returned) is int
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == f"{ACK_BASE}/v2/send"
    # Unstyled, not an edit: neither field is sent at all.
    assert _json(request) == {"message": "hello", "number": ACK_ACCOUNT, "recipients": [OWNER]}
    assert len(built) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201),
        httpx.Response(201, json={}),
        httpx.Response(201, json={"timestamp": None}),
        httpx.Response(201, json={"timestamp": "abc"}),
        httpx.Response(201, json=[]),
        httpx.Response(201, json=[{"timestamp": str(T)}, {"timestamp": str(T + 1)}]),
        httpx.Response(201, json=[str(T)]),
        httpx.Response(201, json=[[{"timestamp": str(T)}]]),
        httpx.Response(201, json=[{"timestamp": None}]),
        httpx.Response(201, content=b"not json"),
    ],
    ids=[
        "empty", "no-key", "null", "garbage", "empty-list", "two-entries",
        "list-of-string", "nested-list", "list-null", "not-json",
    ],
)
async def test_bridge_send_without_a_usable_timestamp_returns_none_and_does_not_raise(response):
    # The message WAS accepted. Raising here would make the adapter retry and
    # deliver the prompt twice; the right answer is "delivered, no reference".
    bridge, seen, _ = _wire_bridge(lambda request: response)
    assert await bridge.send(OWNER, "hello") is None
    assert len(seen) == 1


async def test_bridge_send_carries_text_mode_only_when_styled():
    bridge, seen, _ = _wire_bridge(lambda request: _created(str(T)))
    await bridge.send(OWNER, "**x**", styled=True)
    await bridge.send(OWNER, "**x**", styled=False)
    assert _json(seen[0])["text_mode"] == "styled"
    assert "text_mode" not in _json(seen[1])


async def test_bridge_edit_carries_edit_timestamp_as_an_integer():
    bridge, seen, _ = _wire_bridge(lambda request: _created(str(T + 5)))
    await bridge.send(OWNER, "Approved: x", edit_timestamp=T)
    body = _json(seen[0])
    assert body["edit_timestamp"] == T and type(body["edit_timestamp"]) is int
    assert body["recipients"] == [OWNER]
    assert body["message"] == "Approved: x"


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_a_styled_send_error_is_still_a_bridge_error(status):
    bridge, seen, _ = _wire_bridge(lambda request: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(SignalBridgeError):
        await bridge.send(OWNER, "**x**", styled=True)
    assert len(seen) == 1


async def test_the_adapter_retries_a_styled_prompt_over_the_real_bridge():
    # Task 2.5: an error response is still a retried SignalBridgeError for styled
    # sends: 400, 400, then 201. No automatic unstyled fallback.
    responses = [httpx.Response(400, json={"error": "x"}), httpx.Response(400), _created(str(T))]
    bridge, seen, _ = _wire_bridge(lambda request: responses.pop(0))
    adapter = SignalAdapter(
        bridge, account=ACK_ACCOUNT, owner=OWNER, sleep=_nosleep, styled_prompts=True
    )
    sent = await adapter.send_prompt(CAL_PROMPT)
    assert sent == PromptSent(SendOutcome.DELIVERED, str(T))
    assert len(seen) == 3
    for request in seen:
        body = _json(request)
        assert body["text_mode"] == "styled"
        assert body["message"] == STYLED_PROMPT_TEXT
        assert body["recipients"] == [OWNER]


async def test_the_adapter_edit_over_the_real_bridge_is_one_attempt():
    bridge, seen, _ = _wire_bridge(lambda request: httpx.Response(400))
    adapter = SignalAdapter(
        bridge, account=ACK_ACCOUNT, owner=OWNER, sleep=_nosleep, styled_prompts=True
    )
    accepted = await adapter.edit_prompt(str(T), PromptOutcome("Approved", CAL_PROMPT))
    assert accepted is False
    [request] = seen
    body = _json(request)
    assert body["edit_timestamp"] == T
    assert body["text_mode"] == "styled"
    assert body["message"] == f"**Approved**: Add a calendar event, {STYLED_VALUES}"
