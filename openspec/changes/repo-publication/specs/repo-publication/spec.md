# repo-publication

## ADDED Requirements

### Requirement: Publication hygiene is enforced automatically
The repository SHALL enforce its publication-safety rules through an automated pre-commit
gate rather than author discipline. The gate SHALL be version-controlled inside the
repository (not only in a local `.git/hooks/` directory) so that it survives a fresh clone,
and SHALL run two independent layers: a secret scanner over the staged content, and
repo-specific pattern checks for the shapes the scanner has no rule for — tailnet addresses
in the carrier-grade NAT range Tailscale allocates from, the non-allowlisted domain that
must not appear in this repository, service-token-shaped strings, and phone numbers other
than the documented test placeholders.

The gate SHALL examine only lines being **added**, so that removing an offending value is
never itself blocked. A deliberate bypass SHALL remain possible for false positives.

#### Scenario: A tailnet address is caught before it is published
- **WHEN** a commit is staged whose added lines contain an address in the tailnet range
- **THEN** the commit is blocked and the offending line is reported

#### Scenario: A sanctioned placeholder does not block
- **WHEN** a commit is staged containing a documented test placeholder phone number
- **THEN** the commit proceeds

#### Scenario: Removing an offending value is not blocked
- **WHEN** a commit is staged that only deletes a line containing an offending value
- **THEN** the commit proceeds

### Requirement: A missing secret scanner fails the commit
When the secret scanner is not installed, the gate SHALL fail the commit with installation
instructions rather than skipping the scan. A silently-skipped scanner is worse than none,
because a commit that was never scanned is indistinguishable from one that scanned clean.

#### Scenario: Scanner absent
- **WHEN** a commit is attempted on a machine with no secret scanner installed
- **THEN** the commit is blocked and the message explains how to install it and how to bypass

### Requirement: Pre-flip audit precedes the visibility change
Before repository visibility is changed, a full-history secret scan SHALL pass and the
commits added since the history scrub SHALL be audited for the repo-specific patterns. The
audit's known benign match classes — documentation quoting the search patterns themselves,
and the sanctioned test placeholders — SHALL be recorded alongside the result, so that a
later operator can distinguish a real hit from expected noise.

#### Scenario: Audit result is interpretable
- **WHEN** the pre-flip audit reports matches
- **THEN** each match is classified as benign or real, and only real matches block the flip

### Requirement: The visibility change is owner-executed
Changing repository visibility SHALL be performed by the owner, not by an assistant or
automation. The change is effectively irreversible — published content can be cloned,
cached, and indexed within minutes, and reverting visibility does not unpublish it.

#### Scenario: Assistant prepares but does not flip
- **WHEN** all pre-flip prerequisites are met
- **THEN** the exact command is prepared and handed to the owner, who runs it
