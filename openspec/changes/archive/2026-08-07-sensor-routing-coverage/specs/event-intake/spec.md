## MODIFIED Requirements

### Requirement: Stable alert identity
The intake SHALL derive a stable alert-identity key from each event's contract fields (source, alert-or-endpoint name — see sensor-routing's payload contract) via per-source derivation rules that are configurable, with a documented deterministic fallback (normalized title) for nonconforming events. This identity key SHALL be the key used for cooldown, dedup, and recurrence detection.

Where a single alert name can fire concurrently for several distinct subjects — several scrape targets, several containers — the identity SHALL be scopeable by an additional discriminator, so that distinct subjects yield distinct identity keys and one subject's incident cannot suppress another's. The discriminator SHALL be **opt-in per alert**, declared by the sensor alongside the alert itself rather than configured separately in the intake, and an alert that does not declare one SHALL derive exactly the key it derives today. A fire and its later resolve for the same subject SHALL continue to share one key, so the discriminator SHALL be drawn from the alert's own labelling and never from its state.

#### Scenario: Re-fire maps to the same identity
- **WHEN** the same source publishes the same alert twice
- **THEN** both events derive the same identity key

#### Scenario: Nonconforming event still keyed
- **WHEN** an event does not match the payload contract
- **THEN** it receives a deterministic fallback identity key and is processed normally

#### Scenario: One alert name firing for two subjects
- **WHEN** an alert that declares an identity discriminator fires concurrently for two distinct subjects
- **THEN** the two events derive two distinct identity keys, and neither subject's cooldown suppresses the other

#### Scenario: Resolve pairs with its fire
- **WHEN** an alert that declares an identity discriminator fires for a subject and later resolves for that same subject
- **THEN** both events derive the same identity key

#### Scenario: Alert declaring no discriminator is unaffected
- **WHEN** an alert that declares no identity discriminator fires
- **THEN** it derives the same identity key it derived before this capability existed
