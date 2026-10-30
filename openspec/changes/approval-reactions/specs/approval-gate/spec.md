## MODIFIED Requirements

### Requirement: Inline approval over the channel
When the agent attempts to invoke a mutating tool whose authorization tier is `per-instance`, the gate SHALL suspend the invocation and send the owner an approval prompt over the channel adapter stating the resolved action: the tool's owner-facing label, its tool name, and each argument, with every model-chosen argument value whitespace-collapsed (so it occupies exactly one line), truncated to a bounded length, and handed to the adapter as a separate field — never interpolated raw into prompt text — so that the adapter renders it visibly bounded and argument content cannot impersonate prompt structure or instructions. Every argument key SHALL be one of the tool's declared parameter names; an invocation carrying any undeclared key SHALL resolve as `cancelled` without a prompt, so a model-chosen key can never reach the prompt. Each cancel the gate causes itself SHALL carry its own model-facing reason and receipt `detail`, never the unrelated-message reason: an undeclared key returns that the named argument (whitespace-collapsed and bounded before it is echoed) is not a parameter of the tool and that nothing was asked or executed, with detail `undeclared argument`; an oversized prompt returns that the action is too large to show in one approval prompt and nothing was executed, with detail `prompt too large`. Authorization SHALL never be derived from argument content. The prompt SHALL never be split across messages; a prompt that cannot be sent as one message SHALL resolve as `cancelled` without being split. The owner responds in either of two ways: (a) a typed approval keyword (`yes` / `approve`) or denial keyword (`no` / `deny`), matched exactly and case-insensitively; or (b) a reaction on the pending prompt message itself, where 👍 approves and 👎 denies, in any skin tone. A reaction SHALL count only when it is from the owner, targets the prompt message of the approval that is pending at that moment, and is not a reaction removal; every other reaction (another emoji, another target message, a removal, or any reaction while no approval is pending) SHALL be ignored without resolving, cancelling, or otherwise affecting the pending approval, and SHALL never become an agent turn. Internally, the gate SHALL bind the pending approval to that single invocation and its arguments via a one-time reference; the owner never types the reference. At most one approval SHALL be pending per conversation at any time — an invariant the gate itself enforces (see "Gate concurrency is fail-closed"), not one derived from turn serialization, since a single assistant message may carry multiple tool invocations.

#### Scenario: Owner approves
- **WHEN** the gate sends an approval prompt and the owner replies with an approval keyword
- **THEN** the tool executes exactly once with the arguments shown in the prompt, and the result flows back into the agent turn

#### Scenario: Owner approves by reaction
- **WHEN** the gate sends an approval prompt and the owner reacts 👍 (with or without a skin-tone modifier) on that prompt message
- **THEN** the tool executes exactly once with the arguments shown in the prompt, and the result flows back into the agent turn

#### Scenario: Owner denies
- **WHEN** the owner replies with a denial keyword
- **THEN** the invocation is cancelled, the agent turn continues with a "denied by owner" tool result, and the tool is not executed

#### Scenario: Owner denies by reaction
- **WHEN** the owner reacts 👎 on the pending prompt message
- **THEN** the invocation is not executed, the agent turn continues with a "denied by owner" tool result, and the receipt records outcome `denied`

#### Scenario: Other reactions are ignored
- **WHEN** an approval is pending and the owner reacts with any emoji other than 👍 or 👎 on the prompt, or reacts 👍 or 👎 on any other message (an earlier prompt included), or removes a reaction
- **THEN** the approval stays pending and unresolved, nothing is executed, no turn is queued, and a later keyword or valid reaction still resolves it

#### Scenario: A reaction with nothing pending does nothing
- **WHEN** the owner reacts 👍 on a prompt whose approval has already been decided, timed out, or was never pending
- **THEN** no tool executes, no approval resolves, and no agent turn is queued

#### Scenario: Taking back a reaction after execution
- **WHEN** the owner removes a 👍 reaction after the approved action has executed
- **THEN** nothing happens: the action is not reversed, no message is sent, and no turn is queued

#### Scenario: Reactions from anyone but the owner
- **WHEN** a non-owner identity reacts 👍 on the pending prompt message
- **THEN** the reaction is dropped by the owner-only allowlist and the approval stays pending

#### Scenario: Unrelated message during pending approval
- **WHEN** the owner sends a message that matches no approval or denial keyword while a gate is pending (e.g., "what's on my board?")
- **THEN** the gate resolves as cancelled (fail closed), the suspended turn resumes with a "cancelled" result and its reply states that the pending action was cancelled, and the owner's message is then processed as a normal new turn in order — it is not swallowed

#### Scenario: Single pending approval per conversation
- **WHEN** an approval prompt is pending in a conversation
- **THEN** no second approval prompt can become outstanding in that conversation until the first is resolved

#### Scenario: Approval is single-use and argument-bound
- **WHEN** an invocation has been approved and executed
- **THEN** that approval cannot authorize any further invocation, including an identical one, and a second 👍 on the same prompt authorizes nothing; a new invocation requires a new prompt

#### Scenario: Argument content cannot impersonate the prompt
- **WHEN** a per-instance tool is invoked with an argument value crafted to resemble approval-prompt text, instructions, delimiters or styling markup, or containing line breaks
- **THEN** the value reaches the adapter as one bounded single-line field inside the argument section, it does not alter the prompt's structure, its styling or the keyword and reaction matching

#### Scenario: An undeclared argument key fails closed
- **WHEN** a per-instance tool is invoked with an argument key that is not one of its declared parameters (for example a key containing a line break)
- **THEN** no prompt is sent, the invocation resolves as `cancelled` without executing, the tool result names the undeclared argument and says nothing was asked or executed (not that an unrelated message arrived), and its receipt records outcome `cancelled` with detail `undeclared argument`

#### Scenario: A prompt too large for one message fails closed
- **WHEN** the rendered prompt for an invocation would not fit in a single channel message
- **THEN** no prompt is sent, the invocation resolves as `cancelled` without executing, the tool result says the action is too large to show in one approval prompt, and its receipt records outcome `cancelled` with detail `prompt too large`

### Requirement: Gate paths are covered by automated tests
The approve, deny, cancel-by-unrelated-message, timeout, standing-without-prompt, kill-switch-demotion, turn-scope/taint-denial, concurrency (both standing-concurrent and rejected-busy), and suppressed-turn-denial paths SHALL each be covered by automated tests driven through a channel-adapter test double, as SHALL the reaction paths (approve and deny by reaction, every ignored-reaction case, reaction with nothing pending), the outcome edit for each decided outcome, the release of the pending slot before the edit, the undeclared-key refusal, and the single-message bound.

#### Scenario: Gate test coverage
- **WHEN** the test suite runs
- **THEN** every path listed above passes

### Requirement: Standing tier can be demoted by configuration, never widened
A configuration flag SHALL demote all standing-tier tools to per-instance approval (kill-switch), defaulting to off. No configuration SHALL promote a per-instance tool to standing, widen a tool's turn scope, or register a new mutating tool — authorization widens only through code review.

#### Scenario: Kill-switch demotes standing tools
- **WHEN** the demotion flag is enabled and the agent invokes a standing-tier tool
- **THEN** the gate sends a per-instance approval prompt and the tool executes only on an owner approval (keyword or reaction)

### Requirement: Reminder tools carry a declared tier and scope
`remind` and `cancel_reminder` SHALL both be registered as mutating, authorization tier **standing**, turn scope **owner-only** — the same containment argument as `capture` and `store_memory`: a write into a Henk-local store whose only external effect is a message to the configured owner, receipted every time, and in the cancellation case reversible by an owner command with the row and its text retained. They SHALL therefore execute without an approval prompt in an untainted owner session, and SHALL be denied with outcome `out-of-scope` during any event turn and during any turn of a tainted session. `reminders_read` SHALL be registered read-only and SHALL bypass the gate. The standing-tier kill switch SHALL apply to both mutating reminder tools unchanged: with demotion enabled, scheduling or cancelling a reminder requires inline approval.

#### Scenario: Scheduling executes without a prompt
- **WHEN** the agent invokes `remind` in an untainted owner session
- **THEN** the reminder is stored, no approval prompt is sent, and the authorization is reported for the audit record with tier `standing` and outcome `authorized`

#### Scenario: Untrusted event input cannot schedule a message
- **WHEN** an event payload instructs Henk to set a reminder and the event turn is processed
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and no reminder is stored

#### Scenario: Untrusted event input cannot cancel a reminder
- **WHEN** an event payload instructs Henk to cancel a reminder and the event turn is processed
- **THEN** the invocation is denied with outcome `out-of-scope` and no reminder changes status

#### Scenario: Tainted session cannot schedule
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent then invokes `remind`
- **THEN** the invocation is denied with outcome `out-of-scope` and the tool result names the incident taint and the `/remind` command as the remedy

#### Scenario: Kill-switch demotes both reminder tools
- **WHEN** the demotion flag is enabled and the agent invokes `remind` or `cancel_reminder`
- **THEN** an approval prompt is sent and the tool's effect occurs only on an owner approval (keyword or reaction)

### Requirement: Scheduled delivery is app-initiated and outside the gate
Delivery of a due reminder SHALL NOT pass through the approval gate. Its authority was granted when the reminder was scheduled — by the owner directly through a command, or by a gate-authorized standing-tier invocation in an untainted owner turn whose confirmation echoed the resolved due time. The gate governs model-initiated invocations only; the scheduler, like an owner command, is not one. The scheduler SHALL therefore send without occupying or consulting the pending-approval slot, a pending approval SHALL be unaffected by a delivery, and a delivered reminder SHALL NOT be classifiable as an approval prompt (it carries the reminder marker, and the gate classifies only inbound owner text and owner reactions on the pending prompt message, never Henk's own outbound messages). Accountability for every delivery comes from its `reminder` audit record (audit-log spec), not from an approval.

#### Scenario: Delivery does not prompt
- **WHEN** a reminder comes due
- **THEN** it is delivered with no approval prompt, and no approval record is created for the send

#### Scenario: Delivery leaves a pending approval intact
- **WHEN** a reminder is delivered while an approval prompt is pending
- **THEN** the pending approval is unchanged and still resolvable by the owner's next approval or denial keyword or reaction on the prompt

#### Scenario: Every delivery is traceable to a schedule
- **WHEN** any delivered reminder's audit trail is inspected
- **THEN** it contains both the `scheduled` record naming who scheduled it and the delivery record naming the scheduler

### Requirement: Calendar tools carry a declared tier and scope
`calendar_create_event` SHALL be registered as mutating, authorization tier **per-instance**, turn scope **owner-only**, with the owner-facing label `Add a calendar event`: unlike the standing writes into Henk-local stores, it changes a shared external calendar that another agent also reads, so every write is shown to the owner first. It SHALL therefore send an approval prompt in the gate's prompt format, rendering `title`, `start` and `end` as bounded argument values, and SHALL execute only on an owner approval (keyword or reaction); it SHALL be denied with outcome `out-of-scope`, without a prompt, during any event turn and during any turn of a tainted session. `calendar_upcoming` SHALL be registered read-only and SHALL bypass the gate. The standing-tier kill switch has no effect on either tool.

#### Scenario: Approval prompt shows the resolved event
- **WHEN** the agent invokes `calendar_create_event` with `title` `Homelab: patch round`, `start` `Sat 2026-11-07 20:00`, `end` `Sat 2026-11-07 22:00` in an untainted owner session
- **THEN** the gate hands the adapter one prompt with label `Add a calendar event`, tool name `calendar_create_event` and the fields `title` `Homelab: patch round`, `start` `Sat 2026-11-07 20:00` and `end` `Sat 2026-11-07 22:00` in that order, and no insert is made before an owner approval

#### Scenario: Approval receipted as per-instance
- **WHEN** the owner replies `yes` to that prompt, or reacts 👍 on it
- **THEN** the authorization record carries tool `calendar_create_event`, tier `per-instance` and outcome `approved`

#### Scenario: Tainted session cannot write the calendar
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent invokes `calendar_create_event`
- **THEN** the invocation is denied with outcome `out-of-scope`, no prompt is sent, and no insert is made

#### Scenario: Reading needs no approval
- **WHEN** the agent invokes `calendar_upcoming`
- **THEN** it executes without an approval prompt and without an authorization record

## ADDED Requirements

### Requirement: Mutating tools declare an owner-facing label
Every mutating tool SHALL declare a short owner-facing label in code alongside its tier (for example `Add a calendar event`), because any mutating tool can be made to prompt by the standing-tier kill-switch. The agent core SHALL refuse to register a mutating tool without a non-empty label. The label SHALL be fixed in code and SHALL never be derived from model output or argument content. Read-only and notify-only tools need no label.

#### Scenario: Mutating tool without a label rejected at startup
- **WHEN** a mutating tool without a label is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: The prompt names the action in plain words
- **WHEN** the gate prompts for `calendar_create_event`
- **THEN** the prompt states the label `Add a calendar event` followed by the tool name `calendar_create_event`

### Requirement: A decided prompt records its outcome
Once a pending approval is decided — `approved`, `denied`, `cancelled` or `timeout` — and its prompt was sent with a channel reference, the gate SHALL ask the adapter to edit that prompt into a one-line outcome naming the decision (`Approved`, `Denied`, `Cancelled`, `Timed out`), the label, and the argument values in the order the prompt showed them. The pending-approval slot SHALL be released before the edit is awaited, so an owner message or reaction arriving during the edit is handled exactly as one arriving after the approval resolved. The edit SHALL be attempted after the decision and before the tool runs or the non-execution result returns, SHALL be a single best-effort attempt, and its failure SHALL be logged and SHALL NOT change the decision, the execution, or the receipt. Decisions that sent no prompt (`authorized`, `rejected-busy`, `suppressed`, `out-of-scope`, a prompt that could not be sent) SHALL edit nothing. The authorization receipt for an `approved` or `denied` outcome SHALL carry `detail` naming how the owner answered: `answered by reaction` or `answered by keyword`, recorded together with the decision itself: the first answer to arrive decides, and a later one changes neither the outcome nor the detail.

#### Scenario: Approval edits the prompt
- **WHEN** the owner approves a pending prompt, by keyword or reaction
- **THEN** the prompt message is edited to start with `Approved` followed by the label and the argument values, before the tool executes

#### Scenario: Each non-executing outcome edits the prompt
- **WHEN** a pending approval resolves as denied, cancelled by an unrelated message, or timed out
- **THEN** the prompt is edited to start with `Denied`, `Cancelled` or `Timed out` respectively, and the tool is not executed

#### Scenario: A failed edit changes nothing
- **WHEN** the owner approves and the adapter reports the edit failed
- **THEN** the tool still executes exactly once, the receipt records `approved`, and the failure is logged

#### Scenario: A message during the edit is not swallowed
- **WHEN** the owner approves and, while the edit is still in flight, sends `yes` or any other text
- **THEN** that message is processed as a normal new turn, and the approved action executes exactly once

#### Scenario: First answer wins
- **WHEN** a 👍 reaction and a typed `no` for the same pending prompt arrive back to back
- **THEN** the first to be routed decides the outcome and the receipt detail, and the second resolves nothing

#### Scenario: The receipt names how the owner answered
- **WHEN** the owner approves one prompt by reaction and denies another by keyword
- **THEN** the first receipt records outcome `approved` with detail `answered by reaction`, and the second records `denied` with detail `answered by keyword`
