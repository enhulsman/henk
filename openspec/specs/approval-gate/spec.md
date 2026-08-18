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
A pending approval SHALL expire after a configured timeout (default 5 minutes). Expiry SHALL count as denial: the tool is not executed and the agent turn resumes with a "timed out, not executed" result.

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
Every mutating tool SHALL declare the turn types in which it may execute (`owner`, `event`), defaulting to owner-only; the declaration lives in code alongside the tool, like its tier. The agent core SHALL supply the gate the current turn's context (turn type, announceability, and whether the session is tainted), scoped strictly to the turn. A session becomes tainted when it processes an event turn and SHALL remain tainted for its lifetime. An invocation of a tool whose scope excludes event turns SHALL be denied — without any channel send, fail closed, outcome `out-of-scope` in its receipt — during any event turn AND during any turn of a tainted session. The denial's tool result SHALL name the reason and the remedy: that the session is handling an incident (or the turn is an event turn), and that the owner-command path (`/remember`, `/capture`) or a fresh session (`/new`) is the way to persist something. Both mutating tools introduced by this change (`store_memory`, `capture`) are owner-turn-only. Owner commands are not model-initiated tool calls and are outside this requirement's scope.

#### Scenario: Mutating tool denied during an event turn
- **WHEN** the agent invokes `store_memory` (or `capture`) during an event-triage turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and the store is unchanged

#### Scenario: Tainted session denies mutations even on owner turns
- **WHEN** the owner follows up on a triage message in the session the event turn started, and the agent then invokes `store_memory`
- **THEN** the invocation is denied with outcome `out-of-scope`, the store is unchanged, and the tool result names the incident taint and the `/remember` / `/new` remedy

#### Scenario: Untainted owner session executes normally
- **WHEN** the agent invokes `store_memory` or `capture` in an owner session no event turn has touched
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
