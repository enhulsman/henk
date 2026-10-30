# Pre-merge credential test (owner request, 2026-10-30)

Before merging, the real service-account key was exercised from the workstation with
this branch's own code (`parse_service_account_key`, `AccessTokenSource`,
`CalendarClient`), against the real Agents calendar. The key never entered the repo, a
log or the transcript; the script printed statuses and counts only.

```
key: ok (service-account JSON, pinned token_uri, RSA private key)
sign-in: ok (Google issued an access token for the calendar.events scope)
read: ok, 1 Homelab event(s) admitted in the next 14 days (1 creator(s) allowlisted, plus Henk's own account)
write: ok, 'Homelab: Henk write test' tomorrow 10:00-10:15; admitted count 1 -> 2 (Henk's own events are admitted)
```

Read back through the owner's Google Calendar connector: the event was created by
Henk's service account, 10:00 to 10:15 `+01:00` with time zone `Europe/Amsterdam`
(correct after the 2026-10-25 DST change), status confirmed, with no description, no
attendees and no location.

Proven live: the key format and `token_uri` pin, the hand-rolled JWT-bearer sign-in,
the calendar share ("make changes to events"), the field mask and admission rule (the
owner-created `Homelab:` event admitted with the owner allowlisted, Henk's own event
admitted without configuration), and the insert body. Still for §8.4: the same from
inside the rp5 container, the approval gate end to end over Signal, and hold/release.
