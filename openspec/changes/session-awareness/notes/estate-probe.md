# Estate probe — apply-time re-check of the facts the publisher depends on

**Probed:** 2026-09-02, on the workstation (WSL2), at the start of the apply session. Standing
rule 1 applies throughout: shapes, key sets, character classes, and counts only. No pane's
title, working directory, workspace label, or tab label is recorded here. Where a finding
disagrees with `design.md`'s Context, the finding wins (standing rule 2) and the deviation is
marked **DEVIATION**.

## 1.1 `herdr agent list`

- **Envelope.** One JSON line per invocation (2913 bytes for six agents). The agents are
  **not** at the top level — **DEVIATION** from the design's shorthand — the envelope is:

  ```
  {"id": <string>, "result": {"type": "agent_list", "agents": [ ...one object per agent... ]}}
  ```

  The publisher reads `result.agents` and refuses the run when `result.type` is not
  `agent_list` or `result.agents` is not a list.
- **Per-agent key set** (identical on all six agents, 14 keys):
  `agent`, `agent_session`, `agent_status`, `cwd`, `focused`, `foreground_cwd`, `pane_id`,
  `revision`, `state_change_seq`, `tab_id`, `terminal_id`, `terminal_title`,
  `terminal_title_stripped`, `workspace_id`. Four of these (`agent`, `focused`,
  `terminal_id`, `terminal_title`) were not in the design's list; none is consumed.
- **Types.** `pane_id`, `agent_status`, `cwd`, `foreground_cwd`: string. `revision`,
  `state_change_seq`: integer. `focused`: boolean. `agent_session`: object with keys
  `agent`, `kind`, `source`, `value`; `value` is a UUID-shaped string
  (`hhhhhhhh-hhhh-hhhh-hhhh-hhhhhhhhhhhh`), `kind` is `id`.
- **`agent_status` observed:** `idle`, `working` (two of the five documented values; the
  other three were not live at probe time and remain documented-only).
- **`foreground_cwd`:** present on 6 of 6, null on 0, **differs from `cwd` on 0 of 6**. The
  both-paths gate (D3) therefore had no live case to exercise at probe time; it stays in the
  code because the field exists and the design's reasoning stands, and task 8.5 records
  whether the live dry-run ever sees a differing pair.
- **`terminal_title_stripped`:** present and non-empty on 6 of 6. Not consumed.
- **Every `cwd` is an absolute path** (6 of 6).
- **Pane-id character set (the record Henk's shape regex is set from).** Six ids, lengths 5
  and 6. Characters observed, as a class: **ASCII letters (both cases), ASCII digits, and one
  `:`** — nothing else. Shapes with letters as `A` and digits as `9`: `A9:A9`, `A9:A99`,
  `A9:AA`, `AA:A9`. The workspace segment starts with `w`, the pane segment with `p`; the
  remainder is alphanumeric in either case (herdr's docs show `p9`, `pW`, `p14` side by side
  and refuse to specify a grammar). `tab_id` has the same shape with a `t` segment;
  `workspace_id` is the bare `w` segment.
  **Conclusion:** the design's `^[\w:.-]{1,32}$` admits every observed id and the observed
  set is strictly inside it (no `.` or `-` was seen). The regex is kept as designed; a
  tighter `^[A-Za-z0-9:]+$` would also pass today but would break on the first id herdr
  emits with a character it does not document, which is exactly the derivation its docs warn
  against.

## 1.2 `claude-estate status --json`

- **Runs without a tty:** invoked with stdin from `/dev/null`, exit 0, empty stderr.
- **Shape:** a pretty-printed JSON object (92 lines, 2563 bytes for six rows):
  `{"agents": [ ...rows... ], "summary": { ... }}`.
- **Row key set** (identical on all six rows, 11 keys): `age_s`, `class`, `cwd`, `kind`,
  `pane_id`, `resume`, `session`, `status`, `tab_id`, `title`, `workspace_id`.
- **`age_s`:** integer on 6 of 6, all non-negative, **0 nulls observed**. The null case
  (design: "a pane in herdr but not in claude-estate gets `null`") is therefore
  fixture-verified only.
- **Join key:** `pane_id`, with exactly the same four shapes as herdr's (`A9:A9`, `A9:A99`,
  `A9:AA`, `AA:A9`).
- `status` observed `idle`, `working`; `kind` observed `claude`; `class` observed `active`,
  `fresh`, `stale`. Only `pane_id` and `age_s` are consumed; `cwd`, `resume`, `session`,
  `title` are the fields that must never leave the machine and are never read.

## 1.3 `git` behaviour by checkout shape

Scratch repositories only (created under the session scratchpad with placeholder identity;
the workstation's global git config signs commits and routes identity through `includeIf`,
which is why the probe passed `-c commit.gpgsign=false` and an explicit identity — the
publisher never commits, so neither matters to it).

| Shape | `remote get-url origin` | `branch --show-current` | `rev-parse --is-inside-work-tree` |
|---|---|---|---|
| checkout with `origin` | rc 0, the URL | rc 0, branch name | rc 0, `true` |
| subdirectory two levels below that checkout | rc 0, same URL | rc 0, same branch | rc 0, `true` |
| checkout without `origin` | **rc 2**, `error: No such remote 'origin'` | rc 0, branch name | rc 0, `true` |
| non-git directory | **rc 128**, `fatal: not a git repository …` | rc 128, same | rc 128, same |
| detached HEAD | (no origin) rc 2 | **rc 0, empty output** | rc 0, `true` |
| linked worktree | rc 0, the main checkout's URL | rc 0, the worktree's branch | rc 0, `true` (`--show-toplevel` is the worktree path; `--git-common-dir` points into the main checkout) |
| symlinked path into a checkout | rc 0, the URL | — | — |

- **Clean environment is identical:** every row above was run twice — once in the shell's
  environment, once under `env -i PATH HOME GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1` with
  `-c core.pager=cat` — and output and exit code matched in every case.
- `remote get-url` never touches the network, so `GIT_TERMINAL_PROMPT=0` is belt-and-braces
  for it; the timeout is what bounds a pathological filesystem.
- Consequence for the owner gate: rc 2 (no origin) and rc 128 (not a repository) are
  different outcomes and are handled differently — 128 means "not a git work tree, owner
  gate passes"; 2 means "a checkout with no origin, owner gate fails".

## 1.4 vps ntfy limits and poll behaviour

Read over SSH from `/opt/ntfy/config/server.yml` (read-only; filtered to the relevant keys):

- `auth-default-access: deny-all`
- `cache-duration: "72h"`
- `keepalive-interval: "45s"` (matches Henk's recorded copy in `config.yaml`)
- **No `message-size-limit` key is set**, so ntfy's default of 4096 bytes applies —
  consistent with the design's ~4 KB and its 3800-byte budget.
- Attachments are enabled (`attachment-cache-dir` set, `attachment-file-size-limit: "5M"`,
  `attachment-total-size-limit: "100M"`, `attachment-expiry-duration: "72h"`), which is what
  makes an over-limit body become an attachment rather than an error — the promotion the
  budget exists to prevent.
- `behind-proxy: true`, `base-url` is the public hostname; the tailnet port Henk uses is
  the direct one.

Poll shape, probed with the workstation's existing read-only pickup credential:

- `GET /henk-handoffs/json?poll=1&since=72h` → HTTP 200 with an **empty body** (no cached
  message in the last 72 h). So an empty poll is a 200 with zero lines, not an error and not
  an `open` frame — G2's "no `message` frame in either stage" is exactly this case.
- The same request against a topic the credential is not granted → **HTTP 403** with a
  one-line JSON body of keys `code`, `error`, `http`, `link`. That is not a frame and has no
  `event` key; Henk's G1 catches it as a non-2xx before any frame parsing.
- **Not confirmed live:** the `message` frame key set (`id`, `time`, `expires`, `event`,
  `topic`, `title`, `message`, `priority`) and newest-last ordering, because no readable
  topic had a cached message at probe time. This is confirmed at task 8.7 when the first
  snapshot is read through the admin account; the tool's parser follows ntfy's documented
  frame format until then.

## 1.5 systemd user instance and Python

- `systemctl --user is-system-running` → `running`.
- Three user timers listed; two are the owner's custom timers (`claude-memory-sync`,
  `claude-estate-nudge`), each a `.timer` + `.service` pair.
- **Unit-symlink pattern:** `~/.config/systemd/user/<unit>` → `~/.claude-config/systemd/user/<unit>`
  for both pairs, plus a `timers.target.wants/` directory. The publisher's units follow the
  same pattern (task 8.7): committed here under `deploy/session-publisher/`, symlinked from
  `~/.config/systemd/user/`.
- `python3 -V` → **Python 3.12.3** at `/usr/bin/python3`; `tomllib`, `fcntl` and
  `urllib.request` import. The 3.11+ guard passes on this host.

## Deviations summary (code follows these, not the design text)

1. herdr's agents live at `result.agents` inside an `{"id", "result": {"type", "agents"}}`
   envelope; the publisher validates `result.type == "agent_list"`.
2. `foreground_cwd` never differed from `cwd` in the live estate at probe time; the
   both-paths gate is kept (the field exists) and 8.5 records the live count.
3. `claude-estate` returned no `age_s: null` live; the null path is fixture-verified.
4. `branch --show-current` on a detached HEAD is rc 0 with empty output (recorded for the
   deferred titles follow-up; v1 never runs it).
5. The ntfy `message` frame key set is not live-confirmed until 8.7.
