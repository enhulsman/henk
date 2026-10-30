# Task 8.2: egress probe for the Google token endpoint

**Run:** 2026-10-30, by the owner on rp5, from inside the henk container (the same
network namespace intake and the tools use). A plain `httpx.get` to each host, printing
only the host and the HTTP status.

```
oauth2.googleapis.com 404
www.googleapis.com 200
```

**Result: reachable, no DOCKER-USER rule needed.** `oauth2.googleapis.com` answered with
an HTTP status; 404 is the expected answer to a bare GET on `/token`, which only accepts a
POST with an assertion. Any HTTP status proves the TCP and TLS path; a blocked path shows
as a connect error or timeout. `www.googleapis.com` (the Calendar API host, a discovery
document) returned 200, matching the 2026-10-30 prerequisite probe. The task text's "does
not return 200" assumed a URL that answers 200; the token endpoint has none for GET.
