# Same-Turn Taint

**For the owner, in plain terms.** Nothing you see or do changes with this change alone. It
adds a safety catch that no current tool trips: a tool can now be marked as one that brings
text from outside your control into the conversation. The moment such a tool is called, the
rest of that turn, and the rest of that session, can no longer write to memory, the capture
inbox or reminders. Today that cut-off only happens at the start of a turn, so a write later
in the same turn slips past it. No tool is marked in this change, so every conversation
behaves exactly as it does now. The first tool to be marked will come with the
session-titles feature (roadmap 4b), which cannot ship until this catch exists.

When the catch does fire, Henk is told why and relays it to you. The text Henk receives, and
will paraphrase to you, is exactly:

```
a tool called earlier in this session (<tool>) returned content from outside the owner's control, so this session is now tainted for its lifetime and writes are out of scope in it; nothing was stored or scheduled. The owner can use /remember, /capture or /remind (which bypass this session entirely), or /new to start a clean session where writes work again.
```

`<tool>` is the registered name of the tool that raised the taint. `/remember`, `/capture`,
`/remind` and `/new` keep working exactly as today.

**Decisions that are yours** (my pick first):

1. **How long a mid-turn taint lasts.** (a) *For the rest of the session* (recommended): the
   untrusted text stays in the conversation the model reads on every later turn, so a write
   on the next turn is exactly as exposed as one later in the same turn; this matches how an
   incident already taints a session. (b) Only for the rest of the turn: your next message in
   the same session could write again while the untrusted text is still in context.
   **The cost of (a), concretely:** once session titles ship and are switched on, asking
   Henk about your running sessions switches off Henk's own memory, capture and reminder
   writes for the rest of that conversation, until `/new` or the idle timeout. Your
   `/remember`, `/capture` and `/remind` commands still work throughout.
2. **Which existing tools are marked now.** (a) *None* (recommended). The line this change
   writes down for tool authors: a tool is a taint source when its result can carry free
   text that someone other than you authored. Every current read either reduces what it
   fetches to fixed-shape values (`homelab_health`, `homelab_query`, `sessions_read`) or
   returns text you wrote (`todo_read`, `inbox_read`, `reminders_read`), so none qualifies,
   and marking any would start refusing writes in conversations that work today.
   (b) Mark some reads now anyway, as a precaution: stricter, but it changes everyday
   behaviour with no untrusted text to justify it.
3. **How the refusal is recorded.** (a) *In the existing authorization receipt*
   (recommended): the `out-of-scope` receipt every refusal already writes gains a `detail`
   naming the tool that raised the taint. The field already exists, so the audit schema and
   your stored records do not change. (b) Also add a taint field to the session record: a
   schema bump (v6) for information the receipt already carries whenever it matters.
4. **How a tool gets marked.** (a) *Per tool as it is built* (recommended): a tool can be
   marked only in the mode that renders outside text, so `sessions_read` with titles
   switched on is a taint source and `sessions_read` without them is not. Configuration can
   switch that mode on; it can never unmark a tool whose code marks it. (b) A fixed mark per
   kind of tool: session-titles would then have to add a separate titles tool, because
   marking `sessions_read` itself would end writes on every "what's running?", titles or not.

## Why

Taint is read once per turn. The agent core builds the gate's `TurnContext` at turn entry
(`henk/agent/core.py:683-698`) from `self._session_tainted`, and nothing updates it while the
turn runs. A tool call that pulls untrusted content into the model's context mid-turn
therefore cannot stop a later `store_memory` or `capture` in that same turn. The
session-awareness design found this while scoping free-text session titles and deferred it
(archived `2026-09-06-session-awareness/design.md`, *Deferred: session titles*); the North
Star roadmap row 4b makes it that follow-up's first task. It has to exist before any tool
renders untrusted text, so it ships on its own, ahead of the titles.

## What Changes

- **Tools can declare themselves taint sources.** A new declaration on `Tool`,
  `raises_taint`, default false, set in code by the tool's class or by the tool when it is
  built (decision 4). The gate reads it from the registered tool.
- **Taint is raised at authorization.** When the gate returns a permitting decision for a
  taint-source tool, it taints the current turn before the tool runs. Every scope check
  after that point in the turn sees a tainted context, so a later owner-scoped mutating call
  is refused with outcome `out-of-scope`, receipted, with no channel send.
- **The session inherits it.** When the turn ends, on every exit path, a taint raised during
  it marks the session tainted for its lifetime, exactly like an incident. `/new`, idle
  expiry and an incident displacing the session clear it, as today.
- **Honest reason, audited source.** A refusal caused by a tool-raised taint carries the
  reason quoted above instead of the incident wording (which would be false: there was no
  incident), and its receipt's `detail` is `taint raised by <tool>`. Incident-tainted
  sessions and event turns keep their current reason and receipts unchanged.
- **Owner commands stay exempt.** `/remember`, `/capture`, `/remind` do not pass through the
  gate and are unaffected.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `approval-gate`: *Mutating tools declare a turn scope, enforced per session* widens "a
  session becomes tainted" to include a taint raised mid-turn; ADDED *Tools may declare
  themselves taint sources* (declaration, raise-at-authorization, same-turn refusal, reason,
  receipt detail, unframed fail-closed).
- `agent-core`: ADDED *Taint raised during a turn persists for the session* (fold-back on
  every exit path, cleared only by a new session, commands exempt).

## Impact

- Code: `henk/tools/base.py` (the declaration), `henk/gate/approval.py` (raise, reason,
  receipt detail), `henk/agent/core.py` (fold the turn's taint into the session on exit).
  No change to the SDK wiring: every Henk tool call reaches `decide_tool_permission` →
  `ApprovalGate.authorize` through `can_use_tool` (design, *Risks*, for why that holds for
  read-only tools too).
- Stored data and audit schema: none. `detail` already exists on authorization records.
- Config, dependencies, deployment: none.
- Docs: at archive, NORTH-STAR row 4b is updated to say the taint prerequisite has landed
  and to carry the session-titles obligations listed below.

## Deliberately left out

- **Session titles themselves** (the rest of roadmap 4b): no free text is rendered, no
  publisher change, no `session_render_free_text` switch.
- **Marking any existing tool as a taint source** (decision 2).
- **A session-record taint field** (decision 3). A turn that raises taint but attempts no
  write leaves no taint-specific trace; the session-titles change can add one if it needs it.
- **The system prompt** still describes only incident taint ("any conversation an incident
  has touched"). Changing it would change every untainted turn's prompt; session-titles
  owns the wording once a taint source exists.
- **Live verification.** With no production taint source there is nothing to observe live.
  session-titles owns the first live check, including that `can_use_tool` fires for a
  read-only Henk tool.
- **Re-checking scope after a per-instance approval.** No per-instance tool is registered;
  see design D3.
