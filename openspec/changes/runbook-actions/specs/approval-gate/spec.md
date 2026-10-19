## MODIFIED Requirements

### Requirement: Tools declare their mutation class
Every registered tool SHALL carry an explicit classification: `read-only`, `notify-only`, or `mutating`. Every mutating tool SHALL additionally carry an explicit authorization tier — `standing` or `per-instance` — declared in code alongside the tool, so a tier grant rides code review and cannot be widened by configuration. A runbook action tool SHALL instead declare that its tier is resolved per target, and every target it can be invoked with SHALL carry a `standing` or `per-instance` tier in the code registry (runbook-actions spec); the gate SHALL resolve the tier from the invocation's target before applying the kill switch, and SHALL deny with outcome `unregistered`, without any channel send, an invocation whose target has no registry entry or is disabled. The agent core SHALL refuse to register a tool without a classification, and SHALL refuse to register a mutating tool without an authorization tier or a per-target tier for every one of its targets. Mutating tool invocations SHALL always pass through the approval gate, which enforces the tier; read-only and notify-only invocations SHALL NOT require approval. The third tier, `never`, is the absence of registration: unregistered actions and unregistered targets remain denied by the closed-toolset boundary, and no registry entry expresses it.

#### Scenario: Unclassified tool rejected at startup
- **WHEN** a tool without a mutation classification is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: Mutating tool without a tier rejected at startup
- **WHEN** a mutating tool without an authorization tier is registered
- **THEN** startup fails with an error naming the tool

#### Scenario: Per-target tool with an untiered target rejected at startup
- **WHEN** a runbook action tool is registered and one of its registry targets lacks a tier
- **THEN** startup fails with an error naming the tool and the target

#### Scenario: Read-only tool bypasses the gate
- **WHEN** the agent invokes a read-only tool
- **THEN** the tool executes without any approval prompt

#### Scenario: Tier follows the target
- **WHEN** the agent invokes `restart_container` with a standing target and then with a per-instance target in an untainted owner turn
- **THEN** the first executes without a prompt and is receipted with tier `standing`, and the second sends an approval prompt and is receipted with tier `per-instance`

### Requirement: Standing tier can be demoted by configuration, never widened
A configuration flag SHALL demote all standing-tier tools to per-instance approval (kill-switch), defaulting to off. While it is on, runbook action tools SHALL additionally be narrowed to untainted owner turns: an invocation in an event turn or in a tainted session SHALL be denied with outcome `out-of-scope` and no channel send, so the kill switch never places an approval prompt beside a triage. No configuration SHALL promote a per-instance tool or target to standing, widen a tool's turn scope, add a target or an event binding, or register a new mutating tool — authorization widens only through code review. Disabling the runbook capability or individual targets is narrowing and is permitted.

#### Scenario: Kill-switch demotes standing tools
- **WHEN** the demotion flag is enabled and the agent invokes a standing-tier tool
- **THEN** the gate sends a per-instance approval prompt and the tool executes only on an approval keyword

#### Scenario: Kill-switch demotes a standing runbook action
- **WHEN** the demotion flag is enabled and the agent invokes `restart_container` with a standing target in an untainted owner turn
- **THEN** the gate sends an approval prompt that renders the target inside the argument delimiters, nothing is published before an approval keyword, and the receipt records tier `per-instance`

#### Scenario: Kill-switch keeps runbook actions out of triage
- **WHEN** the demotion flag is enabled and the agent invokes `restart_container` with a bound target during an event turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no approval prompt or other channel message is sent, and nothing is published

### Requirement: Mutating tools declare a turn scope, enforced per session
Every mutating tool SHALL declare the turn types in which it may execute (`owner`, `event`, `tainted-owner`), defaulting to owner-only; the declaration lives in code alongside the tool, like its tier. `tainted-owner` means an owner turn in a tainted session; a tool that does not declare it is treated in such a turn as in an event turn. The agent core SHALL supply the gate the current turn's context (turn type, announceability, whether the session is tainted, the taint's source when a tool raised it, and the session's bound runbook targets), scoped strictly to the turn. A session becomes tainted when it processes an event turn, or when a taint-source tool is authorized in one of its turns (requirement *Tools may declare themselves taint sources*), and SHALL remain tainted for its lifetime. An invocation of a tool whose scope excludes event turns SHALL be denied — without any channel send, fail closed, outcome `out-of-scope` in its receipt — during any event turn, and during any turn of a tainted session unless the tool declares `tainted-owner` and the turn is an owner turn, including the remainder of the turn in which a taint source raised the taint. The denial's tool result SHALL name the reason and the remedy: that the turn is an event turn, that the session is handling an incident, or that a tool earlier in the session returned content from outside the owner's control; and that the owner-command path (`/remember`, `/capture`, `/remind`) or a fresh session (`/new`) is the way to persist something or to act outside the incident. `restart_container` invoked in an event turn or in any turn of a tainted session, whatever raised the taint, SHALL additionally be limited to the session's bound targets (runbook-actions spec); a session tainted only by a taint-source tool has none. Every mutating tool registered before runbook actions — `store_memory`, `capture`, `remind`, `cancel_reminder` — is owner-turn-only. `restart_container` declares `owner`, `event`, and `tainted-owner`; `restart_unit` declares `owner` and `tainted-owner`; `wake_workstation` declares `owner`. Owner commands are not model-initiated tool calls and are outside this requirement's scope.

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

#### Scenario: Per-instance runbook action in an incident follow-up prompts
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent invokes `restart_unit` with `unit` `taiga-mcp`
- **THEN** an approval prompt is sent in the existing format with `unit: <<<taiga-mcp>>>`, and nothing is published before an approval keyword

#### Scenario: Per-instance runbook action never runs in an event turn
- **WHEN** the agent invokes `restart_unit` during an event turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no approval prompt is sent, and nothing is published

#### Scenario: Tainted session refuses an unbound restart
- **WHEN** the owner follows up on a triage message bound to `cadvisor` and the agent invokes `restart_container` with `container` `wordle`
- **THEN** the invocation is denied with outcome `out-of-scope`, nothing is published, and the tool result names `/new` as the way to act outside the incident

## ADDED Requirements

### Requirement: Per-instance runbook actions are approved per invocation
A per-instance runbook invocation SHALL follow *Inline approval over the channel* unchanged: one prompt naming the tool and its single target argument, inside the argument delimiters, single-use and argument-bound. Only an approval keyword SHALL cause the request to be published. The gate SHALL re-check the invocation's turn scope and rate limit when the approval arrives, and SHALL deny it with the corresponding outcome if either no longer permits it.

#### Scenario: Per-instance action approved
- **WHEN** the agent invokes `restart_unit` with `unit` `taiga-mcp` in an untainted owner turn and the owner replies `yes`
- **THEN** exactly one request for `restart_unit.taiga-mcp` is published, and the receipt records tier `per-instance` and outcome `approved` with the request id

#### Scenario: Per-instance action denied
- **WHEN** the owner replies `no` to the prompt for `restart_unit` with `unit` `taiga-mcp`
- **THEN** nothing is published, the tool result says the owner denied it, and the receipt records outcome `denied`

### Requirement: Rate-limited invocations fail closed with a receipt
A mutating invocation refused by a rate limit declared in code (runbook-actions spec) SHALL be denied before any publish and before any approval prompt, without a channel send, with outcome `rate-limited` in its receipt and a tool result that names the limit. The rate-limit check SHALL run after the turn-scope check and before tier resolution, so a rate-limited per-instance invocation never prompts.

#### Scenario: Rate-limited per-instance action does not prompt
- **WHEN** `taiga-mcp` was restarted 3 times in the last hour and the agent invokes `restart_unit` for it
- **THEN** no approval prompt is sent, nothing is published, and the receipt records outcome `rate-limited`
