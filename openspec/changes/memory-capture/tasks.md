# Tasks — Memory & Capture

TDD throughout: each group starts by writing tests derived from the delta spec
scenarios (each Given/When/Then → at least one test; each SHALL → at least one
assertion), then implements to green. Implementation happens in a fresh session
via `/opsx:apply`. **Hard stop before any deploy to rp5 — explicit owner go
required.**

## 1. Store foundation

- [ ] 1.1 Tests: `henk/store/` SQLite module — memories table (types, per-fact
      length limit rejection, caps, FIFO per type with evicted-content returned,
      restart survival) and inbox table (append, oldest-first list with
      remainder count, list-all, mark done, unknown id, no eviction), WAL mode,
      path from the new `store.path` config
- [ ] 1.2 Implement `henk/store/`: schema creation, memory repository
      (add/list/delete-by-substring-with-echo/trim-FIFO), inbox repository
      behind the `InboxStore` seam (design D1/D2/D9)
- [ ] 1.3 Test: backend-seam behavior invariance — inbox tools pass identical
      behavioral tests against the SQLite backend and a test double

## 2. Gate: tiers, turn scope, concurrency, prompts

- [ ] 2.1 Tests from approval-gate delta scenarios: mutating-without-tier
      rejected at registration; standing executes with zero channel sends and
      never touches the pending slot; per-instance flow unchanged
      (approve/deny/cancel-by-unrelated/timeout); kill-switch demotes standing;
      unregistered tool still denied
- [ ] 2.2 Add `authorization` tier + turn-scope declaration to `Tool` and
      registry validation (`henk/tools/base.py`); gate standing path + demotion
      flag (`henk/gate/approval.py`, `henk/config.py`)
- [ ] 2.3 Tests: turn scope + session taint — denial in event turns; denial in
      owner turns of tainted sessions with reason+remedy in the result text;
      untainted owner turns execute; taint set at `_start_event_session` and
      never cleared for the session's life; per-turn gate context cleared on
      every exit path including error (post-turn prompt normal)
- [ ] 2.4 Implement turn-scope/taint enforcement: core supplies per-turn context
      (turn type, announceability, taint) via try/finally;
      `henk/agent/permission.py` + gate deny out-of-scope with honest result
- [ ] 2.5 Tests: gate concurrency — two standing invocations in one assistant
      message both execute; concurrent per-instance resolves `rejected-busy`
      without disturbing the pending approval and without raising
- [ ] 2.6 Implement fail-closed concurrency (replace `GateBusyError` propagation
      at the permission layer)
- [ ] 2.7 Tests: resolve-then-confirm prompt — delimited, truncated argument
      rendering; crafted argument cannot alter prompt structure or keyword match
- [ ] 2.8 Replace `_format_prompt`'s raw `{v!r}` interpolation with
      resolved-action rendering (delimiters + bounded truncation)
- [ ] 2.9 Tests + implementation: suppressed event turn — per-instance
      invocation (test-only event-scoped tool) denied with no channel send and
      `suppressed` outcome

## 3. Receipts: durable, unconditional, threaded (before or with the first mutating tool)

- [ ] 3.1 Tests from audit-log delta scenarios: `authorization` record durable at
      decision time (SIGKILL-style: no graceful close, record on disk);
      per-instance decision recorded; standing `authorized` receipt;
      owner-command receipts (`/capture`; `/forget` with count); no-op commands
      write no receipt; receipts written with events disabled; session
      `approvals[]` never empty when a mutating tool was invoked; v3 records
      validate; v1/v2 records still validate
- [ ] 3.2 Decouple audit from event intake: `audit.path` config (fallback
      `events.audit_path`), `AuditLog` constructed unconditionally in
      `henk/runtime.py` (design D11)
- [ ] 3.3 `authorization_record()` builder + immediate append at decision time;
      gate decision recorder reporting (tool, tier, outcome, reference,
      turn_type, initiated_by) for every mutating invocation and mutating owner
      command
- [ ] 3.4 Fix the verified defect: thread recorded entries into `_SessionAudit`
      and pass `approvals=` in `AgentCore._write_audit_record` → `session_record`
- [ ] 3.5 Test: acc-rotation receipt scoping — approvals recorded during a
      triage acc do not leak into the continuation (interrogation) record
- [ ] 3.6 `tool_calls.executed` flag derived by correlating with authorization
      records (never from result text); explicit test for whether the SDK
      surfaces denied calls as ToolUseBlocks (verify, don't assume)
- [ ] 3.7 Bump `SCHEMA_VERSION` to 3; commit `audit-record.v3.schema.json`
      (authorization record type, approvals entry shape, executed flag,
      memory_hash); keep v1/v2 documents

## 4. Memory capability

- [ ] 4.1 Tests from memory-store delta scenarios: commands (`/remember`,
      `/forget` substring + bounded echo + multi-match + no-match, `/memories`,
      empty remember, over-limit rejection, eviction named in confirmation),
      `store_memory` (agent-type, empty fails, over-limit fails, standing
      receipt), untrusted-input negative paths (event payload cannot plant a
      memory; tainted-session content never reaches later recall)
- [ ] 4.2 Owner command dispatch in `AgentCore._process_owner` extending the
      `/new` pattern (agent-core delta), wired to the memory repository, with
      command receipts per D5
- [ ] 4.3 `store_memory` tool (mutating, standing, owner-turn-only) registered
      in the production toolset
- [ ] 4.4 Tests + implementation: recall block — first owner turn per session
      (including owner follow-up in an event-started session; never event
      turns), delimited typed markdown newest-first within groups, 8,000-char
      render bound with omission count (store intact), hash of the block as
      injected, hash into the session audit record, empty store injects nothing
- [ ] 4.5 Tests + implementation: memory store failure modes — unreadable store
      does not block the turn (no recall block, error logged); failed writes
      never report success

## 5. Capture capability

- [ ] 5.1 Tests from capture-inbox delta scenarios: capture durability (SIGKILL
      after result), empty capture fails, no approval prompt (untainted owner
      turn), `/capture` command (no agent turn, confirmation with id, works
      mid-triage), read-back oldest-first 20 + "and N newer", `/inbox all`,
      `/inbox done <id>` including unknown id, done items excluded but not
      deleted, no eviction under growth, store-reopen persistence, failure
      modes (failed capture never claims success; unreadable inbox is not
      "empty")
- [ ] 5.2 `capture` tool (mutating, standing, owner-turn-only) + `inbox_read`
      tool (read-only) over the `InboxStore` seam; register both in the
      production toolset
- [ ] 5.3 `/capture`, `/inbox`, `/inbox all`, `/inbox done <id>` owner commands
      in the dispatch, with receipts for the mutating ones

## 6. Config and system prompt

- [ ] 6.1 Config: `store` section (path, memory caps, fact length limit, render
      bound), `audit` section (path with events fallback), standing-demotion
      flag — additive keys with safe defaults, no new secret
- [ ] 6.2 Update the system prompt's tool enumeration (config default) for
      `store_memory`, `capture`, `inbox_read`, and the owner command set; keep
      the honest-capability framing
- [ ] 6.3 Test: production registry contains exactly the intended toolset with
      correct classes, tiers, and turn scopes (replaces the old "registry stays
      read-only" assertion deliberately)

## 7. Hygiene and verification

- [ ] 7.1 At sync/archive: apply the drafted Purpose texts from design.md's
      appendix to approval-gate, audit-log, agent-core, incident-triage,
      secure-deployment, memory-store, and capture-inbox (paste operation — no
      drafting left to that session)
- [ ] 7.2 After archive: re-read the merged approval-gate and audit-log specs
      end-to-end for contradictions (the scrutiny loop verified the deltas, not
      the merge). Note: "Schema is versioned" deliberately subsumes the old
      "readers SHALL be able to distinguish records across versions" clause via
      the prior-documents-remain-committed obligation — not an accidental drop
- [ ] 7.3 Full test suite green; `openspec validate --all` clean
- [ ] 7.4 Deploy-smoke checklist (deploy-verified items, ONLY after the explicit
      owner go): inbox/memory survive container recreation on rp5; SDK
      denied-call ToolUseBlock visibility confirmed against the live SDK; deploy
      also picks up the pending cryptography lockfile rebuild
- [ ] 7.5 Commit via git-commit-handler (publication rules; pre-commit hook);
      **no deploy — explicit owner go required**
