# 2026-09-23 vps swap incident — ground truth and evidence gaps

Written by the session that investigated this morning's `HenkSwapPressure` handoff by hand
(Prometheus on vps, host inspection over SSH). Offered as input to `triage-quality`; the
owner asked for it to be folded into the proposal where it fits. Times are UTC.

## Ground truth (usable as a graded triage-replay case)

- **Trigger:** `apt-daily-upgrade.service` ran unattended-upgrades at 06:10. Its `rsyslog`
  upgrade restarts `dmesg.service` (rsyslog ships the unit; postinst runs
  `deb-systemd-invoke ... dmesg.service`). Mid-uptime, that unit runs
  `journalctl --boot 0 --dmesg` over the whole boot: 111 days, a 120 MB dump that was 99% UFW
  block lines, 1m17s of CPU.
- **Mechanism:** reading the journal pulled ~1 GB into page cache, attributed to the
  `/system.slice/dmesg.service` cgroup (working set 107 → 1026 MB, peak 06:12:00). Idle Taiga
  gunicorn/celery pages were swapped out: swap-out peaked ~1,570 pages/s at 06:12:17 and
  fullness stepped from 70.4% to 98.7% in one minute. The later swap-in (06:27–06:30) was
  those pages being touched again.
- **Which branch fired:** the alert value `A=98.39` is the fullness branch (>95%). The I/O
  branch was also true.
- **Correct diagnosis:** a one-off, upgrade-triggered page-cache burst from a host systemd
  unit, not a container and not a leak. It recurs on every rsyslog upgrade (the previous
  one, 2026-07-26, left a 56 MB `dmesg.0`).
- **Fix applied:** a boot-only `ExecCondition` drop-in for `dmesg.service`, journald capped
  at 2G, swap grown to 2 GB (see homelab docs, vps.md, 2026-09-23 note).
- **Where the handoff went wrong:** it read a one-minute step as "rising steadily" (no
  timestamps; already covered here), attributed the event to the I/O branch (already
  covered), and could not name a culprit.

## Gap 1 — host systemd units are invisible to `container_state`

The design's new memory/swap aspects select `name!=""`, i.e. named Docker containers only.
**Today's culprit had no `name`**: it was `/system.slice/dmesg.service`. cAdvisor already
exports host units as cgroups with an empty `name` and an `id` label. Measured today:
`count by (job) (container_memory_working_set_bytes{id=~"/system.slice/.+\\.service"})`
returns 27 series on `cadvisor-vps` and 36 on `cadvisor-pi5`. [Historical hand query:
this selector spelling is superseded by the canonical templates in the triage-quality
design, D5 table.]

The query that found it by hand was "biggest working-set movers across all cgroups in the
window", shaped roughly like
`topk(5, max_over_time(container_memory_working_set_bytes{job="<job>",id=~"/system.slice/.+"}[<w>]) - min_over_time(...[<w>]))`,
rendered with the time of each peak. [Historical hand query: superseded by the D5 canonical
`memory_movers` templates, which apply each range function per selector.] Working set includes active page cache, which is
exactly the signal here: a journal scan shows up as the scanning unit's memory.

Related: the nightly 01:00 backup runs `docker run --rm alpine` and those containers get
Docker's random names (`suspicious_mendeleev`, `lucid_chebyshev`). A renderer that shows
container names should say that auto-generated names mean ephemeral `docker run`
containers. The owner was alarmed by one.

## Gap 2 — crash-looping host services

`nextcloud-rclone.service` crash-looped every ~10 s from 2026-06-05 to today (472,184
restarts, about half of all journal entries) and nothing noticed. The design's restart
aspect covers containers only.

**The signal already exists on vps.** node_exporter there runs with `--collector.systemd`,
and `node_systemd_unit_state{name="nextcloud-rclone.service",state="activating"} == 1` held
in every sample of the hour before removal. A query or rule on
`node_systemd_unit_state{state=~"activating|failed"} == 1` sustained over e.g. 15m would
have caught it. Caveats:
- Only vps has the systemd collector; rp5 and rp2 would need the flag (an infra change,
  probably a follow-up rather than part of this change).
- vps runs node_exporter **0.18.1** (2019). It supports
  `--collector.systemd.enable-restarts-metrics` (`node_systemd_service_restart_total`),
  which is not enabled.
