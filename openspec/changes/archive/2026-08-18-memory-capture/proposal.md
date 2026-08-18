# Memory & Capture

## Why

Henk forgets everything: session context evaporates after the idle window ("context
lost after an hour"), nothing the owner tells him survives a restart, and a passing
thought has nowhere durable to land. NORTH-STAR.md (settled 2026-08-07) names
memory + capture as roadmap change 1 and evolves the approval posture from binary
(every mutation prompts) to a three-tier model where the tier is a property of the
*named action* — with receipts always. This change delivers that, plus a verified
audit defect fix that standing authorization turns from cosmetic into critical:
approval decisions are never written to audit records today, and an agent that
acts without asking must be *more* accountable, not less.

## What Changes

- **Memory store + recall.** A capped, type-namespaced SQLite store of short
  natural-language facts on the existing backed-up audit volume. Writes are
  explicit: owner commands (`/remember`, `/forget`, `/memories`) and an agent
  tool (`store_memory`, registered and receipted). Recall is the full store
  rendered as a bounded, delimited markdown block injected at the first owner
  turn of each session — continuity by rebuild, not by longer sessions
  (`idle_timeout_seconds` stays at 3600). The rendered block's hash lands in the
  session's audit record.
- **Capture inbox — Henk's first production mutating tool, standing tier.** A
  `capture` tool and a `/capture` owner command append thoughts to a durable
  Henk-native inbox behind a thin storage-backend seam, so a future
  personal-inbox service (its own change; decision recorded in design.md) swaps
  in without touching agent logic. Read-back via an `inbox_read` read-only tool
  and `/inbox`, `/inbox all`, `/inbox done <id>` commands; oldest-first drain,
  no eviction ever.
- **Approval gate → three-tier authorization model with turn scope.** **BREAKING**
  (spec-level reversal, owner-blessed per NORTH-STAR.md): the "v1 SHALL ship no
  mutating tools in the production tool registry" requirement is replaced.
  Mutating tools carry a per-named-action authorization tier — **standing**
  (execute without prompting; receipt always), **per-instance** (inline
  approval, single-use, argument-bound, fail-closed), **never** (= unregistered,
  default-deny) — and a **turn scope** enforced with session taint, so no
  out-of-scope mutation can be driven from an event turn or from a session an
  event turn has touched. `capture` and `store_memory` ship standing,
  owner-turn-only. A config kill-switch can demote standing to per-instance;
  nothing in config can widen authorization.
- **Receipts fix (verified defect) + durable receipts.** Approval decisions are
  accepted by the audit record builder but never passed by the agent core —
  `approvals` is always `[]` while the audit-log spec requires it. Every
  mutation path — gate decisions and mutating owner commands alike — now writes
  an append-only `authorization` record at decision time (surviving hard kills
  and independent of event intake being enabled), plus the aggregated
  `approvals[]` in session records; `tool_calls` entries gain an `executed`
  flag; `schema_version` bumps to 3 with a published schema document.
- **Adjacent gate defects swept in** (verified in code): (a) a mutating
  invocation during a cap-suppressed (non-announceable) event turn would send
  the owner an approval prompt with zero context — per-instance invocations
  there are now auto-denied without prompting (suppress the mutation, not the
  prompt); (b) the approval prompt renders model-chosen arguments raw (`{v!r}`,
  undelimited) — replaced by resolve-then-confirm with delimited, truncated
  argument rendering; (c) gate concurrency is fail-closed (standing never
  touches the pending slot; a concurrent per-instance request resolves
  `rejected-busy`, never an unhandled error).
- **Spec hygiene carried by this change:** fill the placeholder Purposes of the
  specs it touches from NORTH-STAR.md (texts drafted in design.md's appendix).

## Capabilities

### New Capabilities

- `memory-store`: durable owner-facts memory — capped, type-namespaced,
  length-bounded store; explicit receipted write paths (owner commands +
  `store_memory`); bounded dump-all recall injected at the first owner turn;
  structural protection from untrusted event input; loud-but-honest failure
  modes.
- `capture-inbox`: durable thought capture — `capture` tool and `/capture`
  command appending to a Henk-native inbox behind a swappable backend seam;
  oldest-first read-back (`inbox_read`, `/inbox`, `/inbox all`,
  `/inbox done <id>`); no eviction; loud-but-honest failure modes.

### Modified Capabilities

- `approval-gate`: binary mutating-always-prompts model becomes the three-tier
  named-action model plus turn scope with session taint; the "no production
  mutating tools in v1" requirement is deliberately reversed; suppressed event
  turns auto-deny per-instance invocations instead of prompting; gate
  concurrency fails closed; approval prompts show a resolved, delimited,
  truncated action instead of raw args.
- `audit-log`: mutation receipts durable at decision time for every path
  (model-initiated and owner commands), independent of event intake; approval
  decisions threaded into session records (defect fix); `executed` flag on tool
  calls; `memory_hash`; schema-version history consolidated; `schema_version` 3
  with published schema, prior versions remain readable.
- `agent-core`: owner command dispatch extends beyond `/new` to the full
  memory/inbox command set; turn composition gains the recall-block
  cross-reference (first owner turn per session; never event turns).
- `incident-triage`: the read-only-toolset requirement reworded for the tiered
  registry (mutating tools exist but are out of scope in event turns); the
  cadence requirement's "suppressed from Signal only" gains the mutation
  exception (a suppressed incident can never place an approval prompt on the
  channel).
- `secure-deployment`: the durable-state surface enumeration gains the
  memory/inbox store on the existing backed-up audit volume — no new volume,
  port, socket, ACL grant, or secret.

## Impact

- **Code:** `henk/gate/approval.py` (tiers, turn scope/taint, prompt rendering,
  concurrency, suppressed-turn denial), `henk/agent/core.py` (receipts
  threading, session taint marking, command dispatch, recall injection),
  `henk/agent/permission.py` (tier/scope-aware decisions), `henk/audit/`
  (authorization records, schema v3), new `henk/store/` (SQLite store, memory +
  inbox repositories, `InboxStore` seam) and the two tool modules,
  `henk/config.py` (`store` + `audit` sections, caps, demotion flag),
  `henk/runtime.py` (unconditional audit construction), system prompt.
- **Storage:** one new SQLite file on the existing backed-up audit volume — no
  new volume, port, listening socket, ACL grant, or secret (spec'd in
  secure-deployment).
- **Security posture:** first production mutating tools. Both standing-tier
  verbs are append-only writes to Henk-local stores, owner-turn-only under
  session taint, receipted at decision time. Inherited posture (closed toolset,
  default-deny hook, owner-only outputs) unchanged.
- **Not in scope:** reminders (change 2), the personal-inbox service (own future
  change), ambient memory extraction (pruned growth path), container/service
  restart verbs (change 5), Obsidian vault writes (off the table). Deliberate
  deferrals are consolidated in design.md's ledger.
