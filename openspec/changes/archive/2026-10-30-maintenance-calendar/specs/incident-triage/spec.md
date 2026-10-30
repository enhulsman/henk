## ADDED Requirements

### Requirement: Held events are triaged only when released
An event held during a maintenance window (event-intake spec) SHALL NOT start a triage session while it is held. Holding is not suppression: a held event has not yet been through cooldown or the cap. When a release forwards held events, the triageable survivors SHALL be triaged in one session, under the same cap, handoff and recording rules as any event turn. The triage framing SHALL tell the session three things: these alerts fired during the named planned maintenance window; they were still not resolved when the window and its grace ended, which is itself the finding; and their live state must be checked before anything is concluded. When the hold's age exceeds 24 hours, the framing SHALL state the age and that the held state may be stale.

#### Scenario: No triage while held
- **WHEN** an event is held at 20:40 in a 20:00–22:00 window
- **THEN** no triage session exists for it before the window is released

#### Scenario: Released triage is framed by its window
- **WHEN** a release forwards identity A from window `Homelab: patch round`
- **THEN** the triage session's framing names the window, states that A was still unresolved after the grace, and directs a live check

#### Scenario: Old hold is framed as possibly stale
- **WHEN** a window is released 30 hours after its end
- **THEN** the framing states the hold's age and that the held state may be stale
