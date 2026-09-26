# Triage Working Indicator

**For the owner, in plain terms.** When an incident triage is about to be sent to you over
Signal, you will now see "Henk is typing" in the chat while the triage runs, until the
triage message lands, exactly as you do when you ask Henk something. A triage that the daily
alert cap holds back (it is still written to the audit log and handoffs, but not sent) shows
nothing, so "typing" is never followed by silence. Nothing else changes: no new message, no
notification, no new setting. Typing appears when the triage turn starts, which is after the
120 s debounce, so it can begin up to about two minutes after the Discord/ntfy alert.

**Decisions that are yours** (my pick first):

1. **Which triages show typing.** (a) *Only triages that will be sent to you* (recommended):
   the send decision is made before the turn starts, so the indicator always ends in a
   message. (b) All triages, including cap-suppressed ones: "typing" would then sometimes end
   in nothing, which is a false promise.
2. **Which setting controls it.** (a) *The existing `signal.acknowledge_owner` flag*
   (recommended): one flag, one rollback, and a triage indicator without the chat indicator
   has no scenario. (b) A new flag just for triage typing: an extra key on rp5's
   locally-modified `config.yaml` for a behaviour you can already switch off.

## Why

A triage is the one Henk message you did not ask for, and it takes the longest: the Opus
replays took about a minute each. The Discord/ntfy alert has already told you something is
wrong; the Signal chat with Henk then stays silent until the triage lands. The
`owner-acknowledgement` change built the indicator and its bounds, and deliberately excluded
event turns ("no owner is waiting on a conversation"). In practice the owner *is* waiting:
the alert says a triage is coming. The exclusion was the conservative first step; this change
lifts it for the triages that will actually reach the owner.

It also prepares roadmap item 5 (runbook actions). Approval prompts will then occur inside
triages, and the indicator's pause-during-approval behaviour will matter there.

## What Changes

- **Event turns whose output will be sent to the owner are bracketed by the working
  indicator**, from the start of the turn (before the incident session is started) until
  after the proactive send of the triage or of the incomplete-triage notice.
- **Cap-suppressed event turns are not bracketed.** They run exactly as today.
- **The same bracket, bounds, cancellation and pause rules apply** as for owner turns: the
  `OwnerAcknowledgement.working` bracket is reused unchanged. No new module, config key,
  dependency or stored data.
- `agent-core`'s requirement *Owner agent turns are bracketed by the working indicator* drops
  its event-turn exclusion, and a new requirement covers sent event triages.
- `channel-adapter`'s *Owner-only acknowledgement of inbound messages* widens "every owner
  agent turn" to include sent event triages.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-core`: *Owner agent turns are bracketed by the working indicator* no longer excludes
  event turns (the exclusion and its scenario move to the new requirement, narrowed to
  cap-suppressed triages); ADDED *Sent event triages are bracketed by the working indicator*.
- `channel-adapter`: *Owner-only acknowledgement of inbound messages* shows the working
  indicator for every owner agent turn **and every event turn whose output will be sent to
  the owner**.

## Impact

- **Code:** `henk/agent/core.py` (`_process_event` enters the existing `_working()` bracket
  when the turn is announceable). No change to `henk/channel/`, `henk/app.py`,
  `henk/runtime.py` or config: the runtime already passes the bracket to the core.
- **Tests:** a new event-turn section beside `tests/test_agent_core_acknowledgement.py`, plus
  one `App.run` guard.
- **Deployment:** image rebuild only. No config edit, volume, port or ACL change. Rollback is
  the existing `signal.acknowledge_owner: false`, which now also turns off triage typing, or
  the previous image.

## Left out, deliberately

- **Typing for owner commands.** Still excluded; they answer within one round trip.
- **Typing for other proactive sends** (reminder deliveries, the degraded-durability notice
  on its own): they are not turns and take no time.
- **Any change to when a triage is sent**, the cap or the debounce.
