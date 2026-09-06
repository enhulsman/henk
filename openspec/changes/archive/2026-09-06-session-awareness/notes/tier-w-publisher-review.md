# Tier W publisher review — `--dry-run` against the live estate (task 8.5)

**Run:** 2026-09-02, on the workstation, `session_publisher.py --dry-run --config
~/.config/henk-session-publisher/config.toml`, twice (before and after the 8.6 decision).
Standing rule 1: counts, gates, reported-path roles, and allowlisted labels only. No pane's
working directory, title, workspace label, or tab label is recorded.

## Configuration under review (shape, not values)

- `allow_roots`: **11 entries**, one per project, each with its own label. No container root:
  the projects directory itself is deliberately not an allow root, so a future clone under it
  is unlisted until it is added by hand.
- `allow_owners`: **2** owner segments, both the owner's own identities.
- `deny_roots`: **1** entry — the work subtree under the projects directory.
- `publish_unlisted`: **true** (8.6, below). `heartbeat_seconds` 900, `tick_seconds` 300.

## Classification of the live estate

| Panes live | Admitted | Denied | Denying gate | Denying reported path |
|---|---|---|---|---|
| 4 | 2 | 2 | root gate (both) | `cwd` (both) |

- **Admitted labels:** `henk`, `config`.
- **Every work-subtree pane is denied**, by the root gate on `cwd`. The second denied pane is a
  scratch shell whose `cwd` is the projects directory itself — not under any allow root.
- **`foreground_cwd` differed from `cwd` on 0 of 4 panes** (matches probe §1.1: 0 of 6).
- The owner gate was not reached for either denied pane (root denial short-circuits it), and
  admitted both admitted panes.
- Snapshot: 2 sessions × exactly four keys; `age_source: claude-estate`; `degraded` absent;
  after 8.6, `unlisted: {"count": 2, "blocked": 0}`.
- Journal line as printed: `admitted=2 denied=2 dropped=0 labels=config,henk reason=dry-run`.

## 8.6 — `publish_unlisted`: **on**

Owner decision 2026-09-02. Reasoning: the aggregate is the only way Henk can answer "is
anything waiting on me?" when the waiting session is a work one, and it carries two integers
and nothing else about those sessions. The default-deny rule that made it off-by-default is
satisfied by this being an explicit owner decision, recorded here.

## Labels to allowlist on rp5 (task 8.8)

`henk`, `claude-config`, `docs`, `weekly-review`, `cclog`, `launchpad`, `geldpilot`,
`health-pipeline`, `tailscale-acl`, `vaste-grond`, `bible-tui`.

**Post-8.8 label rename.** The `.claude-config` root's label was `config` at review time and was
renamed `claude-config` by the owner after the first live reply; both the publisher config and
rp5's allowlist were changed. The one tick in between produced a live *filtered* clause (the old
label was no longer allowlisted), which is the intended behaviour of the two-key gate.
