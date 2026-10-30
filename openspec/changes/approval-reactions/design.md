## Context

The approval gate (`henk/gate/approval.py`) renders a per-instance prompt as one plain
string (`format_prompt`), sends it through the channel adapter's reply `send`, and waits
on a future that only a typed message can resolve: `Dispatcher.on_inbound` (`henk/app.py`)
hands every allowlisted message to `gate.deliver(text)` while an approval is pending.
Argument values are whitespace-collapsed, stripped of the delimiters, truncated to 200
characters, and wrapped in `<<<` `>>>` (design D7 of henk-v1). The prompt is never touched
again after it is sent.

The Signal adapter (`henk/channel/signal.py`) drops every envelope without a text body in
`_convert`, so reactions never reach Henk today. `SignalBridge.send` discards the
`/v2/send` response, so Henk does not know the timestamp of anything he sent, and a
Signal edit or a reaction target is named by exactly that timestamp.

`henk/channel/base.py` is channel-neutral by contract: nothing outside `signal.py` may
reference Signal types or the bridge's wire format, and contract tests assert it.

The bridge's published API (checked 2026-10-30, `src/docs/swagger.json` and
`src/api/api.go` on master): `SendMessageV2` carries `text_mode` (`normal` | `styled`) and
`edit_timestamp`; the response carries `timestamp`; styled syntax is `**bold**`,
`*italic*`, `~strike~`, `||spoiler||`, `` `monospace` ``, with a backslash prefix to escape
a formatting character; inbound `receive.Reaction` carries `emoji`, `isRemove`,
`targetAuthor`, `targetAuthorNumber`, `targetAuthorUuid`, `targetSentTimestamp`.

Only `calendar_create_event` is per-instance today. The four standing mutating tools
prompt only when `approval.demote_standing` (the kill-switch) is on.

## Goals / Non-Goals

**Goals:**
- One tap to approve or deny, bound to the exact prompt it answers.
- A prompt that reads like a message while every model-chosen value stays visibly
  bounded and unable to restyle or restructure the prompt.
- A durable on-phone record: the prompt becomes its own outcome line.
- No widening anywhere: owner-only, single pending approval, fail closed, single use.

**Non-Goals:**
- Styling any other message (triage, replies, reminders, notices).
- Deleting prompts, or reactions as answers to anything but a pending prompt.
- Surviving a restart: a pending approval dies with the process today and still does;
  its prompt then stays unedited.
- A Telegram or other adapter. The contract stays neutral so one can implement it.

## Decisions

### D1. The gate hands the adapter a structured prompt; the adapter renders it

New neutral types in `base.py`:

```python
@dataclass(frozen=True)
class ApprovalPrompt:
    label: str                         # code-fixed, from the tool
    tool_name: str                     # code-fixed
    fields: tuple[tuple[str, str], ...]  # (schema key, bounded value), in call order

@dataclass(frozen=True)
class PromptOutcome:
    verb: str                          # "Approved" | "Denied" | "Cancelled" | "Timed out"
    prompt: ApprovalPrompt
```

The gate keeps the trust work it does today: whitespace collapse and truncation of every
value (`ARGUMENT_MAX_CHARS`). The adapter owns everything about its own syntax: styled
markup, escaping, and the unstyled fallback with delimiters.

*Alternative rejected:* the gate renders a styled string. That puts Signal's markup
grammar in neutral code, which the contract tests forbid for good reason: a Telegram
adapter would inherit Signal escaping rules that are wrong for it.

### D2. `send_prompt` returns an opaque reference; `edit_prompt` uses it

```python
async def send_prompt(self, prompt: ApprovalPrompt) -> PromptSent  # (outcome, ref | None)
async def edit_prompt(self, ref: str, outcome: PromptOutcome) -> bool
```

`send_prompt` never splits the prompt, sends under the existing send lock (outbound
serialization now names prompts and edits), with the reply retry budget, and is
at-least-once like every send. The Signal reference is the `timestamp` from the `/v2/send` response,
parsed to an integer whatever JSON type it arrives in and minted as a decimal string by
the same function that mints a reaction's target reference, only in `signal.py` (the
same pattern as `channel_ref` for receipts, owner-acknowledgement D2). A missing or unparsable
timestamp yields `ref = None`: the prompt still works by keyword, reactions to it are
ignored, and it is not edited.

If the prompt in the form it would actually be sent (styled after escaping, or unstyled)
exceeds the adapter's safe length, `send_prompt` sends nothing and returns `FAILED`; the gate resolves `cancelled` exactly
as for any other failed prompt send. Splitting is rejected: a reaction lands on one chunk,
and an edit would rewrite one chunk of several.

`edit_prompt` is `/v2/send` with `edit_timestamp = ref`, one attempt, under the send
lock, never raising, rendered in the mode the prompt was actually sent in (the adapter
uses the configured mode, which cannot change while a prompt is pending because
configuration loads at start and a pending approval does not survive a restart). The gate releases the pending
slot first, then awaits the edit after the decision and before returning, so a message
that arrives during the edit is routed exactly as one arriving after the decision,
never classified against an approval that is already decided, so the
record is on the phone before the tool's result is; its failure is logged and changes
nothing. One attempt rather than the retry budget: a lost edit costs a cosmetic record,
a retried one costs owner latency on every approval.

*Alternative rejected:* fire-and-forget edit task. It would race the agent's reply for
the send lock, outlive the turn, and need its own shutdown path, for about one second
saved per approval.

### D3. Reactions are a separate neutral inbound type

```python
@dataclass(frozen=True)
class InboundReaction:
    sender: str
    emoji: str
    target_ref: str | None   # adapter-minted; None unless the target is Henk's own message
    removed: bool
    is_group: bool = False
```

`messages()` yields `InboundMessage | InboundReaction`. A separate type rather than an
`InboundMessage` with empty text: an empty-text message that slipped past a check would
reach `core.submit` as a turn, while a reaction reaching code that expects `.text`
fails loudly.

Signal: a `dataMessage.reaction` envelope becomes an `InboundReaction`. `target_ref` is
minted from `targetSentTimestamp` only when `targetAuthorNumber` equals the configured
Henk account; otherwise `None`. No loosening to "either field", for the same reason as
the sender match: which field the bridge fills is a deploy-verify item (task 1.2), and
an unverified match defaults to ignoring reactions, never to accepting them.

`AllowlistFilter.allows` accepts both types: the same group drop and the same exact
sender match. Reactions get no read receipt.

### D4. Classification lives in the gate

`ApprovalGate.deliver_reaction(reaction) -> Classification`, with a new member
`IGNORED`. In order:

1. no pending approval, or the pending prompt has no ref → `IGNORED`;
2. `removed` → `IGNORED` (a taken-back reaction never resolves or reverses anything);
3. `target_ref != pending.prompt_ref` → `IGNORED`;
4. emoji normalised by dropping skin-tone modifiers (U+1F3FB–U+1F3FF) and variation
   selector 16 (U+FE0F): 👍 (U+1F44D) → `APPROVE`, 👎 (U+1F44E) → `DENY`;
5. anything else → `IGNORED`.

`IGNORED` never touches the future: the approval stays pending, and the reaction never
becomes a turn. That differs from a typed unrelated message, which cancels, and is the
owner's decision (2026-10-30, question 1).

The pending approval records how it was resolved (`reaction` or `keyword`) for the
receipt's `detail` (D7), set in the same step as `future.set_result` and only when the
future is not yet done: first answer wins, for the outcome and the detail alike.

*Race:* the ref is stored when `send_prompt` returns. A reaction that arrives before
that (the phone showed the message before the bridge's HTTP response returned) sees no
ref and is ignored; the owner can react again or type. Accepted: it fails closed and is
a sub-second window. The same holds for a prompt sent with no ref at all (the bridge
reported no timestamp): the footer still advertises reactions, and a 👍 then does
nothing until the prompt times out. Task 1.1 establishes that the deployed bridge
returns the timestamp; buffering early reactions was considered and rejected as more
state for a window nobody can hit by hand.

### D5. Styled rendering, escaping, fallback (Signal adapter)

Styled prompt:

```
**Approval needed**
<label> (<tool_name>)
<key>: `<value>`
...
React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it.
```

Outcome line: `**<verb>**: <label>, `<v1>`, `<v2>`, ...` (values only, in call order).

Every value, key, label and tool name has the styling characters `* ~ | `` ` ``
escaped with one backslash before it is placed in markup (the parser's escape is a single
`\` character in the decoded string; "two backslashes" in the bridge doc is the JSON
source form). A backslash cannot be escaped: the parser swallows any backslash that
precedes a styling character, so `\\` before a backtick still escapes the backtick. Every
backslash in content is therefore replaced by the visible stand-in `⧵` (U+29F5), so a
value ending in a backslash can never escape its own closing backtick. The escape sequence is one constant; the exact
backslash count the bridge expects ("prefix it with two backslashes" is ambiguous between
the JSON source and the decoded string) is settled by the escaping unit tests plus the
live check in task 7.2, with a crafted value. Delimiters `<<<` `>>>` are still stripped
from values in both modes, so the fallback stays sound.

Fallback: configuration only. An automatic fallback on a refused styled send was
specified and then dropped after reading the deployed bridge (0.101, `notes/bridge-facts.md`):
its style parser cannot fail (an unclosed marker just disappears from the text), and the
`/v2/send` handler answers every signal-cli failure, network trouble included, with HTTP
400. A 400 therefore cannot single out styling, and treating it as a styling refusal
would stop retrying ordinary transient failures. Styled sends keep today's retry rules.
`signal.styled_prompts`
(default `false` until the live check in task 7.2 passes; task 7.4 flips the code
default to `true`) selects the styled form for prompts and edits alike.

What styling does not guarantee, stated so nobody relies on it: the bridge reports
acceptance, not rendering. Whitespace collapse (gate) is what stops a value forging a
line in either mode; monospace and escaping are what stop it blending into Henk's text.

### D6. Owner-facing labels are declared on the tool

`Tool.label: str`, required for every mutating tool at registration (startup fails
naming the tool), since the kill-switch can make any of them prompt. Read-only and
notify-only tools need none. Values per the proposal's decision 1.

### D7. Receipt detail, no schema change

On `approved` and `denied`, the gate passes `detail="answered by reaction"` or
`detail="answered by keyword"`. The v6 schema already allows a bounded free string in
`detail` for authorization records. Other outcomes keep the detail they carry today
(`out-of-scope` names a raised taint; the rest carry none).

### D8. Argument keys must be declared parameters

The gate puts argument keys into the prompt, and today it takes them from the model's
call as is (`format_prompt`); `calendar_create_event` rejects an unexpected key only in
`_run`, after approval. A key containing a line break could therefore add a line to
today's prompt. Before prompting, the gate checks every key against the tool's
`parameters["properties"]`; any undeclared key resolves `cancelled` with no prompt
sent, with its own model-facing reason naming the key (collapsed and bounded before it is
echoed) and receipt detail `undeclared argument`; the size-bound cancel likewise says the
action is too large to show and carries detail `prompt too large`. Neither reuses the
unrelated-message reason, which would have the model tell the owner about a message they
never sent. This closes an existing gap rather than one this change opens; it belongs here
because this change restates the anti-forgery guarantee.

## Risks / Trade-offs

- [Molly or a future Signal client draws styles or edits differently] → Unstyled
  form by config; edits degrade to a second message on clients without edit support
  (Signal's own behaviour); the owner checks rendering at deploy (task 7.2).
- [The bridge's escaping differs from its doc] → One constant, a live crafted-value
  check before enabling; until then `styled_prompts: false` is the safe default for the
  rollout (task 7.1).
- [A sequencing dependency] → The calendar archive (branch
  `chore/archive-maintenance-calendar`) adds the requirement *Calendar tools carry a
  declared tier and scope*, whose scenario quotes today's `<<< >>>` prompt; this change
  modifies it, so it must land after that archive merges (task 0.1).
- [`targetAuthorNumber` empty when Henk's account is UUID-addressed] → Reactions are
  ignored (fail closed) and keywords still work; task 1.2 checks a real envelope first.
- [An accidental 👍 approves] → Same exposure as an accidental `yes`, and narrower: it
  must land on the pending prompt within the timeout.
- [Edit adds ~1 s before the tool runs] → Accepted for a per-approval record; measured
  in task 7.3.
- [A retried prompt send delivers twice] → The ref is the acknowledged copy's; a
  reaction on the other copy is ignored, keywords still work.

## Migration Plan

1. Deploy with `signal.styled_prompts: false` (the code default at first): reactions and edits live, prompt in the
   old delimiter form with labels. Live-check reactions and edits.
2. Run the crafted-value styling check; flip to `true`; check rendering by eye.
3. Rollback: `styled_prompts: false` for rendering; the previous image tag for the rest.
   No stored data changes in either direction.

## Open Questions

- Exact `/v2/send` response shape on the deployed bridge (object or array) → task 1.1.
- Which `targetAuthor*` field the bridge fills for Henk's account → task 1.2.
- Backslash count for escaping → task 7.2.
