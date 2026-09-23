## Why

An independent review of Henk's triage, and a hand investigation of the same incident,
found that triage quality is capped by **evidence and framing, not by model strength**.
The hand investigation's write-up is
`notes/2026-09-23-vps-swap-incident-findings.md`, and it serves as the ground truth.

The worked example is the `HenkSwapPressure` handoff from the morning of 2026-09-23.

**What went wrong:**
- **The culprit was invisible to every query.** It was a host systemd unit,
  `/system.slice/dmesg.service`, whose journal scan pulled about 1 GB into page cache
  after an rsyslog upgrade. The container query sees neither host units nor per-container
  memory.
- **The handoff misread the trend.** The query renderer discards the times of the
  extremes (`henk/tools/query_renderers.py:153-161`), so the handoff read a one-minute
  step as "rising steadily".
- **The handoff blamed the wrong branch.** The fullness branch fired (A=98.39), but the
  registry labels that branch "NOT the rule's trigger" (`henk/tools/query_registry.py:425-436`).
  The rule is `A or B`, so the alert value cannot say which branch fired. Only checking
  both measurements can.
- **Failures are mishandled.** An API error that the CLI renders as text reaches the
  owner as if it were the triage message. An empty reply reaches the owner as silence
  (`henk/agent/core.py:375`).

The owner wants better triage, and eventually a stronger triage model, but only once
evidence shows it helps. That evidence needs two things: the evidence and framing fixes,
and a way to replay real triages against a chosen model and grade the results.

## What Changes

**Evidence (homelab-tools)**
- The trend and DNS summaries carry the **time of every figure**.
- Thresholds carry their rule's pinned **`for` window**.
- `swap_used` is treated as one of the rule's **two branches**. Swap results say both
  branches need checking.
- `container_state` gains per-container working-set **memory and swap**, with Docker's
  auto-generated names annotated as ephemeral.
- `container_state` gains a **restart count**, `resets()` of the CPU counter, which was
  **measured on both rp5 and the vps** on 2026-09-23. It replaces the "not observable"
  caveat.
- rp2 moves into the domain as an explicit `Unavailable`, including for
  named-container follow-ups. Aspect-level holes are rendered in place.
- **Two new measuring queries:**
  - `memory_movers` ranks the cgroups whose working set moved most in the window,
    **host systemd units and containers together**, each with its peak time.
  - `host_service_state` reports `failed` and `activating` units on the vps, with sample
    fractions, and proves that it ran. rp5 and rp2 are explicitly unavailable, because
    their node-exporters lack the systemd collector.

**Framing (incident-triage, agent-core)**
- The framing tells the model to:
  - check **every branch** of the rule;
  - use the **shortest window reaching back past the alert's notification time minus the
    rule's `for`**;
  - name host units as well as containers;
  - write "**evidence not available: X**" rather than infer;
  - treat memory as **context, not an override of evidence**;
  - consult `homelab_docs` when it is registered.
- Each incident states its notification time (labelled as such, not as onset) and its receive time.
- **Block markers are neutralised** in every string placed inside the untrusted block.
- The arc and its check are unchanged. The framing stays after the block and is
  defence-in-depth only.

**Endings (incident-triage, agent-core)**
- A triage's ending is classified from the SDK's **structured signals first**:
  `AssistantMessage.error`, `stop_reason == "refusal"`, and `ResultMessage.is_error` and
  `api_error_status`. Only then is it classified from text.
- Error-rendered text is never delivered. Announceable incidents get an honest
  incomplete-triage notice instead of silence or an API error string. The notice
  carries no refusal category, because the SDK drops it.

**History (triage-handoff, incident-triage)**
- Handoffs published in event-started sessions are **retained locally**, up to 500 or
  90 days, where today they live only in ntfy's 72 h cache.
- Every event turn gets a bounded **digest of related handoffs**: same identity, same
  rule, or same node. It sits inside the untrusted block, labelled as prior model
  output, and never reaches owner sessions or recall.
- A recurrence carries the prior handoff's content, which counts toward the digest's
  6,000-character total.

**Memory in event turns (agent-core, memory-store)**
- Both namespaces are recalled into event turns.
- The turn stays tainted, and writes stay refused.
- design.md D7 states the safety argument and its residual risks accurately:
  - event turns already reach private-data tools;
  - payload text can be third-party-influenced;
  - handoff-to-handoff propagation resets an injected instruction's age;
  - memory can reach the vps ntfy cache via a handoff;
  - the same-turn taint gap from NORTH-STAR row 4b.

**Replay and grading (new capability: triage-replay)**
- **Recording.** Each triage is recorded to the audit volume: the composed content,
  profile, every tool call with its result, the reply and the ending. Recordings are kept
  for 30 days, plus the backup's four-week snapshot rotation. They are never committed to
  the repo.
- **Replay.** An owner-invoked CLI replays a recording against a chosen model and
  effort. It runs as `docker compose run --rm --no-deps` from the checkout directory,
  with a separate `CLAUDE_CONFIG_DIR`, never as `exec` inside the live container.
  Recorded results are served back, and an unrecorded call gets an explicit error
  result. The hook and gate stay in force, and no tool can originate a network request.
- **Grading.**
  - Side-by-side output for the owner.
  - A no-tool judge on `claude-fable-5-1` scores against a versioned rubric, blind to
    models, and uses a verified reference when a case has one.
  - **The 2026-09-23 incident is the first graded case.** The audit record keeps tool
    names only, so the case is rebuilt from preserved material:
    - the ntfy event and handoff;
    - a Prometheus capture of the whole closed query-argument space at several
      evaluation times across the triage's window. The main session runs it **soon; the
      hard limit is 2026-10-07 06:30Z**.

    It is composed with no recall and no digest. Only Prometheus expressions are served,
    with a drift check. `endpoint_history`, the targets API part of `scrape_targets`,
    `homelab_docs` and the other tools return "unavailable in reconstruction". It is graded against the notes' ground truth.
    It measures the **new** renderers, and says so. Reference cases live outside the
    recording pruning, bounded by count.
- `--recompose` is **deferred**.

**Triage profile (agent-core)**
- `agent.event_model`, `agent.event_effort` and `agent.event_thinking` configure the
  triage profile. Each defaults to its chat value, so shipping changes nothing.
- The profile is recorded **from the factory that built the session**, so owner
  follow-ups in a triage session record the triage profile.
- The switch to Opus is **not** part of this change.

**Audit (audit-log)**
- Schema **v5** adds `profile`, `effort`, `recording_id` and `prior_handoff_ids`. Every
  v4 field is unchanged.

**Config**
- The new keys are the three profile keys, `triage_recording.enabled`,
  `replay.judge_model` and `replay.judge_effort`. Every other bound is a module constant.

**Non-goals, named so their absence is not read as an oversight.** Each is either a
homelab follow-up or a separate decision:
- making `HenkContainerRestarting` able to fire (it can now use `resets()`);
- enabling the systemd collector on rp5 and rp2, the restart metrics on the vps, and a
  node_exporter upgrade on the vps;
- switching the triage model;
- any owner-facing handoff-history tool;
- a rule-state query;
- automatic replays;
- classifying *owner*-turn endings.

## Capabilities

### New Capabilities
- `triage-replay`: per-triage recording and its retention; the replay entry point; the
  replay registry; the structural inability to publish, send, or write live state;
  side-by-side comparison; the no-tool judge with a versioned rubric and verified
  references; reconstructed cases.

### Modified Capabilities
- `homelab-tools`: the closed enum gains `memory_movers` and `host_service_state`; the
  rule-state exclusion is restated; the domains change; summaries carry times;
  thresholds carry `for`; the swap branches; container memory, swap and restarts; rp2 and
  aspect holes.
- `incident-triage`: recurrence content; method framing; marker neutralisation; incident
  times; ending classification and the incomplete-triage notice.
- `triage-handoff`: local retention of event-session handoffs; the related-handoff
  digest; retained handoffs stay inside the triage path.
- `agent-core`: event-turn composition; the triage profile, recorded from the factory.
- `memory-store`: recall reaches event turns; recall renders only the memory store.
- `audit-log`: schema v5; profile on every session record; evidence links on triage
  records.
- `secure-deployment`: the handoff archive shares the store; recordings and replay stay on
  the audit volume, and replay runs as its own one-shot container.

## Impact

- **Overlap with in-flight `owner-acknowledgement`.** That change ADDS *Owner agent turns
  are bracketed by the working indicator* to agent-core. This change MODIFIES *Turns are
  typed and event turns carry triage framing* and ADDS *Event sessions run on the triage
  profile*. Those are different requirements, so they archive in either order. Both edit
  `henk/agent/core.py`'s event path. Whichever lands second re-greps its citations, and
  task 9.4 keeps "event turns are not bracketed" true for event-factory sessions.
- **Tests inverted on purpose:**
  - `tests/test_recall.py:208,217`;
  - `tests/test_query_dispatch.py:155,196`;
  - `tests/test_query_registry.py:528-530`;
  - `tests/test_query_renderers.py:918`.

  They are listed in tasks.md.
- **Code:**
  - `henk/tools/query_registry.py` and `query_renderers.py`;
  - `henk/agent/triage.py`, `core.py`, `session.py` (the optional `ending()`) and
    `sdk_session.py` (the transcript and ending observers);
  - `henk/tools/publish_handoff.py`;
  - `henk/store/` (the `handoffs` table);
  - `henk/audit/` (v5);
  - new `henk/replay/`;
  - `henk/config.py`, `config.yaml` and `henk/runtime.py`.
- **Deployment:**
  - New files on the existing audit volume only.
  - No new volume, port, socket, grant or secret.
  - Replay runs as a one-shot `compose run` container with its own memory limit.
  - `operations/backup-recovery.md` gains a row.
- **Time-critical step:** run the Prometheus capture for the first case soon. The hard
  limit is 2026-10-07 06:30Z (group 1b). The raw material was preserved on 2026-09-23 (task 1.9).
- **Publication safety:**
  - Recordings and the rebuilt case stay on rp5.
  - Fixtures use placeholder hosts and units, and RFC 5737 addresses.
  - The findings notes contain no addresses.
