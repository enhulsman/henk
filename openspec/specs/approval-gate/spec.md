# approval-gate Specification

## Purpose
TBD - created by archiving change henk-v1. Update Purpose after archive.
## Requirements
### Requirement: Tools declare their mutation class
Every registered tool SHALL carry an explicit classification: `read-only`, `notify-only`, or `mutating`. The agent core SHALL refuse to register a tool without a classification. Mutating tool invocations SHALL always pass through the approval gate; read-only and notify-only invocations SHALL NOT require approval.

#### Scenario: Unclassified tool rejected at startup
- **WHEN** a tool without a mutation classification is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: Read-only tool bypasses the gate
- **WHEN** the agent invokes a read-only tool
- **THEN** the tool executes without any approval prompt

### Requirement: Inline approval over the channel
When the agent attempts to invoke a mutating tool, the gate SHALL suspend the invocation and send the owner an approval prompt over the channel adapter containing the tool name and the exact arguments. The owner responds with an approval keyword (`yes` / `approve`) or a denial keyword (`no` / `deny`), matched exactly and case-insensitively. Internally, the gate SHALL bind the pending approval to that single invocation and its arguments via a one-time reference; the owner never types the reference. At most one approval SHALL be pending per conversation at any time (guaranteed by serial turn processing).

#### Scenario: Owner approves
- **WHEN** the gate sends an approval prompt and the owner replies with an approval keyword
- **THEN** the tool executes exactly once with the arguments shown in the prompt, and the result flows back into the agent turn

#### Scenario: Owner denies
- **WHEN** the owner replies with a denial keyword
- **THEN** the invocation is cancelled, the agent turn continues with a "denied by owner" tool result, and the tool is not executed

#### Scenario: Unrelated message during pending approval
- **WHEN** the owner sends a message that matches no approval or denial keyword while a gate is pending (e.g., "what's on my board?")
- **THEN** the gate resolves as denied (fail closed), the suspended turn resumes with a "denied" result and its reply states that the pending action was cancelled, and the owner's message is then processed as a normal new turn in order — it is not swallowed

#### Scenario: Single pending approval per conversation
- **WHEN** an approval prompt is pending in a conversation
- **THEN** no second approval prompt can become outstanding in that conversation until the first is resolved

#### Scenario: Approval is single-use and argument-bound
- **WHEN** an invocation has been approved and executed
- **THEN** that approval cannot authorize any further invocation, including an identical one; a new invocation requires a new prompt

### Requirement: Fail closed on timeout
A pending approval SHALL expire after a configured timeout (default 5 minutes). Expiry SHALL count as denial: the tool is not executed and the agent turn resumes with a "timed out, not executed" result.

#### Scenario: Owner does not respond
- **WHEN** no owner response arrives within the timeout
- **THEN** the invocation is cancelled without executing, and the owner's next message is treated as a normal message, not a late approval

### Requirement: Gate is exercised in v1 despite zero production mutating tools
v1 SHALL ship no mutating tools in the production tool registry, but SHALL include a test-only mutating tool and automated tests that drive the full approve, deny, and timeout paths through a channel-adapter test double.

#### Scenario: Approval flow covered by tests
- **WHEN** the v1 test suite runs
- **THEN** approve, deny, and timeout paths of the gate each pass against the test-only mutating tool

#### Scenario: Production registry stays read-only
- **WHEN** the production configuration is loaded
- **THEN** the registered toolset contains no mutating tools

