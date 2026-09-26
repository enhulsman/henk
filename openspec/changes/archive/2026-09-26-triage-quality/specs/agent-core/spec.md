## MODIFIED Requirements

### Requirement: Turns are typed and event turns carry triage framing
The serial queue SHALL carry typed turns distinguishing owner messages from event turns.
Event turns SHALL carry the event metadata: source, alert identity, payload,
firing/resolved state, announceable flag, and the event's notification and receive times.

When processing an event turn, the agent core SHALL compose the turn content in this
order:
1. the memory recall block (memory-store spec), when the store is non-empty and readable;
2. a clearly delimited untrusted-data block, holding each incident's payload and times
   followed by the related-handoff digest (triage-handoff spec) with any recurrence
   reference;
3. the triage-mode framing: the method and arc mandate, the handoff instruction, and the
   recurrence note where applicable.

The framing SHALL come after the untrusted-data block and SHALL name only tools registered
in the session. An event turn that injects the recall block SHALL mark the session as
having received it, so the owner follow-up in that session does not receive it again. The
event turn SHALL remain an event turn for the gate, and its session SHALL remain tainted.

Owner turns SHALL NOT receive triage framing, the untrusted-data block, or the digest.

**When reminders are enabled, every owner turn SHALL carry a one-line current-time
header** stating the current time in the owner's configured timezone, so a relative time
is resolved against the moment of the turn and not against the session's start. The
header:
- SHALL be composed per turn rather than per session;
- SHALL be delimited as data;
- SHALL NEVER be carried by an event turn;
- SHALL be produced by the same renderer as every reminder due time (reminders spec), so
  the time the model reasons from and the time the owner is told read identically.

The first owner turn of a session that has not yet received the recall block SHALL be
prefixed with it, per the memory-store spec.

**An owner turn SHALL additionally carry the delivered-reminder block when an unsurfaced
delivery exists** (reminders spec), independently of whether the recall block was already
given in that session. Event turns SHALL never carry it, and it SHALL NOT taint the
session.

Event-turn output SHALL be routed through the channel adapter's proactive owner-directed
send, suppressed for non-announceable incidents. Owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed while memories exist and a related handoff is retained
- **THEN** the text passed to the agent session is, in order, the recall block, the delimited untrusted-data block containing the event payload with its times followed by the digest, and the triage-mode framing — and it contains no time header and no delivered-reminder block

#### Scenario: Event turn with an empty store carries no recall block
- **WHEN** an event turn is processed while no memories exist
- **THEN** its content begins with the untrusted-data block and contains no recall block

#### Scenario: Recall given at the event turn is not repeated
- **WHEN** an event turn injected the recall block and the owner then sends a follow-up in that session
- **THEN** the follow-up's content carries no second recall block

#### Scenario: Event turns stay tainted with recall
- **WHEN** an event turn carrying the recall block attempts a mutating tool
- **THEN** the gate denies it as out of scope for an event turn, exactly as without recall

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing, no untrusted-data block and no digest, and its output is delivered as a normal reply

#### Scenario: Every owner turn knows the time
- **WHEN** reminders are enabled and two owner turns run an hour apart in the same session
- **THEN** each turn's content carries a current-time header reflecting the time of that turn, rendered in the owner's configured timezone with its weekday and zone marker

#### Scenario: No header when reminders are disabled
- **WHEN** reminders are disabled and an owner turn is processed
- **THEN** its content carries no current-time header

#### Scenario: First owner turn carries recall
- **WHEN** the first owner turn of a session runs while memories exist
- **THEN** its content is prefixed with the recall block (composition details per the memory-store spec)

#### Scenario: Delivered reminder reaches a mid-session turn
- **WHEN** a reminder is delivered while a session is already open and the owner then sends a message
- **THEN** that turn's content carries the delivered-reminder block even though the recall block was given earlier in the session

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

## ADDED Requirements

### Requirement: Event sessions run on the triage profile
The agent core SHALL create every session that an event turn starts from a **triage
profile**: a session model, an effort and a thinking mode configured separately from the
chat profile. Sessions started by an owner turn SHALL use the chat profile. A session
keeps the profile it was created with for its lifetime, so an owner follow-up inside a
triage session SHALL run on the triage profile. After `/new` or idle expiry, the next
owner session SHALL use the chat profile.

Each triage-profile setting that is **absent** from configuration SHALL equal the
corresponding resolved chat-profile setting, so a configuration that names none of them
behaves exactly as before this change. An explicit null effort or thinking SHALL keep its
existing meaning of deferring to the SDK's own default. The two profiles SHALL share one
tool registry and one approval gate, and SHALL differ only in model, effort and thinking.
Effort SHALL be passed to the SDK explicitly whenever it is configured, so a model's own
default effort never applies silently. Every session audit record SHALL name the profile
and the effort of **the factory that created the session**, never a profile inferred from
the record's trigger. An owner follow-up recorded from inside a triage session therefore
names the triage profile.

#### Scenario: Defaults change nothing
- **WHEN** the configuration names no triage-profile setting
- **THEN** event sessions are created with exactly the chat profile's model, effort and thinking

#### Scenario: A configured triage profile applies to event sessions only
- **WHEN** the triage profile names a different model and effort, and an event turn and then a fresh owner session run
- **THEN** the event session is created with the triage model and effort, and the owner session with the chat model and effort

#### Scenario: Follow-ups stay on the triage profile
- **WHEN** the owner replies to a triage message within the idle window
- **THEN** the reply runs in the triage session on the triage profile

#### Scenario: Reset returns to the chat profile
- **WHEN** the owner sends `/new` after a triage and then a message
- **THEN** the new session uses the chat profile

#### Scenario: Profiles share the boundary
- **WHEN** the chat and triage session factories are inspected
- **THEN** they hold the same tool registry and the same approval gate, and their configurations differ at most in model, effort and thinking

#### Scenario: The record names the profile
- **WHEN** an event triage's audit record is written
- **THEN** it carries profile `event` and the effort the session ran with

#### Scenario: A follow-up record names the factory's profile, not its trigger
- **WHEN** the owner's follow-up inside a triage session is recorded, with trigger `owner-message`
- **THEN** its record carries profile `event` and the triage effort

#### Scenario: A plain owner session records the chat profile
- **WHEN** an owner session that no event started is recorded
- **THEN** its record carries profile `chat` and the chat effort
