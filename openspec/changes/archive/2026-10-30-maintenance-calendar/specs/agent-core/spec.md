## ADDED Requirements

### Requirement: Owner turns carry the maintenance-window note
When a maintenance window is active, or one was released within the last 24 hours, every owner agent turn SHALL be prefixed with a delimited block, framed as data and not instructions. It SHALL sit after the current-time header and before the recall block. For each such window it SHALL state: the title inside the argument delimiters; the start and end, and the instant until which alerts are held, all through the shared time renderer; and the status. The status is either in progress, with the number of distinct alerts held so far, or ended, with the numbers of distinct alerts held, recovered and forwarded for triage. The block SHALL contain no alert payload text. It SHALL be composed per turn, and a failure to compose it SHALL be logged while the turn proceeds without it. The block SHALL NOT taint the session, and event turns SHALL NOT carry it.

#### Scenario: Owner asks during a patch round
- **WHEN** the owner sends "is everything ok?" at 20:45 during a 20:00–22:00 window that has held 3 distinct alerts
- **THEN** the turn content contains the maintenance block naming `<<<Homelab: patch round>>>`, `in progress`, `alerts held until Saturday 7 November at 22:30 CET` and `3 alerts held so far`, after the time header and before the owner's text

#### Scenario: Ended window remembered for a day
- **WHEN** the owner sends a message 5 hours after a window's release that held 4, recovered 3 and forwarded 1
- **THEN** the turn content contains the block stating it ended with 4 held, 3 recovered and 1 forwarded for triage

#### Scenario: Calendar trouble never blocks a turn
- **WHEN** composing the block raises an error
- **THEN** the turn runs without the block, and the error is logged

### Requirement: A released triage says what the window held
When a turn produced by a hold release sends anything to the owner, the core SHALL append one line after any suppressed-count note: to the triage reply, and equally to the incomplete-triage notice. The line SHALL be composed by code from the turn's hold data, never by the model, and SHALL read, when at least one alert recovered:

`(Held during "<window title>", <start>–<end plus grace>: <held> alerts, <recovered> of which recovered on their own.)`

and when none recovered:

`(Held during "<window title>", <start>–<end plus grace>: <held> alert.)` for one alert, or `... <held> alerts.)` for more.

Counts are distinct alert identities. The times are compact 24-hour `HH:MM` in the owner's zone. This is a stated exception to the shared time renderer: both times fall in one evening, and the line names its window. A non-announceable released turn SHALL send nothing, like any capped turn.

#### Scenario: Release line with recoveries
- **WHEN** a released turn for window `Homelab: patch round` (20:00–22:00, grace 30), with 4 held and 3 recovered, produces reply text R and is announceable
- **THEN** the sent message is R, a blank line, and `(Held during "Homelab: patch round", 20:00–22:30: 4 alerts, 3 of which recovered on their own.)`

#### Scenario: Release line without recoveries
- **WHEN** a released turn for the same window with 1 held and 0 recovered is announceable
- **THEN** the appended line is `(Held during "Homelab: patch round", 20:00–22:30: 1 alert.)`

#### Scenario: Incomplete triage still carries the line
- **WHEN** a released turn ends incomplete and the incomplete-triage notice is sent
- **THEN** the notice ends with the release line

#### Scenario: Live turns carry no release line
- **WHEN** an event turn not produced by a release is sent
- **THEN** no release line is appended
