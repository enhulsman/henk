# Key placement and live checks on rp5 (tasks 8.3, 8.4; 2026-10-30)

The owner ran every rp5 command in their own terminal. The calendar id, the owner's
account and Pam's service-account email came from the owner's Google Calendar connector
and went only into rp5's `config.yaml`. The key went only into rp5's `.env`.

## 8.3 Key placement, deploy, credential test, flip

- Key copied into a mode-700 directory on rp5, base64'd at mode 600, inserted into
  `.env` with nano's read-file (never on a command line). Checked by count and length
  only: one `GOOGLE_CALENDAR_SA_KEY_B64=` line, 27 + 3120 characters (the 2340-byte JSON
  in base64). The directory was then removed.
- `calendar:` (enabled false, calendar id) and `personal_data.calendar_creator_allowlist`
  (owner, Pam) added to the live `config.yaml`; one `calendar:` and one `personal_data:`
  section, `owner.timezone` set.
- Deploy per the README, pull to `97790a9`. **`up -d --build` failed on Compose v5.6.1**:
  `additional privileges requested: pass "--allow=network.host"`. Compose v5 builds only
  through `buildx bake`, which needs the `network.host` entitlement for `build.network:
  host`, and Compose cannot grant it (`COMPOSE_BAKE=false` ignored; docker/compose#14345).
  Built instead with `docker compose ... build --print henk | docker buildx bake --file -
  --allow=network.host`, then `up -d --no-deps henk`. All three deploy tells were real
  (`COPY henk ./henk` rebuilt, `pip install` re-ran, new image sha, `Started`), and the
  `since=` line showed the real audit volume. The README redeploy recipe now uses this.
- Credential test from the deployed image, calendar off: `config: ok`, `creator
  allowlist entries: 2`, `sign-in: ok`, `calendar: ok, 1 Homelab event(s) admitted in
  the next 14 days, 1 would hold alerts`. The one event was the owner's real
  `Homelab: prune rollback images`. The pre-merge write-test event had been deleted
  first, through the connector.
- Flip: `enabled: true`, `up -d --no-deps --force-recreate henk`. Startup logged `no
  calendar snapshot ... yet` and `calendar window provider started; refreshing every
  300s`; token `200`, events list `200`, with the calendar id redacted to `<calendar>`
  in the httpx line.

## 8.4 Live checks over Signal

1. **"What homelab work is planned?"** Henk listed exactly the one admitted event (Sun 1
   Nov 10:00-10:15 CET), said it holds alerts for the window plus a short grace, and
   did not show the non-`Homelab` Pam and owner events on the same calendar.
2. **Create answered `no`.** The approval prompt showed tool, title, start and end;
   after `no` Henk said nothing was written. The connector found no such event.
3. **Create answered `yes`.** "Homelab: hold test" was created; the connector showed it
   with Henk's own service account as creator. Henk's reply stated the hold to the
   window end plus grace. A follow-up "move that window" was declined truthfully
   (create only, no edit tool), with no attempt to work around it.
4. **Hold and release.** The owner moved the window to 21:55-22:05 CET in Calendar, and
   after the next refresh one synthetic Gatus failure (`Gatus: calendar/hold-probe`)
   was published to `henk-events` at 22:03. Log: `held event ... (gatus:calendar/
   hold-probe) in maintenance window ...` at 22:05:17 (after the debounce), no Signal
   message during the window. At 22:35:55 (end 22:05 + 30 min grace): `released
   maintenance window ...: 1 held, 0 recovered`, followed by the triage message. It
   ended with the release line `(Held during "Homelab: hold test", 21:55–22:35: 1
   alert.)`. The triage itself correctly found no Gatus endpoint by that name, all 19
   real endpoints healthy, and called it a probable leftover test alert.

Observed, not changed: the triage's first paragraph gives the window in UTC before CET.

Domain-wide delegation (8.3 step 4): the service account's page shows the standard
yellow notice and a Client ID, which every service account shows; delegation can only be
granted from a Google Workspace Admin Console. The `pam-claude` project is under "No
organization" (a personal account), so there is no Workspace that could grant it.
