# Task 6.0: ntfy backlog-before-keepalive probe

**Run:** 2026-10-30, by the owner on rp5, from inside the henk container (Henk's own
`NTFY_TOKEN`, so the same server, auth and network path as intake).

**Method:** one throwaway subscription to `henk-events` at
`/henk-events/json?since=all`, read until three keepalives or 160 s. The script printed
frame types and arrival times only (consecutive frames of one type collapsed), never ids,
titles or bodies. `since=all` maximises the cached backlog, so ordering is tested against
the largest replay the server can produce.

**Raw output:**

```
http 200
    0.09s  open x1
    0.09s  message x16
    45.1s  keepalive x3
keepalives 3 messages_after_first_keepalive 0
```

**Result: agrees with design D8.** Every cached message (16) arrived in one burst
immediately after `open`, and the first `keepalive` arrived one keepalive interval later
(45 s, matching `endpoints.ntfy.keepalive_interval_seconds: 45` in `config.yaml`). No
message frame followed the first keepalive. So "a keepalive has arrived since the latest
subscribe" implies the `since=` backlog has been fully received, and the caught-up rule
is implemented as designed. The fallback rule (a full keepalive interval with no message
frame) is not needed.

**Consequence for release timing:** caught-up is first reachable about 45 s after each
(re)subscribe, so a release cannot fire sooner than that after a restart. This is well
inside the 60 s release tick and the 15-minute uptime fallback.

**Limits:** one run, one server version, a 16-message backlog. A much larger backlog was
not tested. The ordering is a property of the server writing its cache before it enters
its keepalive loop. If the server or its `keepalive-interval` changes, re-run this probe.
