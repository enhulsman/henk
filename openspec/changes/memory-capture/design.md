# Design — Memory & Capture

## Context

Henk (v1.2+durability) is read-only: four production tools, an approval gate that
has never fired outside tests, and no state that survives the idle window. The
North Star (2026-08-07) makes durable memory, thought capture, and the three-tier
authorization model the first roadmap change. Constraints inherited unchanged:
closed toolset enforced by the default-deny `PreToolUse` hook; owner-only outputs;
no new volume, port, listening socket, ACL grant, or secret (secure-deployment
posture); direct writes into the Obsidian vault are off the table (LiveSync
corruption risk, settled 2026-07-21); obsidian-todo-api is read-only by design.

Memory mechanics are adopted from a proven in-house design (Anna's memory
service — the owner's work-side assistant; patterns transfer, no client data or
work specifics do): one table of short natural-language facts, type-namespaced
with per-type caps and FIFO eviction, dump-all recall injected as markdown.

## Goals / Non-Goals

**Goals:**
- Facts survive restarts and idle expiry; continuity by rebuild, not longer
  sessions (`idle_timeout_seconds` stays 3600).
- A capture verb the owner can fire at Henk any time — as a tool in conversation
  or as a `/capture` command — landing in a durable inbox with a read-back path.
- The three-tier authorization model (standing / per-instance / never) plus its
  second axis, turn scope with session taint, as the reusable, transferable
  artifact — exercised by real production tools.
- Every mutation — model-initiated or owner command — leaves a durable receipt
  at decision time, including the tier under which it was authorized (fixes the
  verified `approvals`-always-empty defect and closes the previously unreceipted
  command path).

**Non-Goals:**
- The personal-inbox service (own future change — see D1).
- Reminders / anything time-scheduled (change 2).
- Ambient memory extraction from conversation (pruned growth path; the in-house
  precedent shows the explicit tool wins).
- Restart/runbook verbs or the standing-verb enumeration beyond `capture` and
  `store_memory` (change 5).
- Semantic/vector recall. The store is dozens of short facts; dump-all is the
  design, not a stopgap.

## Decisions

### D1. Capture inbox home: Henk-native store now, behind a seam; inbox service later

**Decision (owner, 2026-08-17):** change 1 ships a Henk-native SQLite inbox
behind a thin storage-backend interface (`InboxStore`: append / list / mark
done). A future small personal-inbox service — a push-only API/MCP-faced store
that Henk, vault-scraped todos, and other sources push into — is the intended
successor and gets its own change; when it lands, an HTTP-backed `InboxStore`
swaps in without touching agent logic, and the native store remains as the
spool/buffer so `capture` never fails when the service is down. **When that swap
happens, `capture`'s standing tier MUST be re-litigated:** the containment
argument below (Henk-local, cannot leave the container) does not survive the
swap.

Rejected alternatives:
- **Service inside change 1** — its own auth surface, container (charter
  constraint 4 scrutiny), and multi-writer design would triple this change while
  changes 2–5 are serialized behind it; Henk's own surface is identical either
  way.
- **ntfy topic as the inbox** — a message cache, not a store: no done-semantics,
  retention-bound, not queryable. (A deny-all ntfy *mirror* for pull-based pickup
  remains an easy later add-on; not in scope.)
- **Vault writes** — off the table, not relitigated.

### D2. Storage: one SQLite file on the existing audit volume

`memories` and `inbox` tables in a single SQLite database (`henk/store/` module)
on the volume already mounted beside the audit JSONL, e.g. `/data/store/henk.db`.
WAL mode. This inherits the volume's backup-allowlist coverage and adds no deploy
surface (spec'd in secure-deployment). The seam from D1 lives at the store
interface, not the file layout. The store path is its own config section
(`store.path`), not an events-scoped key — see D11.

Rejected: **JSONL** (Henk's audit precedent) — memory needs in-place deletion
(`/forget`) and eviction, and the inbox needs status updates; append-only JSONL
would force compaction logic that SQLite gives for free. JSONL stays correct for
the audit log precisely because records there are immutable.

### D3. Memory model: two explicit write paths mapped to two type namespaces

- `pinned` (cap 50, FIFO): owner-authored via `/remember`.
- `agent` (cap 20, FIFO): agent-authored via the `store_memory` tool (standing
  tier, owner-turn-only, receipted).

Caps adopted from the proven design; a per-fact length limit (default 500 chars,
honest rejection, never truncation) bounds row size. `/forget` deletes by
case-insensitive substring and echoes what it removed (bounded echo) so mistakes
are recoverable. Recall = the full store rendered as a delimited markdown block —
grouped by type, newest-first within group, render bounded at 8,000 chars with an
omission note (store never touched) — prefixed to the **first owner turn** of any
session that has not yet received it. Keying on the first *owner turn* (not
session creation) is deliberate: event turns must never carry memory, but an
owner follow-up continuing an event-started session is the common path in an
event-active homelab and must not silently miss recall.

Worst-case arithmetic, stated: 70 rows × 500 chars ≈ 35KB unbounded — hence the
8,000-char render bound (~2k tokens), which caps per-session injection cost.

The block carries a content hash **of the rendered block as injected**; the
session's audit record stores it (`memory_hash`, schema v3), so the audit trail
shows exactly which memory state a session saw. Mid-session writes need no
invalidation: the write's confirmation is already in the conversation.

### D4. Three-tier model: tier is a code-declared property of the named action

`Tool` gains an `authorization` attribute required for mutating tools
(`standing` | `per-instance`); the registry refuses a mutating tool without one,
exactly as it refuses a missing classification. **Never** stays what it already
is: unregistered, denied by the default-deny hook — no third enum value needed.

Tiers are declared in code, not config: a tier grant is a security decision that
must ride code review (owner-reviewed, publication-bound repo), not a YAML edit
on the host. Config can *narrow* (a kill-switch demoting standing →
per-instance globally) but never widen. No other gate-relevant config knob
exists — every config surface on the gate is a security surface, so knobs
require a user and a scenario to earn their place.

Gate behavior: standing → execute without prompting, receipt always;
per-instance → today's prompt/keyword flow, hardened per D6/D7. This change
ships exactly two standing actions (`capture`, `store_memory`) — argued on
**containment**, not anti-noise: both are append-only writes to Henk-local
stores, cannot reach outside the container, are owner-turn-only, and are
receipted. (The North Star's anti-noise justification for standing tier belongs
to change 5's verbs, which replace real message classes; these two verbs never
prompted before because they didn't exist. Claiming saved attention here would
be dishonest.) Argument-bound named actions (`restart_container:<name>`) are
change 5's problem; the tier attribute is deliberately shaped so a registry of
named actions can carry it later.

### D5. Receipts: durable at decision time, for every mutation path

The gate reports every mutating authorization decision — approved, denied,
cancelled, timed out, rejected-busy, out-of-scope, suppressed, and standing
authorizations alike — and each is **appended to the audit log immediately as an
`authorization` record** (same durability posture as suppression records: no
dependence on graceful close; an OOM-kill after a standing `capture` cannot
orphan the mutation from its receipt). The session record additionally
aggregates them in `approvals[]` — the verified always-empty defect is fixed by
threading the entries through `_write_audit_record`.

Record semantics kept honest:
- `initiated_by: "model" | "owner-command"`; `tier` is a *tool* property
  (`standing`/`per-instance`), null on command records; command records carry
  `turn_type: "command"` (they run outside any turn or session).
- Owner commands that mutate (`/remember`, `/forget`, `/capture`,
  `/inbox done`) write a receipt at execution time with a bounded effect
  summary; read-only commands and no-op commands (nothing matched/changed)
  write none — receipts record mutations.
- The gate records **authorization** (`authorized`), never "executed" — it
  cannot know execution. Execution evidence lives in the session record's
  `tool_calls`, whose entries gain an `executed` flag (true when the invocation
  was permitted to proceed), derived by correlating with the authorization
  records — never inferred from tool-result text.

Outcome vocabulary (v3 schema): `authorized`, `approved`, `denied` (explicit
keyword), `cancelled` (fail-closed by unrelated message — distinguishable from
an owner "no"), `timeout`, `suppressed` (per-instance in a non-announceable
turn), `out-of-scope` (turn-scope/taint denial), `rejected-busy` (concurrent
per-instance while one pending).

### D6. Suppressed event turns: suppress the mutation, not the prompt

During a non-announceable (cap-suppressed) event turn the owner must hear
nothing — but today the gate would still prompt, with zero context (verified:
`core.py` withholds the reply, `approval.py` sends unconditionally). Fix at the
right layer: per-instance invocations during such a turn are **auto-denied
without any channel send** (fail closed, receipt outcome `suppressed`). In this
change the case is only reachable by a test-only event-scoped tool (both
production mutating tools are owner-turn-only per D10), but the requirement is
what change 5's event-scoped verbs will inherit. Deny-not-defer is deliberate:
deferral machinery would be speculative surface for a path with no production
user yet — revisit recorded in the deferred ledger.

### D7. Approval prompts: resolve-then-confirm, delimited and truncated

`_format_prompt` currently interpolates model-chosen arguments raw (`{v!r}`),
so an argument can impersonate prompt text. Replaced by resolve-then-confirm
(pattern from the in-house broadcast-confirm flow): the prompt states the
*resolved action* — tool name plus each argument value rendered inside explicit
delimiters and truncated to a bounded length. Authorization is never derived
from payload content.

### D8. Owner commands execute app-side, no agent turn

`/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all`,
`/inbox done <id>` extend the existing `/new` dispatch in the core:
deterministic, instant, zero tokens, and owner-initiated by construction (no
gate involvement — the gate governs *model-initiated* tool calls; commands get
their receipts from the audit layer directly, per D5). `/capture` exists as a
command precisely because the change's headline verb must not cost a model turn
on its fastest path. The agent-facing paths (`store_memory`, `capture`,
`inbox_read`) are registered tools and follow registry/tier/scope rules.

### D9. Inbox semantics: append + oldest-first drain + done; no caps

Items: `id, text, created_at, source, status(open|done)`. `capture`/`/capture`
append; `inbox_read` and `/inbox` list the **oldest** 20 open items plus a
newer-remainder count (a capture inbox is a queue to drain — oldest-first keeps
every item reachable, so the listing bound never becomes de-facto eviction);
`/inbox all` lists everything; `/inbox done <id>` archives. No FIFO cap —
silently evicting captured thoughts is worse than growth (short text rows;
growth is negligible). No edit/delete of item text in v1.

### D10. Turn scope with session taint: the second permission axis

The tier axis answers "is this verb safe?"; it cannot answer "is this verb safe
when the turn's input is attacker-controlled?" — so mutating tools also declare
a **turn scope** (`owner`, `event`; default owner-only), and the gate enforces
it against the turn's context *and the session's taint*: a session that has
processed an event turn is tainted for life (`_start_event_session` is the only
way an event turn enters a session, so taint cannot be missed), and out-of-scope
tools are denied in any turn of a tainted session. This closes both halves of
the injection path: the event turn itself, and the owner follow-up that
incident-triage mandates must continue the same session.

**The read/write asymmetry, stated as the principle:** *reads* (recall
injection) are permitted in tainted sessions because Henk's outputs are
structurally owner-only (Signal to one identity, deny-all topics) — a tainted
session can at worst mislead the owner once, visibly. *Writes* are denied
because they persist beyond the session and reach every future session
invisibly. For writes, taint is a structural boundary at session granularity —
the tainted session cannot write, whatever the model was talked into — not a
probability reduction. Change 5 should lean on this axis, not relax it: an
event-scoped verb (e.g. `restart_container:<name>`) declares `event` scope
explicitly and executes in event turns by design — scope is a per-tool
declaration, so autonomy ("mundane-and-fixable gets fixed") is unaffected.

**Residual scope, stated so it is not overread:** taint keys on event turns.
Read-only tool results (Gatus endpoint names, Prometheus labels, backend error
bodies via `homelab_health`, note content via `todo_read`) still import
externally-influenced text into *untainted* owner sessions. That vector is
accepted as residual — structured, tool-formatted summaries are a far weaker
channel than free-form event payloads — and recorded here so "the tainted
session cannot write" is not misread as "no external text can ever reach
memory." The outbound direction is likewise open by the same asymmetry:
read-only tools (including `inbox_read`) remain callable in tainted sessions,
so a triage context can read Henk's own stores — acceptable for the same
reason recall injection is (outputs are structurally owner-only), and named
here so the boundary's full shape is on record.

Owner commands are exempt from taint: they are owner-authored input that never
passes through the model, so a mid-triage `/capture` works. Out-of-scope
denials name the reason and remedy in the tool result (session tainted by an
incident; `/remember`, `/capture`, or `/new` is the path) so the model relays a
stated constraint instead of improvising.

### D11. Audit decoupled from event intake

`AuditLog` is currently constructed only when `events.enabled` is true — a
leftover of audit arriving with the events change. With mutations in the
registry, receipts must exist in every supported configuration, including the
documented rollback path (`events.enabled: false`). Audit gets its own config
key (`audit.path`, falling back to the existing `events.audit_path` so rp5's
locally-modified config keeps working without a host edit) and is constructed
unconditionally.

## Known limitations (documented, not hidden)

- **Announceable event turns can also prompt with zero context**: the triage
  reply is sent only after the turn completes, so a mid-triage per-instance
  prompt from an event-scoped tool would arrive before any incident context.
  Unreachable in this change (no production event-scoped tools); change 5 must
  solve prompt context before shipping event-scoped per-instance verbs. In the
  deferred ledger.
- **Receipt density vs. rehydration window**: decision-time records densify the
  log; cadence rehydration is a bounded tail read (`_REHYDRATE_LIMIT = 10_000`),
  so the effective window shortens as record volume grows. Months-deep at Henk's
  volume; revisit if change 5 multiplies receipt volume.
- **Nobody reads receipts yet**: no `/receipts` surface exists; the audit log is
  inspected manually. A receipt-surfacing command is deferred (ledger).

## Deferred to later changes (the single ledger)

| Deferred decision | Where it lands |
|---|---|
| Defer-vs-deny for suppressed-turn mutations (deny chosen now) | change 5 |
| Event-scoped verbs + per-verb taint/scope re-litigation | change 5 |
| Prompt context for mid-triage per-instance approvals | change 5 |
| Standing-verb enumeration beyond `capture`/`store_memory` | change 5 |
| `capture` tier re-litigation at the backend swap | inbox-service change |
| Personal-inbox service (API/MCP face, multi-writer, auth) | inbox-service change |
| deny-all ntfy pickup mirror (only if read-back friction bites) | inbox-service change |
| Receipt-surfacing command (`/receipts` or similar) | change 4/5 |
| Ambient memory extraction | growth path, unscheduled |

## Risks / Trade-offs

- [Weak pickup story until the service change: reading captures means asking
  Henk or `/inbox`] → accepted knowingly (owner decision 2026-08-17); the seam
  keeps the service swap cheap; ntfy mirror documented as a later add-on.
- [Memory content is re-injected into owner sessions — a poisoned memory would
  persist across sessions] → write paths are structurally constrained: owner
  commands (owner-authored), or `store_memory` in untainted owner turns only
  (D10); every write is receipted at decision time; content is injected inside
  a delimited block framed as remembered fact, not instruction; caps and length
  limits bound the blast radius; `/memories` + `/forget` give inspection and
  recoverable removal.
- [The owner can type client data into `/remember`, and it would ride into
  every session prompt] → the Tier-W wall is physical for *stores*; free text
  typed at Henk is already within the owner's discretion for any message. The
  mitigation is the same as for conversation itself (owner discipline +
  structurally owner-only outputs). Recorded, not hand-waved.
- [Memory/inbox free text rides rp5's existing backup path] → same destination
  and sensitivity class as the audit log's owner-session records that already
  ride it; no new exposure surface.
- [Live backup of a SQLite file can catch a mid-write state] → WAL mode plus
  short transactions make torn reads unlikely; worst case loses the newest rows
  of a low-stakes store; acceptable at this scale.
- [First standing-tier tools normalize "act without asking"] → containment
  argument in D4; tier + scope live in code behind review; kill-switch demotes
  globally; receipts are durable and unconditional (D5, D11).
- [Session taint means a long incident interrogation can never write memory
  until `/new` or idle expiry] → deliberate; the owner types `/remember` or
  `/capture` (exempt, receipted) when something must stick; the denial result
  says exactly that.
- [Schema v3 while deployed rp5 still writes v2 during rollout] → records
  declare their version and prior schemas remain committed; readers already
  handle mixed logs.

## Migration Plan

1. Implement behind existing config surface: new `store` and `audit` config
   sections (audit falls back to `events.audit_path`), memory caps and the
   standing-demotion flag — additive keys with safe defaults, no secret, no new
   mount. With no memories and an empty inbox, behavior is identical to today
   except for tool availability.
2. Audit schema v3 document committed beside v1/v2.
3. Deploy = normal image rebuild + restart on rp5 (**explicit owner go
   required — hard stop**; also picks up the pending cryptography 49→50
   lockfile bump). Rollback = previous image; v3 records already written remain
   readable via their committed schema.

## Open Questions

None blocking. Settled this session: inbox home (D1, owner decision
2026-08-17); scrutiny rounds settled turn scope/taint (D10), durable receipts
(D5), and audit decoupling (D11). Everything deliberately deferred is in the
ledger above.

## Appendix: Purpose texts for touched specs (applied at sync/archive)

**approval-gate:** Gates every mutating action Henk can take behind the North
Star's two-axis permission model: a named action's authorization tier (standing
/ per-instance / never-unregistered) declared in code, and a turn scope enforced
with session taint so untrusted event input can never drive an out-of-scope
mutation. Standing actions trade prompts for receipts — an agent that acts
without asking must be more accountable, not less. The gate is fail-closed in
every ambiguous case and is one of the transferable artifacts.

**audit-log:** The append-only, schema-versioned record of what Henk did and why
he was allowed to: every triage, owner session, suppression, and mutation
receipt, durable at decision time and independent of graceful shutdown or event
intake. The published JSON Schema is the transferable artifact — another project
can validate its own records against it.

**agent-core:** Turns owner messages and triageable events into serial,
typed agent turns with the right context composed in (triage framing and
untrusted-data delimiting for events; memory recall for owner turns) and the
right boundaries enforced (closed toolset, session isolation per incident,
continuity by rebuild). Owner commands are dispatched app-side so deterministic
actions never cost a model turn.

**incident-triage:** Defines what happens when the homelab breaks: every
triageable event gets a full investigation and a durable handoff, the owner's
attention is spent only within the cadence contract (hard cap, suppression to
the record — never to the inbox), and triage runs with read-only hands unless a
verb's declared scope says otherwise.

**secure-deployment:** The deployed shape of the inherited security posture:
containerized on rp5 with scoped tokens only, loopback/tailnet binds, an
enumerated secret set, least-privilege ACLs, and an enumerated durable-state
surface (audit volume) — so what runs matches what the specs promise, and any
new surface is a deliberate spec change.

**memory-store:** Henk's durable, capped store of short owner facts —
continuity by rebuild: what the owner tells Henk survives restarts and idle
expiry and is recalled into every owner conversation, with explicit,
receipted write paths and structural protection from untrusted input.

**capture-inbox:** The durable landing place for passing thoughts — capture is
one message or command away, nothing is ever silently evicted, and the inbox
drains oldest-first. Backed today by a Henk-local store behind a seam shaped
for the future personal-inbox service.
