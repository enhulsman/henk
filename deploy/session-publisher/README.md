# session-publisher

Publishes a metadata-only snapshot of the workstation's live Claude Code sessions to
the deny-all ntfy topic Henk reads. Runs from a systemd **user** timer on the
workstation, with the system interpreter and no virtualenv (stdlib only). It never
opens a transcript, never invokes a transcript reader, and never publishes free text.

## What it publishes, and nothing else

Every admitted session is exactly four keys:

| key | value |
|---|---|
| `pane` | herdr's pane id — an opaque addressing token, so two sessions sharing a label can be told apart. Not actionable. |
| `project` | the **configured `label`** of the allow-root entry that admitted the session's `cwd`. Never a path or a path component. |
| `status` | herdr's `agent_status` verbatim. |
| `age_s` | last-activity age in seconds from `claude-estate`, or `null`. |

Around them the snapshot carries `schema`, `generated_at`, `publisher`,
`age_source`, `heartbeat_s`, `tick_s`, `sessions`, plus `unlisted` only when opted in
and `degraded` only when the byte budget dropped something. No absolute or relative
path, no workspace or tab id or label, no session UUID, no branch name, no session
title, no hostname — on any root, under any configuration.

Two independent gates decide admission, and **both** are applied to `cwd` and to
`foreground_cwd` when it differs:

- **root gate** — the canonical path (after `realpath`) is under an `allow_roots`
  entry and under no `deny_roots` entry. Deny wins at any depth.
- **owner gate** — the path is not inside a git work tree, or its `origin` names an
  owner in `allow_owners`. A checkout with no `origin` is refused.

## Install

```bash
# 1. Configuration
mkdir -p ~/.config/henk-session-publisher
install -m 600 deploy/session-publisher/config.example.toml \
  ~/.config/henk-session-publisher/config.toml
$EDITOR ~/.config/henk-session-publisher/config.toml     # real roots, labels, owners

# 2. Token — a write-only ntfy credential for this one topic, minted on the vps with
#    ntfy-provision (--token-out) and moved here by token-place, which places it at
#    mode 600 inside a mode-700 directory and never accepts the token on argv.
#    Without --yes it prints the plan and changes nothing.
~/.claude-config/bin/token-place --target-host <workstation> \
  --consumer henk-session-publisher --token-name ntfy \
  --source-host vps --source-path <path written by --token-out>   # then --yes
ls -la ~/.config/henk-session-publisher/                 # expect drwx------ / -rw-------

# 3. Units, symlinked from the config repo the way the other user timers are
ln -s "$PWD/deploy/session-publisher/session-publisher.service" \
  ~/.config/systemd/user/session-publisher.service
ln -s "$PWD/deploy/session-publisher/session-publisher.timer" \
  ~/.config/systemd/user/session-publisher.timer
systemctl --user daemon-reload && systemctl --user enable --now session-publisher.timer
```

Enable the **timer**, never the service: the service is `Type=oneshot` and carries no
`[Install]` section.

## Review it before you enable it

`--dry-run` is the Tier W review instrument, and it is the only way to see the
classification without publishing anything. It prints one line per pane — pane id,
admitted or denied, the status, and either the project label or every (gate, reported
path) pair that refused it — then the snapshot as JSON. It issues **no HTTP request**,
writes **no state**, and needs no token and no state directory:

```bash
deploy/session-publisher/session_publisher.py --dry-run \
  --config ~/.config/henk-session-publisher/config.toml
```

Read it before enabling the timer and confirm that every work-subtree pane is denied
and by which gate. Record the findings as **counts and admitted labels only** — never
a real cwd, title, or workspace label.

## The `OnCalendar` / `tick_seconds` coupling

`session-publisher.timer`'s `OnCalendar=*:0/5` and the config's `tick_seconds = 300`
must be changed **together**. Nothing can check them against each other from inside
the process: the timer decides how often the tick actually fires, `tick_seconds` is
what the publisher believes its cadence to be, and that belief is published as the
snapshot's `tick_s`, which is what Henk judges its own staleness bound against. A
timer firing on a different period than the config claims makes Henk's freshness
statement wrong in a way neither machine can detect.

## One entry per project

Prefer one `allow_roots` entry per project, each with its own label. A container entry
covering a whole projects directory is legal and strictly weaker — it admits every
subdirectory created under it afterwards, including one cloned from somewhere new.
If you use one, pair it with `deny_roots` for the subtrees that must never be
published; deny is evaluated on the canonical path at any depth, so an allow root
sitting above a deny root is a supported shape and loads without complaint.

Labels are also Henk's gate: every label here must be listed in rp5's
`personal_data.session_project_allowlist` before Henk will surface anything from it.

## Publish policy

The publisher hashes the **final, post-degrade** snapshot with `generated_at`,
`heartbeat_s`, `tick_s`, and every `age_s` removed, and publishes when that key
differs from the stored one, or when `elapsed + tick_seconds` exceeds
`heartbeat_seconds` measured from the last **successful** publish. A failed publish
exits non-zero, leaves the state file alone, and does not retry — the next tick is the
retry.

State lives in `$STATE_DIRECTORY` (`StateDirectory=henk-session-publisher`), as
`last.json` plus an advisory lock; `--state-dir` overrides it. An unwritable state
directory is a **hard failure**, not a fallback: silently treating it as a first run
would republish on every tick forever.

## Watching it

```bash
systemctl --user list-timers session-publisher.timer
journalctl --user -u session-publisher.service -n 20
```

Every run logs exactly one line, and it is the artefact that catches a re-pointed or
re-labelled allow root (which Henk's label gate cannot see):

```
session-publisher: admitted=3 denied=7 dropped=0 labels=alpha,beta reason=changed published=yes
```

`reason` is one of `first-run`, `changed`, `heartbeat`, `unchanged`, `locked`, or
`dry-run`. Exit codes: `0` published, nothing to publish, a dry run, or another run
held the lock · `1` a source or the publish failed · `2` the configuration was
refused, no state directory was usable, the token is missing, or the interpreter is
older than 3.11.

## Rollback

```bash
systemctl --user disable --now session-publisher.timer
```

The feed stops; Henk reports the snapshot as stale, then as absent, and never as "no
sessions". Nothing else needs undoing — the publisher holds no state beyond
`last.json`. To cut it off at the other end, `ntfy-provision revoke` the publisher
token and set Henk's grant on the topic to deny.
