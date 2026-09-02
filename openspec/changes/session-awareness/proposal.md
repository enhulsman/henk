## Why

The North Star promises that Henk answers "what's the state of X / anything waiting on
me?" across the homelab *and the owner's working sessions*. Today the second half has no
tool behind it. The owner routinely runs five or six Claude Code sessions in herdr panes
on the workstation, and the questions that actually get asked from the phone — "is
anything blocked on me?", "did the henk session finish?", "what was I in the middle of?" —
can only be answered by walking back to the desk. Henk can say nothing about them.

This is roadmap item 4 of 5, and it is the last read-only item before the verb registry.
It is cheap on Henk's side: the deny-all ntfy instance, the topic-scoped credential
convention, the `tag:henk` → vps:2586 egress, and the poll-at-call-time read-tool pattern
all exist. The work is on the *workstation* side, where the trust lives: deciding,
before anything leaves the machine, which sessions are the owner's own and which fields of
them are metadata rather than content.

## What Changes

- **A workstation-side publisher** — `deploy/session-publisher/session_publisher.py`,
  stdlib-only Python, committed to this repo and deployed from it (the
  `homelab-docs-stamp.sh` precedent), run by a versioned systemd user timer. It reads the
  live estate through the existing CLIs — `herdr agent list` for which panes exist and
  their status, `claude-estate status --json` for the herdr↔age join — and **never opens a
  transcript**. `cclog` is deliberately *not* a runtime source: its `list` re-parses every
  transcript on every call (239 MB, ~1.2 s today, growing linearly) and its `title` field
  is conversation content by construction.

- **Publisher-side filtering, default-deny, two independent gates.** A session is
  published only if its **canonicalised** (symlink-resolved) working directory is under an
  explicitly allowlisted root **and**, when that directory is a git checkout, its remote
  owner is in an explicitly allowlisted owner set. Both gates must admit. An unset or
  empty allowlist publishes **nothing**. A deny list wins over the allow list at any depth,
  and a root that canonicalises to the home directory, a system directory, or a temp root is
  refused at config load, because a cwd under `/tmp` has already been observed to hide a
  work checkout inside a scratch worktree path.

- **Every published session is the same four keys.** The publisher emits `project` (the
  root's configured label, not a path), `status`, `age`, and the herdr pane id — nothing
  else, under any configuration. The publisher's configuration is a closed schema, so
  there is no setting that adds a fifth key; an unrecognised key is refused at load with
  the key named. **No absolute path ever leaves the workstation**, and neither do herdr
  workspace or tab labels, which are human-authored and encode work context.

- **No text from a conversation leaves the machine, in v1, at all.** An earlier draft of
  this change let the owner opt a root into publishing its session title and git branch,
  scrubbed and length-capped. That whole path is deferred: it is where every hard review
  finding landed, it is the only place model-authored text would reach a turn in which
  Henk holds write verbs, and removing it leaves a v1 in which every value Henk renders is
  shape-constrained. The full opt-in design is preserved in `design.md` under *Deferred:
  session titles* and belongs to a **`session-titles` follow-up**, whose first task is a
  finding this review turned up: the taint flag Henk reads at the start of a turn cannot
  refuse a write later in that same turn, so same-turn refusal has to be built before any
  untrusted text is rendered. This is the one scope narrowing the owner can reverse before
  apply.

- **An opt-in aggregate for non-allowlisted sessions** — `unlisted: {count, blocked}`
  and nothing else — off by default. It exists because "a session is waiting on you" is
  exactly the question the owner asks, and a bare count carries no client data; it is
  off by default because default-deny means the owner turns it on.

- **A new deny-all ntfy topic on the vps** (`henk-sessions`), a new **write-only** ntfy
  user for the publisher whose token lives on the workstation under the
  `~/.config/<consumer>/<name>-token` convention, and a **read-only** grant for Henk's
  existing ntfy user on that one topic. The publisher does **not** reuse the herdr
  notify plugin's shared write token. Henk's single `NTFY_TOKEN` gains one read scope; no
  new secret enters the container.

- **Snapshots, not events.** Each publish is the full current state in one JSON body
  within ntfy's ~4 KB message limit, published when the state changes and at least every
  15 minutes as a heartbeat **to the record** — the attention contract bans heartbeats
  to the owner, not to a topic only Henk and the admin read. Full-state snapshots make
  replay idempotent, so Henk needs no cursor and no checkpoint.

- **Henk reads at call time, not by stream.** A new `sessions_read` tool (class:
  read-only, no parameters — recipient, topic, and identity come from configuration per
  North Star principle 4) polls the topic over the existing vps:2586 egress and renders the
  newest snapshot. It is a **two-stage poll**: a short first poll over the staleness window
  answers the common case in one request, and only when that finds no usable snapshot does
  a second poll widen to the full lookback. Before either request, Henk consults its own
  label allowlist — an empty allowlist issues **no request at all** and says so. No second
  long-lived intake, no liveness watchdog, no new background task in a runtime that already
  juggles three. The streaming subscription remains the recorded upgrade path if a proactive
  "blocked for 30 minutes" message class is ever authorised.

- **Freshness is part of the result, never hidden.** The workstation is a gaming PC and
  WSL2 halts when Windows sleeps, so a stale or absent snapshot is *normal*. The result is
  a **gate, then three parts**: selection runs first, and a check it fails ends the result
  with its own sentence — nothing in scope, the backend failed, nothing published in the
  lookback, snapshots found but none usable, an unrecognised schema. When selection passes,
  the result is exactly one headline stating how fresh the snapshot is, exactly one body,
  and one optional notes line carrying what was dropped and why. None of them can read as
  "no sessions" unless that is what the snapshot says.

- **Tool result text stays out of the audit log**, asserted not implemented: the new tool
  is not in the result-capturing set, so no rendered snapshot reaches an audit record.

- Not in scope, recorded so their absence does not later read as an oversight: **no
  proactive messages** derived from session state (an attention-contract question for its
  own change); **no mutations** — no sending input to a pane, no `--resume`, no parking
  (roadmap item 5 material, and herdr's socket is not reachable from rp5 anyway); **no
  transcript-derived text** in any field, without exception, including session titles,
  branch names, and cclog's `--summary`; **no herdr
  `agent explain`**, whose `region_preview` is raw pane text; **no event-driven trigger
  via the herdr plugin bus** in v1 — the timer is the single install path, and herdr's
  remotely-versioned status rules flap enough that a per-change trigger needs a debounce
  this change does not want to own yet.

## Capabilities

### New Capabilities
- `session-awareness`: the workstation session-state feed — the snapshot contract, the
  publisher's two-gate session allowlist applied to every reported path, its closed
  configuration schema and fixed four-key session object, the change-or-heartbeat publish
  policy, and Henk's `sessions_read` tool: its own default-deny label allowlist, its
  selection gate, its freshness headline, and its audit exclusion. Transcript-derived free
  text is deliberately out of v1 scope and belongs to the `session-titles` follow-up.

### Modified Capabilities
- `secure-deployment`: the topic-scoped ntfy credential requirement gains a fourth scope
  (read on the sessions topic) and is restated whole; a new enumerated-surface requirement
  records that the sessions topic adds no published port, no listening socket, no ACL or
  egress grant, and no new secret in the container, and that the publisher's write-only
  credential is held on the workstation only.

## Impact

- **Code**: one new read tool `henk/tools/sessions_read.py` with a snapshot parser and
  renderer; registration gated on `sessions.enabled`; a `sessions` config section; the
  prompt composer's spelled-out tool-count table extended past twelve (rp5 registers twelve
  tools today with docs and reminders enabled, and a thirteenth raises `KeyError` by
  design). Workstation side: `deploy/session-publisher/` with the publisher, its systemd
  user units, and an example config using placeholder paths. The Dockerfile copies only
  `henk/`, so publisher code is never in the image.
- **Config**: five new keys across two sections — `sessions.enabled` (default **false** —
  there is host and vps provisioning to stage, the `homelab_docs` precedent),
  `sessions.topic` (`henk-sessions`), `sessions.stale_after_seconds` (**1500**),
  `sessions.lookback_seconds` (21600), and `personal_data.session_project_allowlist`
  (**empty**, which surfaces nothing and logs a startup warning). Each must default safely
  through `Config.from_dict`, since rp5's `config.yaml` is skip-worktree'd; the pinned
  `PersonalDataConfig` field-set test is updated deliberately for the new key. Reuses
  `endpoints.ntfy.base_url` and its timeout; no new timeout key, no new secret key.
- **Deployment (vps, owner-gated, real terminal)**: `ntfy-provision create` for the
  publisher user with `henk-sessions:wo`, `ntfy-provision grant henk henk-sessions ro`,
  probe both; `token-place` for the publisher token on the workstation; enable the user
  timer. Recorded in `~/.claude-config/tooling-backlog.md` at apply time.
- **Workstation prior art to reckon with**: two existing paths already carry session
  titles, absolute cwds, and work repo names off this machine — the herdr `ntfy-agent-notify`
  plugin (to the owner's phone) and the daily `digests/wsl.json` rsync (to the owner's own
  vps runner). Neither feeds an agent with tool reach; this feed does, which is why it is
  held to a stricter bar. Tightening those two is *not* in scope and is recorded as a
  follow-up in the design.
- **Publication safety**: no real cwd, title, workspace label, or tab label from the live
  estate appears in this repo — fixtures use placeholder paths and labels; `notes/*.md`
  record classification *counts* and allowlisted project labels only. The example config
  ships with placeholder roots and owners.
- **Docs**: README tools table; `/docs-update` for the new ntfy user and topic in
  `services/monitoring.md` and the publisher in `devices/workstation.md`.
- **Known collateral**: new config keys shift line numbers cited in
  `openspec/changes/owner-acknowledgement/proposal.md`; re-grep at close-out, as read-depth
  did. The `tests/test_query_registry.py` archive-path fix made while preparing this
  proposal restored the suite to green (1896 passed, 12 deselected) and is the baseline
  this change starts from.
