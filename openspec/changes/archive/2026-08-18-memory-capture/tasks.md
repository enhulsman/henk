# Tasks — Memory & Capture

TDD throughout: each group starts by writing tests derived from the delta spec
scenarios (each Given/When/Then → at least one test; each SHALL → at least one
assertion), then implements to green. Implementation happens in a fresh session
via `/opsx:apply`. **Hard stop before any deploy to rp5 — explicit owner go
required.**

## 1. Store foundation

- [x] 1.1 Tests: `henk/store/` SQLite module — memories table (types, per-fact
      length limit rejection, caps, FIFO per type with evicted-content returned,
      restart survival) and inbox table (append, oldest-first list with
      remainder count, list-all, mark done, unknown id, no eviction), WAL mode,
      path from the new `store.path` config
- [x] 1.2 Implement `henk/store/`: schema creation, memory repository
      (add/list/delete-by-substring-with-echo/trim-FIFO), inbox repository
      behind the `InboxStore` seam (design D1/D2/D9)
- [x] 1.3 Test: backend-seam behavior invariance — inbox tools pass identical
      behavioral tests against the SQLite backend and a test double

## 2. Gate: tiers, turn scope, concurrency, prompts

- [x] 2.1 Tests from approval-gate delta scenarios: mutating-without-tier
      rejected at registration; standing executes with zero channel sends and
      never touches the pending slot; per-instance flow unchanged
      (approve/deny/cancel-by-unrelated/timeout); kill-switch demotes standing;
      unregistered tool still denied
- [x] 2.2 Add `authorization` tier + turn-scope declaration to `Tool` and
      registry validation (`henk/tools/base.py`); gate standing path + demotion
      flag (`henk/gate/approval.py`, `henk/config.py`)
- [x] 2.3 Tests: turn scope + session taint — denial in event turns; denial in
      owner turns of tainted sessions with reason+remedy in the result text;
      untainted owner turns execute; taint set at `_start_event_session` and
      never cleared for the session's life; per-turn gate context cleared on
      every exit path including error (post-turn prompt normal)
- [x] 2.4 Implement turn-scope/taint enforcement: core supplies per-turn context
      (turn type, announceability, taint) via try/finally;
      `henk/agent/permission.py` + gate deny out-of-scope with honest result
- [x] 2.5 Tests: gate concurrency — two standing invocations in one assistant
      message both execute; concurrent per-instance resolves `rejected-busy`
      without disturbing the pending approval and without raising
- [x] 2.6 Implement fail-closed concurrency (replace `GateBusyError` propagation
      at the permission layer)
- [x] 2.7 Tests: resolve-then-confirm prompt — delimited, truncated argument
      rendering; crafted argument cannot alter prompt structure or keyword match
- [x] 2.8 Replace `_format_prompt`'s raw `{v!r}` interpolation with
      resolved-action rendering (delimiters + bounded truncation)
- [x] 2.9 Tests + implementation: suppressed event turn — per-instance
      invocation (test-only event-scoped tool) denied with no channel send and
      `suppressed` outcome

## 3. Receipts: durable, unconditional, threaded (before or with the first mutating tool)

- [x] 3.1 Tests from audit-log delta scenarios: `authorization` record durable at
      decision time (SIGKILL-style: no graceful close, record on disk);
      per-instance decision recorded; standing `authorized` receipt;
      owner-command receipts (`/capture`; `/forget` with count); no-op commands
      write no receipt; receipts written with events disabled; session
      `approvals[]` never empty when a mutating tool was invoked; v3 records
      validate; v1/v2 records still validate
- [x] 3.2 Decouple audit from event intake: `audit.path` config (fallback
      `events.audit_path`), `AuditLog` constructed unconditionally in
      `henk/runtime.py` (design D11)
- [x] 3.3 `authorization_record()` builder + immediate append at decision time;
      gate decision recorder reporting (tool, tier, outcome, reference,
      turn_type, initiated_by) for every mutating invocation and mutating owner
      command
- [x] 3.4 Fix the verified defect: thread recorded entries into `_SessionAudit`
      and pass `approvals=` in `AgentCore._write_audit_record` → `session_record`
- [x] 3.5 Test: acc-rotation receipt scoping — approvals recorded during a
      triage acc do not leak into the continuation (interrogation) record
- [x] 3.6 `tool_calls.executed` flag derived by correlating with authorization
      records (never from result text); explicit test for whether the SDK
      surfaces denied calls as ToolUseBlocks (verify, don't assume)
- [x] 3.7 Bump `SCHEMA_VERSION` to 3; commit `audit-record.v3.schema.json`
      (authorization record type, approvals entry shape, executed flag,
      memory_hash); keep v1/v2 documents

## 4. Memory capability

- [x] 4.1 Tests from memory-store delta scenarios: commands (`/remember`,
      `/forget` substring + bounded echo + multi-match + no-match, `/memories`,
      empty remember, over-limit rejection, eviction named in confirmation),
      `store_memory` (agent-type, empty fails, over-limit fails, standing
      receipt), untrusted-input negative paths (event payload cannot plant a
      memory; tainted-session content never reaches later recall)
- [x] 4.2 Owner command dispatch in `AgentCore._process_owner` extending the
      `/new` pattern (agent-core delta), wired to the memory repository, with
      command receipts per D5
- [x] 4.3 `store_memory` tool (mutating, standing, owner-turn-only) registered
      in the production toolset
- [x] 4.4 Tests + implementation: recall block — first owner turn per session
      (including owner follow-up in an event-started session; never event
      turns), delimited typed markdown newest-first within groups, 8,000-char
      render bound with omission count (store intact), hash of the block as
      injected, hash into the session audit record, empty store injects nothing
- [x] 4.5 Tests + implementation: memory store failure modes — unreadable store
      does not block the turn (no recall block, error logged); failed writes
      never report success

## 5. Capture capability

- [x] 5.1 Tests from capture-inbox delta scenarios: capture durability (SIGKILL
      after result), empty capture fails, no approval prompt (untainted owner
      turn), `/capture` command (no agent turn, confirmation with id, works
      mid-triage), read-back oldest-first 20 + "and N newer", `/inbox all`,
      `/inbox done <id>` including unknown id, done items excluded but not
      deleted, no eviction under growth, store-reopen persistence, failure
      modes (failed capture never claims success; unreadable inbox is not
      "empty")
- [x] 5.2 `capture` tool (mutating, standing, owner-turn-only) + `inbox_read`
      tool (read-only) over the `InboxStore` seam; register both in the
      production toolset
- [x] 5.3 `/capture`, `/inbox`, `/inbox all`, `/inbox done <id>` owner commands
      in the dispatch, with receipts for the mutating ones

## 6. Config and system prompt

- [x] 6.1 Config: `store` section (path, memory caps, fact length limit, render
      bound), `audit` section (path with events fallback), standing-demotion
      flag — additive keys with safe defaults, no new secret
- [x] 6.2 Update the system prompt's tool enumeration (config default) for
      `store_memory`, `capture`, `inbox_read`, and the owner command set; keep
      the honest-capability framing
- [x] 6.3 Test: production registry contains exactly the intended toolset with
      correct classes, tiers, and turn scopes (replaces the old "registry stays
      read-only" assertion deliberately)

## 7. Hygiene and verification

- [x] 7.1 At sync/archive: apply the drafted Purpose texts from design.md's
      appendix to approval-gate, audit-log, agent-core, incident-triage,
      secure-deployment, memory-store, and capture-inbox (paste operation — no
      drafting left to that session)
- [x] 7.2 After archive: re-read the merged approval-gate and audit-log specs
      end-to-end for contradictions (the scrutiny loop verified the deltas, not
      the merge). Note: "Schema is versioned" deliberately subsumes the old
      "readers SHALL be able to distinguish records across versions" clause via
      the prior-documents-remain-committed obligation — not an accidental drop.
      **Read done, no contradictions.** One ambiguity recorded for a later change
      (not edited here — it sits in an untouched requirement and an edit would be
      unreviewed): approval-gate's "Fail closed on timeout" still says expiry
      "SHALL count as denial", which its own colon defines as *not executed* with
      a "timed out" result — consistent with the distinct `timeout` receipt
      outcome, but a skimming reader could read it as recording `denied`
- [x] 7.3 Full test suite green; `openspec validate --all` clean
- [x] 7.4 Deploy-smoke checklist (deploy-verified items, ONLY after the explicit
      owner go): inbox/memory survive container recreation on rp5; SDK
      denied-call ToolUseBlock visibility confirmed against the live SDK; deploy
      also picks up the pending cryptography lockfile rebuild
- [x] 7.5 Commit via git-commit-handler (publication rules; pre-commit hook);
      **no deploy — explicit owner go required**

## As-built (deployed to rp5 2026-08-18, image rebuilt from `24fb65a`)

Deploy-verified results for task 7.4, recorded here because none of it is reproducible
from the test suite:

- **Memory and inbox survive container recreation.** Verified with
  `up -d --force-recreate henk`, and the test was stronger than it looks: at that moment
  `henk-store.db` was 4 KB while `henk-store.db-wal` was 84 KB, so nearly all rows lived in
  the write-ahead log. SQLite replayed it on open in the fresh container and every item was
  still listed. WAL mode plus `synchronous=FULL` is doing what D2 claimed.
- **The SDK DOES surface a denied tool call as a `ToolUseBlock`** (claude-agent-sdk 0.2.123).
  Exercised by setting `gate.demote_standing: true`, denying a `store_memory` prompt, and
  projecting the session record: `tool_calls` carried
  `("store_memory", executed=False)` with the matching `denied` entry in `approvals`. So the
  `executed` flag is load-bearing rather than theoretical — without it, a denied invocation
  would read as an executed one in the record. Its receipt also kept `tier: "standing"`
  while enforcement was demoted, confirming tier is reported as a *tool* property.
- **The cryptography lockfile bullet was a false premise, not a passed check.** The image is
  built with `pip install ".[runtime]"` and never reads `uv.lock`; `cryptography` only enters
  the lock transitively via `pyjwt[crypto]` in the dev/uv resolution. Nothing about the
  49→50 bump reaches the deployed image, so there was nothing to verify.
- **Receipts, approvals threading and `memory_hash` all confirmed in production.** The
  verified always-empty-`approvals` defect is fixed on the live host; two consecutive owner
  sessions recorded *different* `memory_hash` values because the store grew between them,
  which is the property the field exists for.
- **Backup coverage confirmed** (the secure-deployment claim that the store "rides the
  volume's existing backup path"): rp5's backup script lists `henk_henk_audit` in its
  volume allowlist and produces a dated tarball for it nightly; it also copies the deployed
  `config.yaml`.

Two findings for a later change (neither blocks anything):

1. **`tool_calls[].result_id` carries full tool result text**, not just the handoff message
   id its name implies. With memory and capture in the registry that means owner-personal
   content now lands in the audit JSONL and its backups — same sensitivity class as the
   store itself, so not an exposure change, but it defeats the "audit records are metadata"
   assumption and makes raw records unsafe to share. Candidate fix: bound or drop result
   text for non-handoff tools.
2. **The three new tools omit `additionalProperties: false`** from their parameter schemas,
   where every pre-existing tool sets it. Harmless (a looser schema, and both write tools
   absorb extra keys), but inconsistent with the shape proven against the live SDK.
