# signal-cli send latency on rp5 — the measurement the send bound is designed against

Taken 2026-08-20, per `openspec/changes/reminders/notes/README.md`: *"Before designing the
bound: log signal-cli send latency on rp5 for a day."* The measurement came out **better than
asked**: the bridge container (`henk-signal-cli-rest-api-1`, up 4 weeks) logs a Gin access line
for every `POST /v2/send` with **server-side latency**, so the full retained log is a
retroactive measurement — **29 days, not one**, and on the bridge's side of the wire, which is
exactly where signal-cli's own processing time lives.

## Harvest command (re-runnable at apply time to extend the window)

```bash
ssh rp5 'sudo -n docker logs henk-signal-cli-rest-api-1 2>&1 | grep -a "v2/send"'
```

## Distribution — n=82 sends, 2026-07-22 → 2026-08-20, every one `201`

| statistic | value |
|---|---|
| min | 118 ms |
| median | 162 ms |
| mean | 295 ms |
| p75 | 258 ms |
| p90 | 730 ms |
| p95 | 812 ms |
| p99 | 1066 ms |
| **max** | **1087 ms** |
| over 2 s | **0** |
| over 1 s | 2 |
| over 500 ms | 17 |

The distribution is **bimodal**: a warm mode at ~120–260 ms and a slow mode at ~680–1090 ms
(likely signal-cli waking from idle — slow-mode samples cluster at first-send-after-quiet, e.g.
the five consecutive ~700–780 ms sends on 2026-07-23 morning). Nothing in a month came within
an order of magnitude of the 10.0 s ceiling, or of the 6.0 s one before it.

## Why the pre-rebuild samples are valid at 10.0 s

The README warned that measuring under the old effective 6.0 s read ceiling would measure
"Henk's give-up point rather than signal-cli's latency distribution." That censoring never
happened: these are **server-side** numbers (Gin logs when the handler returns, whether or not
the client is still waiting), and the maximum observed is 1.087 s — no send ever approached
6.0 s, so the client-side ceiling never cut anything off. Zero non-201 responses in the whole
retained log is the corroborating half.

## What this does and does not establish

- **Established:** healthy-path per-chunk send latency on rp5 is ≤ ~1.1 s at worst, ~160 ms
  typically. An 18-chunk `/memories` reply — the worst reachable message — holds the send path
  for ~3 s typically, ~20 s if every chunk hit the observed maximum. The elevated-latency
  premise behind D5's 90–144 s estimate ("5–8 s per chunk on a working bridge") is
  **contradicted by a month of data** on this host.
- **Established:** `send_timeout_seconds = 10.0` carries ~9× headroom over the worst observed
  send. It stays as is; no evidence supports touching it in either direction.
- **Not established:** latency under bridge degradation. All 82 samples are successes; the
  degraded ceiling is still the configured one (`max_send_attempts × send_timeout + backoff`
  ≈ 33 s per chunk). Any serialization design must price that case from config, not from this
  data.
- **Not established:** henk-side overhead (client construction, connect, write). Same-host
  compose network, plain HTTP — order of milliseconds; the henk-side number is the Gin number
  plus noise.
- **Watch status, stated precisely:** the bridge-side log shows zero non-201 and zero
  \>6 s requests over 29 days. The henk-side `partial`/`failed`/`giving up` grep and the
  store-error grep were both empty — but the henk container was recreated at today's
  reminders-core deploy, so those greps cover only ~1 h. The bridge-side month is the strong
  evidence; the henk-side watches remain standing and should be re-run at apply and at deploy.

## Raw samples

All 82 Gin lines as harvested (timestamps are the bridge container's clock, CEST):

```
2026-07-22 15:55:51  196.8ms   2026-07-22 16:01:31  298.6ms   2026-07-22 16:01:51  127.0ms
2026-07-22 16:51:21  135.7ms   2026-07-22 16:53:46  188.9ms   2026-07-22 17:02:40  138.4ms
2026-07-22 17:05:38  144.0ms   2026-07-23 06:05:59  141.6ms   2026-07-23 06:06:48  127.5ms
2026-07-23 06:07:37  121.9ms   2026-07-23 06:08:15  128.7ms   2026-07-23 07:38:02  735.7ms
2026-07-23 07:43:39  708.6ms   2026-07-23 08:21:17  717.2ms   2026-07-23 08:25:03  726.9ms
2026-07-23 08:51:42  779.1ms   2026-07-23 09:58:40  139.7ms   2026-07-23 09:59:59  326.6ms
2026-07-24 10:53:15  134.7ms   2026-07-24 10:57:47  193.9ms   2026-07-24 11:15:24  127.8ms
2026-07-24 11:15:29  131.0ms   2026-07-24 11:15:41  223.5ms   2026-07-24 11:21:14  135.4ms
2026-07-24 18:15:10  919.0ms   2026-07-24 18:39:17  766.8ms   2026-07-24 19:33:46  710.9ms
2026-08-06 17:53:52 1086.6ms   2026-08-06 18:32:44  720.5ms   2026-08-06 21:53:27  718.5ms
2026-08-08 20:42:11  136.2ms   2026-08-11 12:25:23 1066.2ms   2026-08-11 20:30:20  912.1ms
2026-08-18 19:19:52  258.0ms   2026-08-18 19:19:56  165.5ms   2026-08-18 19:20:20  161.4ms
2026-08-18 19:20:23  164.9ms   2026-08-18 19:25:32  121.6ms   2026-08-18 19:25:40  223.8ms
2026-08-18 19:25:42  164.7ms   2026-08-18 19:25:46  159.2ms   2026-08-18 19:25:59  120.5ms
2026-08-18 19:26:11  129.8ms   2026-08-18 19:26:14  123.9ms   2026-08-18 19:31:13  730.1ms
2026-08-18 19:31:16  181.1ms   2026-08-18 19:32:00  119.9ms   2026-08-18 19:32:14  119.3ms
2026-08-18 19:32:23  120.8ms   2026-08-18 19:34:46  117.8ms   2026-08-18 19:35:01  156.6ms
2026-08-18 19:35:03  160.0ms   2026-08-18 19:39:42  681.8ms   2026-08-19 05:23:12  136.3ms
2026-08-19 05:23:19  119.4ms   2026-08-19 18:35:36  257.9ms   2026-08-19 18:35:47  161.1ms
2026-08-19 18:35:52  162.2ms   2026-08-19 18:35:57  154.4ms   2026-08-19 18:36:02  162.1ms
2026-08-19 18:36:07  160.3ms   2026-08-19 18:36:26  177.6ms   2026-08-19 18:36:48  155.5ms
2026-08-19 18:36:59  236.4ms   2026-08-19 18:37:18  248.2ms   2026-08-19 18:37:25  238.1ms
2026-08-19 19:51:12  706.0ms   2026-08-19 21:05:00  161.1ms   2026-08-19 21:37:51  131.9ms
2026-08-20 07:00:41  161.9ms   2026-08-20 07:00:48  169.8ms   2026-08-20 07:00:53  204.1ms
2026-08-20 07:06:16  128.0ms   2026-08-20 07:06:16  168.0ms   2026-08-20 11:56:43  812.3ms
2026-08-20 11:56:57  157.6ms   2026-08-20 11:57:07  174.6ms   2026-08-20 11:57:10  172.2ms
2026-08-20 11:57:21  162.0ms   2026-08-20 11:57:29  177.9ms   2026-08-20 11:57:36  160.0ms
2026-08-20 11:57:46  161.4ms
```

(The dense runs — 2026-08-18 evening, 2026-08-20 11:57 — are conversation sessions: gaps of
5–20 s between sends with ~160 ms server latency each are replies paced by the owner and the
model, not chunks. The one confirmed multi-chunk send, the two-message enumeration reply in
channel-integrity's As-built, is the 07:06:16 same-second pair.)
