# Live verification record (tasks 9.1–9.6), 2026-09-02

Shapes and conclusions only. Raw replies and logs carried endpoint names and, in the
corpus reply, addresses from the documentation itself; none are reproduced here.

## 9.1 Surface

- **ACL grants:** no ACL was edited in this change. The henk node is the same tagged
  device as before; egress observed in the container log during verification went only to
  the Gatus API on rp5, the Prometheus API and the ntfy topic on the vps — the pre-existing
  set.
- **Mounts:** the new bind mount is present and read-only — `touch` inside the container
  fails with "Read-only file system"; the stamp file is visible at the container path.
- **Sockets:** no port is published by any henk container (the Signal bridge still shows
  its exposed-not-published 8080); the only listeners on the rp5 host belong to rp5's own
  tailscaled, unchanged.

## 9.2 Credentials in the container

Environment variable *names* are the same four secrets as before (Anthropic OAuth, ntfy,
todo, Tailscale auth key) plus the base image's own; nothing git- or SSH-shaped. `/root/.ssh`
is unreadable to uid 10001 and `/home/henk/.ssh` does not exist. The mounted clone's
`.git` metadata is visible (remote URL is an ssh host alias, no key material). The deploy
key lives only under root on the host.

## 9.4 The six queries, live over Signal

| query | conclusion |
|---|---|
| `node_resource_trend` (vps, memory, 24h) | summary-only output; compared against the 75 % bar; one `query_range` call |
| `scrape_targets` | **seven** targets enumerated with values, `pushgateway` included; `/api/v1/targets` + `up` |
| `endpoint_history` | discovery on first use (bulk statuses), 19 endpoints; the composed `group_name` key was used verbatim for the per-endpoint statuses and the `7d` uptime route; since-when came from `events`, dating to the endpoint's creation |
| `freshness_check` | eight timestamp metrics rendered as ages that advance between calls; four instant queries |
| `container_state` (rp5) | 19 containers with creation-time labelling; the caveat that creation time is not last restart reached the reply |
| `dns_performance` (rp2) | node named by enum, no address; compared against the measured 24h baseline and rp2's own 200/500 ms bars; the rolling-average caveat was in the tool result but the model omitted it from its prose |

No empty results. No errors or warnings in the container log across the run. Every
request returned 200. The model additionally called `homelab_health` once during the
Gatus question (the two backends' routes appear together at that timestamp).

## 9.5 The corpus tool, live

- **Search then read** on a backups question returned sectioned content with a freshness
  line: pull age 13 minutes, commit age 11 days, correctly **not** marked stale (only the
  pull time is bounded).
- **Deliberately stale stamp** (`pulled_at` edited to ~3 days back): the reply led with an
  explicit stale marker naming the age and the 26 h bound and suggesting the updater may
  have stopped. A real pull afterwards advanced the stamp and cleared it.
- The corpus reply reproduced tailnet addresses **from the documentation text**. This is
  D13's admitted content, not a projection failure; the projection rule covers metric and
  Gatus results. Recorded so the posture (derived addresses never, documented addresses
  yes) is a visible decision rather than an accident.

## 9.6 No corpus content in the repo or the image

- Repo: the only corpus-shaped files are the synthetic fixture under `tests/fixtures/`;
  scanned clean for real hostnames and addresses.
- Image: the build context excludes `tests/` and `openspec/` via `.dockerignore`, and the
  corpus reaches the container only through the runtime bind mount, so no image layer can
  contain it.

## 9.3 Deploy-time guard

With the host mirror moved aside, `docker compose up -d --force-recreate henk` failed with
`invalid mount config for type "bind": bind source path does not exist` and exit 1, and the
host directory was still absent afterwards — Docker created nothing. Compose refused before
removing the running container, so the live agent never went down. Restoring the directory
and running `up -d` again left the stack running. A plain `up -d` without `--force-recreate`
does **not** exercise the guard (it re-mounts nothing on an unchanged service), which is the
first thing to remember when repeating this check.
