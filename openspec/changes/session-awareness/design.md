## Context

Henk's read surface reaches every homelab box and none of the owner's working sessions.
The sessions live on the workstation — a WSL2 distro on a gaming PC — as herdr panes, most
of them Claude Code, and the questions the owner asks about them from the phone are
status questions: is anything blocked, did that one finish, what was I doing. Nothing
reaches rp5 today except two things that should not be the model for this feed (see D13).

Four measured facts shape everything below. They were probed on 2026-09-02 and are the
record this design is built on; where a later probe contradicts them, the probe wins and
the contradiction is written into `notes/`.

**herdr is the authoritative live view, and it is metadata plus one content field.**
`herdr agent list` emits one JSON line per invocation (there is no `--json` flag; JSON is
the only output) with, per agent: `agent_status` from the closed set `idle | done |
working | blocked | unknown`, `cwd` and `foreground_cwd` as absolute paths, `pane_id`,
`tab_id`, `workspace_id`, `agent_session.value` (the Claude session UUID), and
`terminal_title_stripped`. That last field is the **model-generated session title** pushed
as an OSC title — natural language derived from the conversation. For a work session it
carries the work subject verbatim. It is not published by this change at all (Part E /
*Deferred: session titles*). herdr carries **no timestamp**: `revision` and
`state_change_seq` are monotonic counters. Status detection is screen-scraping against a
remotely-versioned rule manifest; `unknown` is documented as "does not prove completion",
and overlays can misread as `unknown` or `blocked`. herdr's documentation
(`~/.claude-config/herdr.md:72`) describes pane ids as "opaque stable handles", says
"Closed ids are not reused", and says a moved pane "gets a new id"; it deliberately
documents no pane-id grammar and warns against deriving one.

**Age is already solved next door, without reading transcripts.** `claude-estate status
--json` joins `herdr agent list` to a last-activity age per pane (`age_s`) by tailing the
last 256 KB of the session's JSONL for its final `timestamp` — because Claude Code rewrites
the file with untimestamped bookkeeping long after the last turn, so mtime lies. Its
payload (`pane_id, tab_id, workspace_id, kind, session, status, title, cwd, age_s, class,
resume`) is nearly the shape a publisher wants, minus the filtering and minus three fields
that must never leave the machine (`cwd`, `resume`, and `title`). It declares itself "NOT A
SECURITY BOUNDARY". It is a data source here, not a gate.

**cclog is the wrong runtime source.** `cclog list --json` parses every line of every
top-level transcript on every call — 186 sessions, 239 MB, ~1.2 s today and linear in
transcript bytes — and its `title` is either a model summary or the literal first user
message (slash-command payloads included). `cclog view --summary` is a pure local parse
but its every field (`task`, `user_messages`, `last_assistant`, file paths) is
conversation content. Neither belongs in a feed to an agent.

**Work and personal are already machine-distinguishable on this workstation, twice
over.** By path: work checkouts live under one work subtree of the projects directory; personal
and homelab projects live directly under the projects directory, the config-repo directory,
and the documents directory.
By git identity: `~/.gitconfig` routes `user.email` through `includeIf` rules keyed on
`gitdir:` and on the remote owner segment, so `git -C <cwd> config user.email` resolves the
whole chain to one value per checkout. Two edge cases break a naive path rule and motivate
using both signals: a checkout *outside* the work subtree whose remote owner is a
third-party org, and a scratch worktree under `/tmp/…` whose path embeds a work checkout.
herdr workspace and tab labels (a workspace labelled for work, tab labels naming
client deliverables) are human-authored free text —
a usable *deny* hint, never an *allow* signal, and not published.

The vps ntfy instance is `auth-default-access: deny-all`, retains messages 72 h, limits a
message body to ~4 KB (larger publishes become attachments), and is reachable from WSL at
`vps:2586` over the tailnet via mirrored networking. Henk already reaches vps:2586 for
`henk-events` and `henk-handoffs`; `NTFY_TOKEN` is one credential with per-topic grants.

## Goals / Non-Goals

**Goals:**

- Henk can answer "what sessions exist, which are blocked, and which are idle and since
  when", for the owner's own sessions only, from a snapshot whose every rendered value is
  shape-constrained.
- **Filtering happens on the workstation, before publish**, is default-deny at two
  levels (which sessions, which fields of them), and is inspectable without publishing
  (`--dry-run` is the Tier W review instrument). Henk then applies its own default-deny
  label gate on top (D1), because the source estate mixes personal and work sessions.
- No absolute path, workspace label, tab label, session UUID, branch name, session title,
  or any other transcript-derived or human-authored free text ever leaves the workstation
  on this feed.
- Zero new infrastructure surface on Henk's side: no port, no socket, no ACL or egress
  grant, no new secret in the container. One read grant on one new deny-all topic.
- A stale or absent snapshot is reported *as* stale or absent, never as "no sessions", and
  a result that shows nothing says which population it dropped and why.
- No real cwd, title, or label from the live estate enters this repo — fixtures, example
  config, and `notes/` use placeholders and counts.

**Non-Goals:**

- **Any proactive message** derived from session state. "Blocked for 30 minutes" is a
  plausible future message class, but admitting one is an attention-contract decision
  with its own change, and the poll-at-call-time design here does not preclude it.
- **Any mutation**: sending input to a pane, resuming, parking, nudging. Roadmap item 5,
  and herdr's Unix socket is not reachable from rp5 in any case.
- **No transcript-derived text in any field, in v1, without exception** — no session
  title, no branch name, no "just the last assistant line", no cclog `--summary`. The
  opt-in design that would admit some of it is preserved below under *Deferred: session
  titles* and belongs to the `session-titles` follow-up, not to this change.
- **`herdr agent explain`**, whose `evidence.region_preview` is raw pane text.
- **Event-driven publishing via herdr's plugin bus** in v1 (D7 records why and the
  upgrade path).
- **Tightening the two pre-existing off-workstation paths** (D13). Recorded, not done.
- **A second long-lived ntfy subscription in Henk** (D9).
- **Making `pane` actionable** (D4). It is published as an addressing token the owner can
  read, not as a handle anything can act on.

## Decisions

### D1 — The publisher is the trust boundary; Henk gates again and renders untrusted data

Filtering lives primarily in the workstation publisher because that is where the
information exists to make the decision — the canonical path, the git identity, the
owner's allowlist — and because North Star principle 3 says so. Henk receives a snapshot,
validates its shape, renders a fixed set of shape-constrained fields, and **drops anything
it does not recognise**: unknown top-level keys, unknown per-session keys, `status` values
outside the documented set (rendered `status not recognised`), values whose shape fails
validation (the session is *unusable*), and a `schema` other than `1`, which is terminal
and has no fallback (D10, G4).

**Henk keeps its own default-deny gate.** The topic carries only the publisher's
post-filter output, and yet Henk applies a second label allowlist —
`personal_data.session_project_allowlist` — before anything is rendered. This is
compliance with the existing default-deny personal-data scoping requirement, declared in
this change's own spec the way the homelab-docs corpus allowlist was, and the reason is
the same: **the source estate mixes personal and work sessions**, so the requirement
applies and being downstream of one filter does not discharge it. An empty or unset
allowlist surfaces nothing (D10, G0) and registers with the same startup WARNING as
`todo_read` (`henk/tools/__init__.py:144-146`). Stated limit: the label gate catches a
widened publisher root only when that root introduces a *new* label; a re-pointed or
relabelled existing root is caught instead by the dry-run review (task 8.5) and by the
publisher's per-run journal line, which logs the admitted label set alongside the counts.

**The bound on Henk's reach, stated truthfully.** An earlier draft of this decision said
Henk "has no verbs to act with". That is false and is recorded as superseded: Henk holds
standing-tier writes to its own stores (`henk/tools/memory.py:36-38`,
`henk/tools/capture.py:50-52`) and an owner-only outbound channel, and a `store_memory`
write persists and re-enters future prompts. The true v1 bound is narrower and holds for a
different reason: **nothing this tool renders is free text**, so no attacker-authored
sentence reaches a turn in which those verbs are available. The titles follow-up must add
invocation-time taint before it exercises that bound.

*Rejected:* filtering only in Henk (a denylist of project names applied on rp5). The wall
must be where the data is, and a denylist applied after the fact means the data already
left. *Also rejected:* filtering only in the publisher, on the argument that the topic is
already post-filter — see the compliance paragraph above.

### D2 — Sources are `herdr agent list` and `claude-estate status --json`; never cclog, never transcripts

The publisher runs `herdr agent list` for the authoritative pane set — existence,
`agent_status`, `cwd`, `foreground_cwd`, `pane_id` — and `claude-estate status
--json` for `age_s`, joined on `pane_id`. When `claude-estate` is absent or exits
non-zero, `age_s` is `null` for every session and the snapshot says `age_source:
"none"`; a session's status is still published. When `herdr` is absent, unreachable, or
emits JSON missing a required field (`pane_id`, `agent_status`, `cwd`), the publisher
publishes **nothing** and exits non-zero — a partial estate presented as whole is the
failure mode to refuse.

The user instruction named cclog as a candidate source. It is rejected at runtime for the
two measured reasons in Context — cost and content — and because `claude-estate` already
solves the one thing cclog would have been for (age) with a bounded tail read that the
publisher does not have to own. cclog remains what it is: the tool for reading *finished*
sessions by a human or by a Claude session with the owner in the loop.

*Rejected:* computing age in the publisher by tailing the JSONL itself. It works — it is
what claude-estate does — but it is reading transcripts, and the instruction not to is a
boundary worth keeping literal even when the read is timestamps-only.

### D3 — Two independent gates decide whether a session is published; both must admit, on both reported paths

A session passes the **root gate** when its canonicalised working directory — after
`realpath`, so symlinks and worktree indirections resolve — is under an entry in
`allow_roots` and under no entry in `deny_roots`. It passes the **owner gate** when either
the directory is not inside a git work tree, or `git -C <cwd> remote get-url origin` names
an owner in `allow_owners` (matched on the owner path segment for `https://`, `git@host:`,
`ssh://`, and host-alias forms alike). A checkout with no `origin` fails the owner gate.

**Both gates are applied to `cwd` and, when `foreground_cwd` is present and different, to
`foreground_cwd` as well.** A session is admitted only when every reported path passes
both gates; the project label is chosen from `cwd`. herdr reports two paths and publishing
on only one of them would let a foreground excursion into a work checkout ride out on a
session admitted by its shell's cwd. The cost is named as a risk: a brief `foreground_cwd`
excursion makes a session blink out of the snapshot and back.

Every `git` invocation runs with a clean environment — `GIT_TERMINAL_PROMPT=0`,
`GIT_CONFIG_NOSYSTEM=1`, `-c core.pager=cat` — and a per-call timeout, so a hostile or
misconfigured checkout cannot hang the timer or prompt for credentials.

**Deny wins at classification, at any depth, and no allow root is refused at load for
containing a deny root.** An earlier draft refused an `allow_roots` entry that lay above a
`deny_roots` entry. That is recorded as superseded: it refuses exactly the configuration `deny_roots`
exists to express — a work subtree under an otherwise personal parent — and it bought
nothing, because deny is evaluated at classification on the canonical path anyway. A
container root with a deny root below it loads and classifies correctly.

**The publisher configuration is a closed schema.** `allow_roots` entries are `{path,
label}` tables; `label` is required, unique across entries, and must match
`^[\w:.-]{1,32}$` — the same shape Henk enforces at render, so a legal publisher config can
never produce a session Henk would call unusable. When several allowed roots match, the
**longest canonical prefix** admits and supplies the label. Any unrecognised key, at the
top level or inside an entry, is refused at load with the key named: a misspelled
`deny_root` would otherwise silently delete the only deny rule, and `fields` is refused
by name until the titles follow-up deliberately adds it back to the schema. The example
config and README use one entry per project; a container entry is legal, weaker, and
should be paired with `deny_roots` — README guidance, not enforced.

Configuration is also refused at load — the publisher exits non-zero and publishes
nothing — when any `allow_roots` entry canonicalises into the unsafe-root set: `/`,
`$HOME`, `/home`, `/root`, `/mnt` and every immediate child (so a WSL drive mount such as
`/mnt/c` is refused), `/tmp`, the system temporary root, `/var`, `/etc`, `/usr`, `/opt`,
`/proc`, `/sys`, `/dev`, `/run`, `/media`, `/srv`.

*Rejected:* path gate only (defeated by the third-party-org checkout outside the work
subtree, and by scratch worktrees under `/tmp`). Owner gate only (a non-git directory has
no owner; a personal repo can be cloned anywhere). herdr labels as a signal in either
direction (free text with no schema; auto-generated numeric tab labels carry nothing).

### D4 — Every published session is the same four keys

Every published session carries exactly: `pane`, `project` (the **configured label** of
the admitting root entry, never derived from the path), `status` (herdr's value verbatim),
and `age_s` (integer seconds or `null`). Nothing else, on any root, ever — the publisher
config has no field-selection key and the closed schema refuses one (D3), so widening this
set is a deliberate schema change rather than a configuration flag.

`pane` is herdr's pane id (e.g. `wE:p8`): **an opaque addressing token, published so the
owner can tell two same-label sessions apart. It is deliberately not actionable in v1, and
making it actionable is a roadmap item 5 decision, not a consequence of this change.** The
character set is not guessed from herdr's documentation, which declines to specify one
(`herdr.md:72`); task 1.1 records the observed set and Henk's shape regex is set from that
record.

Never published, on any root: absolute or relative paths, `foreground_cwd`, workspace or
tab ids or labels, `agent_session.value`, `resume`, `class`, herdr `revision` counters,
`terminal_title_stripped`, the current branch, the workstation hostname.

*Rejected:* deriving `project` from the cwd basename (leaks directory naming, and the
basename of a worktree is often the branch name). Publishing `session` UUIDs (an
identifier Henk cannot use and the owner would not want in a tool result). Publishing
`title` and `branch` behind a per-root opt-in — deferred wholesale, see *Deferred: session
titles*.

### D5 — The unlisted aggregate is a single opt-in pair of integers

When `publish_unlisted: true`, the snapshot carries `unlisted: {"count": N, "blocked":
K}` — how many live sessions failed a gate, and how many of those herdr reports as
`blocked`. Nothing else about them: no status breakdown beyond blocked, no ages, no
labels. When the key is false or absent, the `unlisted` object is absent from the
snapshot entirely, so Henk cannot distinguish "no unlisted sessions" from "not reporting
them" — which is the point; the owner who wants the distinction turns it on.

The rationale for having it at all: "is anything waiting on me" is the question, and a
blocked work session is waiting on the owner. Two integers carry no client data. The
rationale for default-off, and the reason it is **decided here rather than left open**:
the data axis is default-deny, and this is data that describes sessions the gates
refused — so it crosses the machine boundary only on an explicit owner decision recorded
at task 8.6.

**Why Henk's own counts are allowed while this defaults off:** this decision governs what
crosses the machine boundary into an agent; Henk's notes clauses (filtered, unusable,
skipped) describe records Henk already holds, rendered back to their owner, and cross no
boundary at all.

### D6 — One snapshot is the whole state, in one ntfy message under a byte budget

```json
{"schema": 1, "generated_at": "2026-09-02T10:40:00Z", "publisher": "session-publisher/0.1",
 "age_source": "claude-estate", "heartbeat_s": 900, "tick_s": 300, "sessions": [
   {"pane": "wE:p8", "project": "henk", "status": "working", "age_s": 42},
   {"pane": "w2:pW", "project": "config", "status": "idle", "age_s": 91000}],
 "unlisted": {"count": 2, "blocked": 0}, "degraded": {"dropped": 1}}
```

Top level: `schema`, `generated_at`, `publisher`, `age_source`, `heartbeat_s`, `tick_s`,
`sessions`, optional `unlisted`, optional `degraded`. `heartbeat_s` and `tick_s` are the
publisher's own publish cadence, published so Henk can tell whether its staleness bound is
even meaningful against that cadence (D10's drift clause).

The body budget is **3800 bytes** — ntfy's ~4 KB limit less headroom, so a snapshot is
never silently promoted to an attachment. With four shape-constrained keys per session
there is nothing to strip but sessions themselves, so the degrade is one step: drop
sessions from the tail of a list ordered `blocked`, `working`, then everything else by
ascending `age_s` with `null` ages last within that group, re-measuring after each drop,
and add `degraded: {"dropped": N}`. The key is absent when nothing was dropped; the older
boolean drop flag it supersedes is gone, because a count is strictly more useful and the
publisher now has one. A snapshot with zero sessions is a valid snapshot and is published
(D10 depends on that).

**Rollout rule.** Additive changes — a new optional top-level key, a new optional clause
input — keep `schema: 1`, because Henk ignores what it does not recognise. `schema` is
incremented only for a change that would make an old reader render something wrong. On any
increment, **Henk ships first**: rp5 learns the new schema before the workstation emits
it, so the window is "Henk understands more than it receives", never G4.

The message is published with `Title: session snapshot`, `Priority: min`, no tags, and
the default cache behaviour — it must be cached, because Henk reads the cache (D9).

### D7 — Timer-driven, change-or-heartbeat, stateful only in a hash

A systemd **user** timer runs the publisher every 5 minutes (`OnCalendar=*:0/5`,
`Persistent=false` — a missed tick while WSL was halted must not fire a stale snapshot on
resume; the next tick reads fresh). The tick interval is also configuration:
`tick_seconds` (default **300**) is what the publisher believes its own cadence to be, it
is published as `tick_s`, and it is coupled to `OnCalendar` by a comment in the unit file
and a line in the README — the two cannot be enforced against each other from inside the
process, so they are documented against each other in both places.

The publisher computes a *comparison key* from the **final, post-degrade** snapshot with
`generated_at`, every `age_s`, `heartbeat_s`, and `tick_s` removed, and publishes when the
key differs from the one stored at `$STATE_DIRECTORY/last.json`, or when the heartbeat is
due. Computing the key post-degrade matters: two runs that differ only in a session the
budget dropped are the same published state and must not republish. `unlisted` is part of
the key, so `unlisted.blocked` changing on its own does publish.

**The heartbeat fires when `elapsed + tick_seconds > heartbeat_seconds`** (default 900),
not when `elapsed > heartbeat_seconds`. The naive test makes a 900-second heartbeat on a
300-second tick fire one whole tick late — at 900 plus a tick — and that lateness is
exactly what Henk's staleness bound then has to absorb. So: 900 s elapsed publishes;
600 s does not.

Every run logs one journal line with the admitted label set and the classification
counts — admitted, denied, dropped — which is the artefact that catches a re-pointed or
relabelled root that the label gate cannot (D1).

Failure handling: a failed publish logs to stderr (the journal) and exits non-zero so
`systemctl --user status` shows a failed unit; there is no retry loop — the next tick is
the retry. A failed publish does **not** update `last.json`, so the heartbeat clock keeps
running against the last *successful* publish. `--dry-run` renders the snapshot and a
per-pane classification table (pane, allowed/denied, which gate and which reported path,
project label) to stdout and touches neither the network nor the state file.

The unit sets `TimeoutStartSec=60` and `StateDirectory=henk-session-publisher`; the
publisher reads `$STATE_DIRECTORY` and accepts a `--state-dir` override for tests, and
**fails loudly** rather than silently when the state directory is unwritable, because a
silent failure there turns every tick into a first run and republishes forever. Overlapping
runs are serialised with `flock`; every subprocess call has a timeout; the entry point
refuses to run on Python < 3.11 with a message naming the requirement, and §1 probes
`python3 -V` on the workstation before any of this is relied on.

WSL2 halts entirely when Windows sleeps; systemd user timers are confirmed working on
this distro (`systemd=true`, two custom timers already live, units versioned as symlinks
from `~/.config/systemd/user/` into the config repo). The publisher copies that pattern.

*Rejected for v1:* the herdr plugin bus (`pane.agent_status_changed`, already firing for
the phone-notify plugin). It is the natural low-latency trigger and is the recorded
upgrade path, but it fires on every flap of a remotely-versioned status classifier,
across every pane, and would need a debounce plus a timer *anyway* for the heartbeat —
two install paths for one feature. A Claude Code `SessionStart` hook (the digest
precedent) fires only when sessions start, which is the wrong event.

### D8 — Two credentials, each scoped to one operation on one topic, neither shared

The publisher authenticates as a **new** ntfy user with a write-only grant on
`henk-sessions` and nothing else; its token is placed via `token-place` at
`~/.config/henk-session-publisher/ntfy-token` (mode 600 in a mode-700 directory) and read
by the publisher from that path or from an env override. Henk's existing user gains a
**read-only** grant on `henk-sessions`; `NTFY_TOKEN` in the container is unchanged.
Both are minted, granted, and probed through `ntfy-provision` from a real terminal.

The publisher does **not** reuse the herdr notify plugin's shared `.env` token, which
three other workstation consumers already share: a compromised or leaked publisher
credential must be able to write one deny-all topic and nothing the owner's phone reads.

Topic names are not secrets on a deny-all instance (the homelab docs say so for the Henk
set), so `henk-sessions` is committed in config defaults and in this document.

### D9 — Henk reads the topic at call time, in at most two polls; no second intake

`sessions_read` reads the ntfy cache with `GET {ntfy.base_url}/{sessions.topic}/json?poll=1&since=<window>s`
using the existing bearer token and the existing `endpoints.ntfy` timeout, and it makes at
most two such requests:

- **Stage 1** uses `since=stale_after_seconds`. Every returned line is parsed as an ntfy
  frame; `message` frames are parsed as JSON and accepted as *candidates* only when the
  snapshot's `publisher` value starts with the constant prefix `session-publisher/`.
  Unparseable frames and foreign-publisher frames are **counted, not timed** — their
  server times are never used for anything, so a corrupt or hostile frame cannot make a
  stale answer look fresh.
- **Stage 2** uses `since=lookback_seconds` and runs only when stage 1 yields **no
  candidate at all**. It is skipped entirely when the two windows are equal, which keeps
  the common configuration at one request.

Before either request, Henk's own label allowlist is consulted: an empty allowlist issues
**no HTTP request** and returns G0 (D10). The response is stream-parsed, keeping only the
newest candidate, under a **1 MB byte cap**; on overrun the read stops, the newest
candidate so far is used, and the *cut short* notes clause is added so the owner knows a
newer snapshot may exist. The arithmetic says the cap is generous: one publish per tick is
the worst case, so six hours is at most 72 frames of at most 3.8 KB — about 274 KB.

The lookback default is 6 hours, long enough to say "last reported at 03:12" after a
night's sleep rather than only "nothing in the last hour".

*Rejected:* a second `NtfyEventStream` + `EventIntake` subscription. It exists and is
well-tested, but the runtime would gain a fourth independent task, a second liveness
watchdog, and a second checkpoint — for a feed whose consumer is a synchronous tool call
and whose messages are idempotent full-state snapshots that make a cursor pointless. The
streaming path is what a future proactive message class would need, and nothing here
prevents adding it then.

*Rejected:* `since=all` (72 h of cache per call), and a single-window poll at the lookback
(pays six hours of cache on every call to answer a question the fresh window almost always
answers).

### D10 — The result is a gate, then three parts

Selection runs first as a gate whose non-pass outcomes are terminal; when it passes, the
result is exactly one headline, exactly one body, and one optional notes line. Every
rendering below is verbatim tool output with no markdown; the marker beside each is the
literal substring by which a test pins that sentence.

*Gate.* Checked in order; the first non-pass ends the result.

| # | Check | Rendering (verbatim) | Marker |
|---|---|---|---|
| G0 | Henk's label allowlist empty — no HTTP request | `No sessions are in scope: the allowlist (personal_data.session_project_allowlist) has no entries, so nothing from the workstation can be surfaced.` | `has no entries` |
| G1 | any request fails, either stage — `ToolResult.failure` with one of the three existing sentences (`henk/tools/homelab_query.py:319-324`, extracted into a shared `backend_failure_reason(backend, exc)` helper that both tools call, so the sentences have one home): `ntfy timed out after 10s` · `ntfy returned HTTP 503` · `ntfy request failed: <reason>` | as quoted | `ntfy` |
| G2 | no `message` frame in either stage | `The workstation has not published a session snapshot in the last 6 hours.` | `has not published a session snapshot` |
| G3 | frames present after the full lookback, zero candidates | `3 snapshots were found in the poll, but none could be used (2 unreadable, 1 from an unexpected publisher).` | `none could be used` |
| G4 | newest candidate has `schema` ≠ 1 — no fallback | `The newest snapshot uses an unrecognised snapshot schema (2); the publisher and Henk are out of step.` | `unrecognised snapshot schema` |
| — | pass | continue | pass-through, exempt |

Selection: stage 1 polls `since=stale_after_seconds`; per frame, parse JSON → publisher
prefix (constant `session-publisher/`) → candidate. Stage 2 (`since=lookback_seconds`) runs
when stage 1 yields **no candidate**; skipped when the windows are equal. Unparseable and
foreign frames are counted, not timed. Age is `now − generated_at` on Henk's clock.

*Populations*, defined once as an **ordered partition**: **reported** = sessions in the
snapshot's `sessions` array. Each reported session is classified by exactly one test, in
order: the shape check first — a session failing it is **unusable** and is not tested
further; then the allowlist — a well-formed session whose label is not allowlisted is
**filtered**; the rest are **listed**. So reported = unusable + filtered + listed with no
overlap. **unlisted** is the publisher's own count of sessions it denied, disjoint from
reported. The body's "N sessions were reported" is the reported count.

*Part 1 — headline (exactly one).*

| State | Rendering | Marker |
|---|---|---|
| fresh (age ≤ bound) | `Workstation reported 4 minutes ago.` | `Workstation reported` |
| stale | `Last snapshot is 3 hours old; the workstation is probably asleep or the publisher has stopped (on the workstation: systemctl --user status session-publisher.timer).` | `probably asleep` |
| unknown | `Freshness unknown; the server received this snapshot at 2026-09-02 10:40 UTC.` | `Freshness unknown` |

*Part 2 — body (exactly one).*

| State | Rendering | Marker |
|---|---|---|
| reported > 0, listed = 0 | `5 sessions were reported, but none could be shown.` (the notes line says why: filtered and/or unusable) | `none could be shown` |
| reported = 0 | `No live sessions.` | `No live sessions` |
| listed > 0 | one line per listed session: label, status (or `status not recognised`), humanised age (or `age unknown`); ordered blocked, working, then ascending age, null ages last. Test rendering uses fixed placeholder labels containing no marker | pass-through, exempt |

*Part 3 — notes (`Notes: ` + clauses joined by `; `; present only when a clause applies;
fixed order; the caveat is always last and uses a comma internally).*

| Clause | Rendering | Marker | Suppressed when |
|---|---|---|---|
| ages | `last-activity ages were unavailable on the workstation` | `ages were unavailable` | — |
| degraded | `2 sessions were dropped for size` (count omitted if `dropped` malformed: `sessions were dropped for size`) | `dropped for size` | — |
| filtered | `2 sessions were filtered by Henk's allowlist` | `filtered by Henk's allowlist` | — |
| unusable | `1 session was dropped because its fields were unusable (the publisher may be broken or something else may be writing the topic)` | `fields were unusable` | never |
| unlisted (publisher) | `3 further sessions not shared (1 blocked)` (counts omitted if malformed: `further sessions not shared`) | `further sessions not shared` | — |
| cut short | `the poll was cut short at the read budget, so a newer snapshot may exist` | `poll was cut short` | — |
| skipped | `1 unreadable and 0 unexpected-publisher snapshots were skipped` | `snapshots were skipped` | — |
| drift | `the publisher's heartbeat exceeds the staleness bound, so the staleness statement may be premature` | `heartbeat exceeds` | — |
| no heartbeat data | `the publisher does not report its heartbeat, so the staleness statement may be premature` | `does not report its heartbeat` | — |
| caveat | `only allowlisted sessions are shared, and there may be others` | `there may be others` | filtered or unlisted clause present |

The body's `none could be shown` supersedes an earlier draft's "none is in Henk's
allowlist": with two drop populations that sentence could be false, so the body states the
outcome and the notes line carries the cause. The unusable clause is therefore **never**
suppressed — it is the only place a broken or hostile publisher becomes visible.

A missing, unparseable, or future `generated_at` renders the *unknown* headline with the
frame's server time alongside, and the snapshot is still shown. A `message` body that does
not parse is counted into the skipped clause, and selection continues with the next-newest
candidate. `stale_after_seconds` defaults to **1500** (heartbeat 900 plus two 300-second
ticks of slack, which is what D7's boundary rule can actually cost). The drift clause fires
when `heartbeat_s + 2 × tick_s > stale_after_seconds` using the snapshot's own values; if
either is absent, the no-heartbeat-data clause fires instead.

*Distinctness.* Task 6 asserts **self-match** (each marker appears in its own rendering
with representative interpolations) and **uniqueness** (each marker appears in exactly one
row's rendering across all rows, with the list row rendered from marker-free placeholder
labels). A hand check over all rows found no marker that is a substring of another row's
sentence; the closest pairs are `has no entries`/`has not published`, `No live
sessions`/`No sessions are in scope`, `none could be shown`/`none could be used`,
`snapshots were skipped`/`snapshots were found`, and `dropped for size`/`dropped because`.
The hand check counted **20** rows carrying a marker.

### D11 — Result text stays out of the audit log, asserted not implemented

Result capture is global and default-deny; only `publish_handoff` is opted in. `sessions_read`
is asserted absent from `RESULT_CAPTURING_TOOLS`, and a test writes a session twice — once
with a fully populated snapshot, once with an empty one — and compares the audit records
byte-for-byte after timestamp normalisation (the read-depth decision-17 harness). The tool
has no arguments, so there is nothing to exclude on that side. No redaction code is added
and none is mutation-tested.

### D12 — Config: a `sessions` section that defaults to off, and one personal-data key

```yaml
sessions:
  enabled: false           # host and vps provisioning must exist first
  topic: henk-sessions
  stale_after_seconds: 1500
  lookback_seconds: 21600

personal_data:
  session_project_allowlist: ()   # empty surfaces nothing (D10, G0)
```

`enabled` is `false` in both the dataclass and the `from_dict` literal, with the
owner-acknowledgement finding-2 test (a config omitting every new key yields `false`).
Validation is unconditional, not gated on `enabled`: both durations positive;
`lookback_seconds ≥ stale_after_seconds` (a lookback shorter than the staleness bound
would report "nothing published" for a snapshot that is merely stale). `topic` must be a
single ntfy topic name — no `/`, no `,` — because a comma would silently widen the read to
a second topic. Base URL and timeout are reused from `endpoints.ntfy`; no new key.

`session_project_allowlist` lives in `personal_data` beside the existing scoping keys, is
matched **exactly after a whitespace strip**, discards entries that are empty after the
strip, and defaults to empty — which surfaces nothing and logs the `todo_read`-style
startup WARNING. It must resolve to empty through `Config.from_dict` when the key is
omitted entirely, and the pinned `PersonalDataConfig` field-set test
(`tests/test_config_read_depth.py:189-194`) and its comment are updated in the same change,
since that test exists to make a silent field addition impossible. The sample `config.yaml`
gains every new key at its default value, with the agreement test that pins sample against
defaults.

The system prompt gains `SESSIONS_TOOL_SUMMARIES` and a `sessions_enabled` argument on
`build_system_prompt` (`henk/config.py:132-156`), with the text: "sessions_read — the
owner's Claude Code sessions on the workstation, as last reported. Every result states how
old the report is; say so when it is stale, and never present listed sessions as all
sessions." A thirteenth registered tool exceeds the spelled-out count table
(`henk/config.py:122-129`), which raises `KeyError` by design rather than shipping a wrong
count; `COUNT_WORDS` gains `thirteen` and a test pins that the production registry with
every capability enabled composes without error.

### D13 — Two existing off-workstation paths are recorded, not fixed, and the asymmetry is stated

The workstation already sends session-derived text off the machine on two paths that
predate this change: the herdr `ntfy-agent-notify` plugin posts `workspace_label ·
tab_label` plus the absolute `cwd` for every finishing agent, on every workspace, to the
owner's phone topic; and the daily `digests/wsl.json` rsync to the owner's own vps runner
carries verbatim session titles and work repo names. Both go to the **owner** — a phone
and a personal runner — not to an agent with tool reach. That is the asymmetry: the lethal
trifecta's concern is untrusted content meeting tool reach meeting an outbound channel,
and Henk is the first consumer on this workstation where all three could meet. Holding
this feed to the stricter bar is consistent, and it is also the reason this change does
not use either path as its transport or its precedent.

Tightening the plugin (scoping `NTFY_STATES`, dropping `cwd`, honouring a deny list) is a
config-repo change with no Henk dependency; it is appended to the tooling backlog at task
8.9 as a follow-up, not folded in here. Task 8.9 carries a **second** backlog entry beside
it: the `session-titles` follow-up described below, so the deferral survives this change
directory being archived.

### D14 — Publisher code lives in this repo, tested by this suite, absent from the image

`deploy/session-publisher/session_publisher.py` (stdlib-only Python 3.11+, so the
workstation needs no venv), `session-publisher.service`, `session-publisher.timer`,
`config.example.toml`, and a short README. Tests in `tests/test_session_publisher.py`
drive it through injected fakes for the two CLI invocations, the git calls, the clock,
the state directory, and the HTTP publish. The Dockerfile copies `pyproject.toml`,
`README.md`, and `henk/` only, so nothing under `deploy/` is in the image — the stamp
writer set this precedent and its test still holds.

## Deferred: session titles

This section is the **record of a design that is not being built here**, preserved whole so
the `session-titles` follow-up can lift it rather than re-derive it. Nothing in it is in
scope for this change; v1 publishes no free text at all (Part E of the fix plan, Non-Goals
above, D4). The deferral has three anchors outside this change directory, because the
directory is archived at close: the `session-awareness` capability Purpose written at task
9.4 says transcript-derived free text is deliberately out of v1 scope and names this
follow-up; the North Star roadmap table gains a `session-titles` row between items 4 and 5
whose note carries the taint finding below; and task 8.9 appends the follow-up to the
tooling backlog.

**Why it was deferred.** Every hard problem the reviews found lives on this one path — the
same-turn taint finding below, the scrubber's status, the caps, the non-git exclusion, the
delimiters, the `session_render_free_text` key. The path is double-gated off at the shipped
defaults on both machines, the initial deployment opts no root in, and it is the only place
untrusted text would reach an owner turn. Removing it removes the whole cluster and leaves
a v1 in which every rendered value is shape-constrained.

**The opt-in design, as it stood.**

- **Per-root `fields`.** A root entry in the publisher config could list `title` and/or
  `branch` under a `fields` key; a root without it published neither, and the example
  config opted **no** root in. The v1 closed schema refuses `fields` by name (D3), so the
  follow-up re-adding it is a visible schema change, not a config flag someone can set.
- **`title` is `terminal_title_stripped`; `branch` is `git -C <cwd> branch
  --show-current`.** A detached HEAD publishes no `branch` key.
- **The scrubber is publication hygiene, not a Tier W control.** It replaces the four
  shapes the repo's pre-commit hook enforces (tailnet addresses, phone numbers,
  account-UUID shapes, token-shaped strings) with `[redacted]`. It must never be described
  as the thing that makes a title safe: a model-authored title about a work session is
  unsafe in ways no pattern matches. The control is the per-root opt-in; the scrubber
  catches the accidental shapes.
- **No free text for a session the git gate did not admit.** A non-git directory passes the
  owner gate in v1 because it has no owner to check, but it also has no identity to trust,
  so it must not carry free text. Free text is admitted only for sessions admitted *by the
  owner gate on a git checkout*.
- **Caps: `title` 60 characters, `branch` 40**, with a trailing `…` marking a cap. (The
  60 supersedes an earlier 80; a shorter cap is cheap and every character is attacker- or
  model-authored.)
- **A second Henk-side switch, `session_render_free_text`, default off.** The publisher
  opting a root in is necessary but not sufficient; Henk must also be told to render it,
  so a widened publisher config cannot on its own start putting free text in front of the
  model.
- **Untrusted-text delimiters.** Any rendered free text is wrapped in the same delimiters
  the triage path already uses for untrusted content (`henk/agent/triage.py:22-23`), so the
  model sees a marked region rather than a bare sentence.
- **Invocation-time taint** — and the finding that makes it the follow-up's *first* task.
  The intended control is that a turn which renders untrusted free text becomes tainted and
  therefore loses standing-tier writes. **That cannot be delivered by the existing
  mechanism:** the turn-entry snapshot at `henk/agent/core.py:542-548` builds
  `TurnContext(tainted=self._session_tainted)` **once per turn**, before any tool runs, so a
  taint raised by a tool call in the middle of a turn does not reach the permission check in
  that same turn. Same-turn refusal requires changing how taint is read, not just where it
  is raised. The follow-up starts there; until it lands, no free text renders.

## Risks / Trade-offs

- **herdr's status classifier is heuristic and remotely versioned** → the publisher passes
  `agent_status` through verbatim and Henk renders an unrecognised value as `status not
  recognised`, never as idle or done; the snapshot never infers. A future rule-manifest
  change that renames a status surfaces as `status not recognised` in Henk's output, which
  is honest.
- **herdr's JSON shape may change under a vendored binary upgrade** → required-field
  validation fails closed: nothing is published, the unit fails visibly, `--dry-run` shows
  the parse error.
- **Gating on both `cwd` and `foreground_cwd` makes a session blink** → a brief foreground
  excursion into a denied path drops the session from one snapshot and restores it in the
  next. Accepted: a session that flickers is a smaller harm than a work path riding out on
  a personally-admitted pane, and the blink is legible because the body always says how
  many sessions were reported.
- **A personal-root session can still contain pasted client data** → in v1 nothing derived
  from the conversation is published at all, so the residual is limited to the *existence*
  of a session under an allowlisted label. This is the residual the design accepts, and it
  is what the label gate and the dry-run review exist to bound.
- **Henk's label gate only catches a widened root that introduces a new label** → a
  re-pointed or relabelled existing root passes it. Caught instead by task 8.5's dry-run
  review and by the publisher's per-run journal line. Named as a limit in the spec, not
  papered over.
- **Classification error via a symlinked or scratch-worktree cwd** → gates run on the
  canonical path; the unsafe-root set is refused at load; `deny_roots` wins at any depth.
- **An owner typo widens `allow_roots` to `~`** → refused at load, publisher exits
  non-zero, nothing published.
- **The workstation is asleep most of the day** → D10's stale headline is the normal
  case, not an error; `Persistent=false` prevents a stale burst on resume; the 6-hour
  lookback keeps "last reported at …" answerable.
- **Clock skew between workstation and rp5** → a future `generated_at` renders the unknown
  headline with the server time alongside; skew smaller than the staleness bound is
  invisible and harmless.
- **ntfy body limit** → a 3800-byte budget with a fixed degrade order and a `degraded`
  count Henk renders; the alternative (attachments) would make Henk fetch a second URL and
  is exactly the promotion the budget prevents.
- **The 5-minute tick means up to 5 minutes of lag on a blocked session** → accepted for
  a status-question tool; the plugin-bus trigger is the recorded path if it ever matters.
- **The publisher's cadence and Henk's staleness bound are configured on two machines** →
  the snapshot carries `heartbeat_s` and `tick_s` and Henk says so in the drift clause
  rather than asserting a staleness it cannot justify.
- **Two credentials to provision instead of one** → deliberate (D8); `ntfy-provision`
  makes each a probed one-liner and the backlog records the procedure.
- **Tool count grows to thirteen on rp5** → the count table is extended and pinned by a
  test; the prompt's tool list is derived from the tuple so the two cannot disagree.

## Migration Plan

Ordered, because each step's verification gates the next. **Henk deploys before the
publisher on any schema change** (D6's rollout rule); misordering surfaces as G0's or G4's
sentence, not as a wrong answer.

1. Code and tests land: publisher, `sessions_read`, config, count table, audit
   assertions. Suite green from the 1896-passed baseline. `sessions.enabled` is `false`
   everywhere and `session_project_allowlist` is empty.
2. **vps, real terminal:** `ntfy-provision create <publisher-user> --grant
   henk-sessions:wo --token --token-out <path>`; `ntfy-provision grant henk henk-sessions
   ro`; `ntfy-provision probe henk-sessions … --expect wo` and `… --expect ro`. Record
   the procedure verbatim in the tooling backlog.
3. **Workstation:** `token-place` the publisher token; write the real
   `~/.config/henk-session-publisher/config.toml` from the example — allow roots with
   their labels, allow owners, deny roots, `publish_unlisted` off.
4. **Tier W review:** `session_publisher.py --dry-run` against the live estate; record in
   `notes/tier-w-publisher-review.md` the count of panes admitted and denied, which gate
   and which reported path denied each denied pane, and the allowlisted project labels —
   never a real title, cwd, or label. The owner's one decision here is
   `publish_unlisted`.
5. Enable the user timer; confirm one snapshot arrives on `henk-sessions` (admin read),
   confirm the heartbeat re-publishes with no change, confirm a status change publishes
   within one tick.
6. **rp5:** using the labels from step 3's config, set
   `personal_data.session_project_allowlist` and `sessions.enabled: true` **together** in
   the hand-maintained `config.yaml`; recreate the container; ask Henk over Signal;
   confirm the fresh, stale (stop the timer for 35 minutes), and no-snapshot renderings
   each reach a real reply.
7. `/docs-update`, README, backlog entries (including the `session-titles` follow-up),
   the North Star roadmap row, owner-acknowledgement line-number re-grep, archive.

**Rollback:** disable the timer (feed stops; Henk reports stale, then absent); empty
`session_project_allowlist` (Henk surfaces nothing and says so) or flip `sessions.enabled`
to `false` (tool unregistered); `ntfy-provision revoke` the publisher token and `grant henk
henk-sessions deny`. No data migration; the store is untouched.

## Open Questions

- ~~**Reversing the titles deferral.**~~ **Decided by the owner 2026-09-02: v1 ships
  without titles.** Free text stays deferred to the `session-titles` follow-up, whose first
  task is the `core.py:542-548` same-turn taint finding. The apply session does not re-raise
  this.
- **Heartbeat interval.** 15 minutes chosen against a 5-minute tick and a 25-minute
  staleness bound. A tighter triple (5/10/15) costs three times the messages for a
  status-question tool; not obviously worth it.
- **Publisher ntfy user name.** `henk-workstation` is proposed for symmetry with
  `henk-sensors` and `henk-pickup`; decided at step 2, recorded in `notes/`.
