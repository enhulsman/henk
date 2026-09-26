## MODIFIED Requirements

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

## ADDED Requirements

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
