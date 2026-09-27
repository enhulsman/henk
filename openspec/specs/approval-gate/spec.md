# approval-gate Specification

## Purpose
Gates every mutating action Henk can take behind the North Star's two-axis permission model: a
named action's authorization tier (standing / per-instance / never-unregistered) declared in
code, and a turn scope enforced with session taint so untrusted event input can never drive an
out-of-scope mutation. Standing actions trade prompts for receipts — an agent that acts without
asking must be more accountable, not less. The gate is fail-closed in every ambiguous case and
is one of the transferable artifacts.
## Requirements
### Requirement: Tools declare their mutation class
Every registered tool SHALL carry an explicit classification: `read-only`, `notify-only`, or `mutating`. Every mutating tool SHALL additionally carry an explicit authorization tier — `standing` or `per-instance` — declared in code alongside the tool, so a tier grant rides code review and cannot be widened by configuration. The agent core SHALL refuse to register a tool without a classification, and SHALL refuse to register a mutating tool without an authorization tier. Mutating tool invocations SHALL always pass through the approval gate, which enforces the tier; read-only and notify-only invocations SHALL NOT require approval. The third tier, `never`, is the absence of registration: unregistered actions remain denied by the closed-toolset boundary, and no registry entry expresses it.

#### Scenario: Unclassified tool rejected at startup
- **WHEN** a tool without a mutation classification is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: Mutating tool without a tier rejected at startup
- **WHEN** a mutating tool without an authorization tier is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: Read-only tool bypasses the gate
- **WHEN** the agent invokes a read-only tool
- **THEN** the tool executes without any approval prompt

### Requirement: Inline approval over the channel
When the agent attempts to invoke a mutating tool whose authorization tier is `per-instance`, the gate SHALL suspend the invocation and send the owner an approval prompt over the channel adapter stating the resolved action: the tool name and each argument, with every model-chosen argument value rendered inside explicit delimiters and truncated to a bounded length — never interpolated raw into the prompt text, so argument content cannot impersonate prompt instructions. Authorization SHALL never be derived from argument content. The owner responds with an approval keyword (`yes` / `approve`) or a denial keyword (`no` / `deny`), matched exactly and case-insensitively. Internally, the gate SHALL bind the pending approval to that single invocation and its arguments via a one-time reference; the owner never types the reference. At most one approval SHALL be pending per conversation at any time — an invariant the gate itself enforces (see "Gate concurrency is fail-closed"), not one derived from turn serialization, since a single assistant message may carry multiple tool invocations.

#### Scenario: Owner approves
- **WHEN** the gate sends an approval prompt and the owner replies with an approval keyword
- **THEN** the tool executes exactly once with the arguments shown in the prompt, and the result flows back into the agent turn

#### Scenario: Owner denies
- **WHEN** the owner replies with a denial keyword
- **THEN** the invocation is cancelled, the agent turn continues with a "denied by owner" tool result, and the tool is not executed

#### Scenario: Unrelated message during pending approval
- **WHEN** the owner sends a message that matches no approval or denial keyword while a gate is pending (e.g., "what's on my board?")
- **THEN** the gate resolves as cancelled (fail closed), the suspended turn resumes with a "cancelled" result and its reply states that the pending action was cancelled, and the owner's message is then processed as a normal new turn in order — it is not swallowed

#### Scenario: Single pending approval per conversation
- **WHEN** an approval prompt is pending in a conversation
- **THEN** no second approval prompt can become outstanding in that conversation until the first is resolved

#### Scenario: Approval is single-use and argument-bound
- **WHEN** an invocation has been approved and executed
- **THEN** that approval cannot authorize any further invocation, including an identical one; a new invocation requires a new prompt

#### Scenario: Argument content cannot impersonate the prompt
- **WHEN** a per-instance tool is invoked with an argument value crafted to resemble approval-prompt text or instructions
- **THEN** the prompt renders that value delimited and truncated inside the argument section, and it does not alter the prompt's structure or the keyword matching

### Requirement: Fail closed on timeout
A pending approval SHALL expire after a configured timeout (default 5 minutes). Expiry SHALL fail closed exactly as a denial does — the tool is not executed and the agent turn resumes with a "timed out, not executed" result — while remaining a distinct event: its receipt records the outcome `timeout`, not `denied`, so an owner who said no is never conflated with an owner who was away.

#### Scenario: Owner does not respond
- **WHEN** no owner response arrives within the timeout
- **THEN** the invocation is cancelled without executing, and the owner's next message is treated as a normal message, not a late approval

### Requirement: Standing-tier invocations execute without prompting
When the agent invokes a mutating tool whose authorization tier is `standing` (and whose turn scope permits it), the gate SHALL authorize the invocation without sending anything over the channel, and the tool SHALL execute. Every standing authorization SHALL be reported to the audit path with its tool name, tier, and outcome `authorized` — an agent that acts without asking is more accountable, not less. The standing path SHALL NOT consult or occupy the pending-approval slot. Standing authorization SHALL NOT bypass the registry: an unregistered tool remains denied regardless of any tier.

#### Scenario: Standing tool runs silently
- **WHEN** the agent invokes a standing-tier tool in an untainted owner session
- **THEN** the tool executes, no approval prompt or other channel message is sent, and the authorization is reported for the audit record

#### Scenario: Standing does not exist outside the registry
- **WHEN** the agent attempts an unregistered tool
- **THEN** the invocation is denied by the closed-toolset boundary exactly as before this change

### Requirement: Standing tier can be demoted by configuration, never widened
A configuration flag SHALL demote all standing-tier tools to per-instance approval (kill-switch), defaulting to off. No configuration SHALL promote a per-instance tool to standing, widen a tool's turn scope, or register a new mutating tool — authorization widens only through code review.

#### Scenario: Kill-switch demotes standing tools
- **WHEN** the demotion flag is enabled and the agent invokes a standing-tier tool
- **THEN** the gate sends a per-instance approval prompt and the tool executes only on an approval keyword

### Requirement: Mutating tools declare a turn scope, enforced per session
Every mutating tool SHALL declare the turn types in which it may execute (`owner`, `event`), defaulting to owner-only; the declaration lives in code alongside the tool, like its tier. The agent core SHALL supply the gate the current turn's context (turn type, announceability, whether the session is tainted, and the taint's source when a tool raised it), scoped strictly to the turn. A session becomes tainted when it processes an event turn, or when a taint-source tool is authorized in one of its turns (requirement *Tools may declare themselves taint sources*), and SHALL remain tainted for its lifetime. An invocation of a tool whose scope excludes event turns SHALL be denied — without any channel send, fail closed, outcome `out-of-scope` in its receipt — during any event turn AND during any turn of a tainted session, including the remainder of the turn in which a taint source raised the taint. The denial's tool result SHALL name the reason and the remedy: that the turn is an event turn, that the session is handling an incident, or that a tool earlier in the session returned content from outside the owner's control; and that the owner-command path (`/remember`, `/capture`, `/remind`) or a fresh session (`/new`) is the way to persist something. Every mutating tool registered to date — `store_memory`, `capture`, `remind`, `cancel_reminder` — is owner-turn-only. Owner commands are not model-initiated tool calls and are outside this requirement's scope.

#### Scenario: Mutating tool denied during an event turn
- **WHEN** the agent invokes `store_memory` (or `capture`) during an event-triage turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and the store is unchanged

#### Scenario: Tainted session denies mutations even on owner turns
- **WHEN** the owner follows up on a triage message in the session the event turn started, and the agent then invokes `store_memory`
- **THEN** the invocation is denied with outcome `out-of-scope`, the store is unchanged, and the tool result names the incident taint and the `/remember` / `/new` remedy

#### Scenario: Untainted owner session executes normally
- **WHEN** the agent invokes `store_memory` or `capture` in an owner session no event turn has touched and no taint source has been called in
- **THEN** the tool executes

#### Scenario: Gate state does not outlive the turn
- **WHEN** a non-announceable event turn completes (including by error) and the owner then requests a per-instance action in a fresh owner session
- **THEN** a normal approval prompt is sent

### Requirement: Gate concurrency is fail-closed
A per-instance authorization requested while another approval is already pending SHALL resolve as denied (fail closed) with outcome `rejected-busy` — without a second prompt, without disturbing the pending approval, and without raising an unhandled error into the agent turn. Standing-tier authorizations SHALL be unaffected by a pending approval.

#### Scenario: Two standing invocations in one assistant message
- **WHEN** the agent issues two standing-tier invocations in a single assistant message
- **THEN** both execute and no prompt is sent

#### Scenario: Concurrent per-instance request fails closed
- **WHEN** a per-instance authorization is requested while another approval is pending
- **THEN** it resolves as denied with an explicit "another approval is pending" result, the pending approval is unaffected, and its receipt records outcome `rejected-busy`

### Requirement: Mutations during suppressed event turns fail closed silently
During a non-announceable (cap-suppressed) event turn, a per-instance mutating invocation SHALL be denied without sending any approval prompt or other channel message — the mutation attempt is suppressed, not the prompt — resolving the invocation as not executed with a distinct `suppressed` outcome in its receipt. (In this change every production mutating tool is owner-turn-only, so turn-scope denial fires first; this requirement governs any event-scoped per-instance tool a later change introduces, and is exercised via a test-only tool.)

#### Scenario: Per-instance attempt in a suppressed turn is silent
- **WHEN** the agent invokes an event-scoped per-instance tool during a non-announceable event turn
- **THEN** no channel message is sent, the tool is not executed, the agent turn continues with a "suppressed" tool result, and the receipt records the outcome

### Requirement: Gate paths are covered by automated tests
The approve, deny, cancel-by-unrelated-message, timeout, standing-without-prompt, kill-switch-demotion, turn-scope/taint-denial, concurrency (both standing-concurrent and rejected-busy), and suppressed-turn-denial paths SHALL each be covered by automated tests driven through a channel-adapter test double.

#### Scenario: Gate test coverage
- **WHEN** the test suite runs
- **THEN** every path listed above passes

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
- **THEN** an approval prompt is sent and the tool's effect occurs only on an approval keyword

### Requirement: Scheduled delivery is app-initiated and outside the gate
Delivery of a due reminder SHALL NOT pass through the approval gate. Its authority was granted when the reminder was scheduled — by the owner directly through a command, or by a gate-authorized standing-tier invocation in an untainted owner turn whose confirmation echoed the resolved due time. The gate governs model-initiated invocations only; the scheduler, like an owner command, is not one. The scheduler SHALL therefore send without occupying or consulting the pending-approval slot, a pending approval SHALL be unaffected by a delivery, and a delivered reminder SHALL NOT be classifiable as an approval prompt (it carries the reminder marker, and the gate classifies inbound text only). Accountability for every delivery comes from its `reminder` audit record (audit-log spec), not from an approval.

#### Scenario: Delivery does not prompt
- **WHEN** a reminder comes due
- **THEN** it is delivered with no approval prompt, and no approval record is created for the send

#### Scenario: Delivery leaves a pending approval intact
- **WHEN** a reminder is delivered while an approval prompt is pending
- **THEN** the pending approval is unchanged and still resolvable by the owner's next approval or denial keyword

#### Scenario: Every delivery is traceable to a schedule
- **WHEN** any delivered reminder's audit trail is inspected
- **THEN** it contains both the `scheduled` record naming who scheduled it and the delivery record naming the scheduler

### Requirement: Tools may declare themselves taint sources
A tool SHALL be able to declare that its result can carry free text someone other than the owner authored, through a `raises_taint` declaration that defaults to false. The gate SHALL read the declaration from the registered tool instance, so a tool may set it on its class or when it is built.

When the gate returns a permitting decision for a taint-source tool, it SHALL taint the current turn before that decision returns, and therefore before the tool runs. Every scope check the gate makes after that point in the same turn SHALL be decided against a tainted context, so an owner-scoped mutating invocation is denied under *Mutating tools declare a turn scope, enforced per session*, including one authorized while the taint-source tool is still running. The taint SHALL be raised whether the taint-source tool then succeeds or fails. A taint-source tool whose own authorization does not permit it SHALL NOT raise taint. An invocation whose scope check ran before the taint was raised SHALL be unaffected by it.

A denial caused by a tool-raised taint SHALL give the model exactly this reason, with `<tool>` replaced by the registered name of the tool that raised the taint and no quoting around it:

```
a tool called earlier in this session (<tool>) returned content from outside the owner's control, so this session is now tainted for its lifetime and writes are out of scope in it; nothing was stored or scheduled. The owner can use /remember, /capture or /remind (which bypass this session entirely), or /new to start a clean session where writes work again.
```

and its `out-of-scope` receipt SHALL carry the `detail` `taint raised by <tool>`. When several taint sources are called, the first one is named. Raising taint in a turn that is already tainted — an event turn, or any turn of a session an incident tainted — SHALL change nothing: those denials keep their existing reason and a null receipt `detail`. Every other receipt the gate writes SHALL keep a null `detail`.

When no turn is framed, a permitted taint-source invocation SHALL leave the gate tainted, so later unframed owner-scoped mutating invocations are denied `out-of-scope` until a turn is next framed or cleared.

#### Scenario: A taint source mid-turn refuses a later store_memory
- **WHEN** in an untainted owner turn the agent invokes a taint-source tool and then `store_memory`
- **THEN** the taint-source tool executes, `store_memory` is denied with outcome `out-of-scope`, the memory store is unchanged, no channel message is sent for it, the tool result is exactly the tool-taint reason naming the taint source, and the receipt's `detail` is exactly `taint raised by <source>`

#### Scenario: A taint source mid-turn refuses a later capture
- **WHEN** in an untainted owner turn the agent invokes a taint-source tool and then `capture`
- **THEN** `capture` is denied with outcome `out-of-scope`, the inbox is unchanged, and the receipt's `detail` names the taint source

#### Scenario: Taint is raised before the tool runs
- **WHEN** a taint-source tool is authorized but never run, and `store_memory` is then authorized in the same turn
- **THEN** `store_memory` is denied with outcome `out-of-scope`

#### Scenario: A write authorized while the taint source is still running is refused
- **WHEN** a taint-source tool is authorized and is still running, and `store_memory` is authorized before it returns
- **THEN** `store_memory` is denied with outcome `out-of-scope`

#### Scenario: A write authorized before the taint stands
- **WHEN** in an untainted owner turn the agent invokes `store_memory` and then a taint-source tool, and then `store_memory` again
- **THEN** the first `store_memory` executes with receipt outcome `authorized`, and the second is denied with outcome `out-of-scope`

#### Scenario: A failing taint source still taints
- **WHEN** a taint-source tool returns a failure result and the agent then invokes `store_memory` in the same turn
- **THEN** `store_memory` is denied with outcome `out-of-scope`

#### Scenario: A taint source that was not permitted raises nothing
- **WHEN** the owner denies a per-instance taint-source tool, the agent then invokes `store_memory` in the same turn, then the same tool is approved, and then `store_memory` is invoked again
- **THEN** the first `store_memory` executes and the second is denied with outcome `out-of-scope`

#### Scenario: A declaration made when the tool is built is honoured
- **WHEN** a tool whose class does not declare `raises_taint` is built with the declaration set on the instance, and in an owner turn it is invoked and then `store_memory`
- **THEN** `store_memory` is denied with outcome `out-of-scope`

#### Scenario: Turns without a taint source are unchanged
- **WHEN** an owner turn invokes only tools that do not declare `raises_taint` and then `store_memory`
- **THEN** `store_memory` executes, its receipt records outcome `authorized` with a null `detail`, and the next turn in the same session is framed untainted

#### Scenario: An incident-tainted session keeps its reason
- **WHEN** in the owner follow-up of an event-started session the agent invokes a taint-source tool and then `store_memory`
- **THEN** the taint-source tool executes, and `store_memory` is denied with the existing incident reason and a null receipt `detail`

#### Scenario: The first taint source is the one named
- **WHEN** two different taint-source tools are invoked in one turn and then `store_memory`
- **THEN** the denial's reason and receipt `detail` name the first taint source

#### Scenario: An unframed taint source fails closed
- **WHEN** a taint-source tool is authorized with no turn framed, and then a standing owner-scoped write is authorized
- **THEN** the write is denied with outcome `out-of-scope`

#### Scenario: No production tool is a taint source
- **WHEN** the `Tool` base class is inspected, and every tool in the production registry is inspected with every optional tool enabled in configuration
- **THEN** the `Tool` default for `raises_taint` is false and the list of registered tools declaring it is empty

