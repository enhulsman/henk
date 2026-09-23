## ADDED Requirements

### Requirement: Every triage is recorded to the audit volume
When triage recording is enabled (default: enabled), the application SHALL write one
recording per event triage when the triage turn completes. That includes a triage that
errored, was refused, or produced no reply. The recording SHALL contain:
- a recording id and a timestamp;
- a flag stating that it was recorded live, not reconstructed;
- the triaged incidents with their times;
- the composed content exactly as it was passed to the agent session;
- the model, effort and thinking the session ran with;
- hashes of the system prompt and of the tool definitions;
- every tool call of the turn in order, with its name, arguments, result text, and
  whether it was an error, including calls the hook or gate denied;
- the reply;
- the triage's ending classification (incident-triage spec).

Recordings SHALL validate against a versioned recording schema committed to the
repository. The tool-call capture SHALL be separate from the audit record's tool-call
capture, and SHALL NOT widen the set of tools whose results enter audit records. A
recording write failure SHALL be logged at error level and SHALL NOT affect the triage,
its audit record, its message, or the intake checkpoint.

#### Scenario: A triage leaves a recording
- **WHEN** an event triage that called three tools completes
- **THEN** a recording exists holding the composed content byte-for-byte as sent, the profile, the three calls with arguments and result text in order, the reply, and the ending, marked as recorded live, and it validates against the committed recording schema

#### Scenario: Denied calls are recorded too
- **WHEN** the agent attempts a mutating tool during a triage and the gate denies it
- **THEN** the recording contains that call with its arguments and the denial text the model received

#### Scenario: An errored triage is still recorded
- **WHEN** a triage turn raises an error after two tool calls
- **THEN** a recording exists with those two calls and ending `error`

#### Scenario: Recording does not widen audit capture
- **WHEN** a triage is recorded and its audit record is inspected
- **THEN** the audit record's tool calls carry result text only for the handoff tool, exactly as before

#### Scenario: A recording failure does not disturb the triage
- **WHEN** the recording cannot be written
- **THEN** the triage's message, audit record and checkpoint advance proceed unchanged, and an error is logged

#### Scenario: Recording can be turned off
- **WHEN** triage recording is disabled in configuration
- **THEN** no recording is written, and triage behaves otherwise unchanged

### Requirement: Recordings are bounded and are not audit records
Each recording SHALL be bounded by a fixed size of 256 KB. When a recording would exceed
that bound, the largest tool results SHALL be shortened, each with an explicit
truncation marker stating how many bytes were removed, and the recording SHALL be flagged
incomplete. Recordings SHALL be retained up to a fixed count of 200 and a fixed age of 30
days. After each write, recordings beyond either bound SHALL be deleted oldest first,
together with any replay output for them.

Reference cases, which are graded cases the owner keeps deliberately, SHALL live in their
own directory beside the recordings. A case is a subdirectory holding a `case.json`, and
only subdirectories with a readable `case.json` SHALL count as cases or be listed. Other
entries, such as raw or capture material, and unreadable entries SHALL be skipped. Cases
SHALL NOT be subject to the recordings' count or age pruning. They SHALL be bounded by a
fixed count of 20. Adding a case beyond that
bound SHALL be refused with an error naming the bound, and an existing case SHALL never
be evicted to make room. The case directory SHALL be readable by the henk image's
unprivileged user.

Recordings are not audit records. They SHALL use their own file family and schema. They
SHALL NOT be read by cadence rehydration. Their deletion by retention SHALL NOT be treated
as modifying the audit log.

#### Scenario: An oversized recording is marked, not silently cut
- **WHEN** a triage's tool results exceed the recording bound
- **THEN** the recording is within the bound, each shortened result carries a truncation marker with the bytes removed, and the recording is flagged incomplete

#### Scenario: Retention holds its bounds
- **WHEN** a recording is written while the maximum count is reached or older recordings exceed the maximum age
- **THEN** afterwards no more than the maximum count remain, none is older than the maximum age, and the removed recordings' replay outputs are removed with them

#### Scenario: Reference cases survive recording retention
- **WHEN** recording retention prunes by count or age while reference cases older than 30 days exist
- **THEN** every reference case remains, and adding a case beyond the case bound is refused naming the bound

#### Scenario: Only case.json directories are cases
- **WHEN** the case directory holds two case subdirectories with `case.json`, one raw-material subdirectory without it, and one unreadable entry
- **THEN** the case listing and the case count show exactly the two cases, and the unreadable entry is skipped with a warning

#### Scenario: Rehydration ignores recordings
- **WHEN** the process starts with recordings present on the audit volume
- **THEN** cadence rehydration reads only the audit log, and its result is unchanged by the recordings

### Requirement: The owner can replay a recorded triage against a chosen profile
The application SHALL provide an owner-invoked command-line entry point that re-runs a
recorded triage against an owner-chosen model, effort and thinking mode. It SHALL be able
to:
- list recordings;
- list reference cases;
- run a replay;
- compare runs;
- grade runs;
- rebuild a reconstructed case from preserved and captured material.

The model argument SHALL match the pattern of a Claude model identifier, and the effort
SHALL be one of the SDK's effort levels. Both SHALL be validated before any model call is
made.

A replay SHALL send the recorded composed content unchanged, with the system prompt
composed from the current configuration. A difference between the current and recorded
system-prompt or tool-definition hashes SHALL be reported in the run output. Each replay
SHALL write a run file beneath the replay-output directory, containing:
- its reply;
- its tool calls;
- captured handoffs;
- authorization decisions;
- the count of unrecorded calls;
- usage;
- the profile;
- drift flags.

#### Scenario: A replay runs a recording on another model
- **WHEN** the owner runs a replay of a recording with a different model and effort
- **THEN** the replayed session runs on that model and effort with the recorded composed content, and a run file with the reply, tool calls and profile is written

#### Scenario: Drift is reported, not hidden
- **WHEN** the tool definitions have changed since the recording was made
- **THEN** the run output states that the tool definitions differ from the recording's

#### Scenario: An invalid effort spends nothing
- **WHEN** the owner passes an effort outside the SDK's effort levels
- **THEN** the command refuses with the accepted values and no model call is made

#### Scenario: A malformed model identifier spends nothing
- **WHEN** the owner passes a model argument that does not match the Claude model identifier pattern (for example one containing a space or a slash)
- **THEN** the command refuses, naming the expected form, and no model call is made

### Requirement: Replay serves recorded results and says when it cannot
A replay session's tools SHALL carry the current tool definitions. They SHALL be served by
a replay registry that returns the recorded results:
- A call SHALL match a recorded call when the name and the canonicalized arguments are
  equal.
- Repeated identical calls SHALL be served in recorded order.
- A recorded error SHALL be served as an error.

A call with no unconsumed recorded match SHALL return an explicit error result stating
that the call is **not recorded in this replay**. It SHALL NOT return an empty result, a
guessed result, or a live result, and it SHALL be counted in the run output. The handoff
and notify tools SHALL capture their document or message into the run output and return
a replay-marked result. Mutating tools SHALL NOT execute in replay.

#### Scenario: A recorded call is served its recorded result
- **WHEN** the replayed model calls a tool with the same name and arguments as a recorded call
- **THEN** it receives the recorded result text, and a recorded error is received as an error

#### Scenario: An unrecorded call is answered honestly
- **WHEN** the replayed model calls a tool with arguments the original triage never used
- **THEN** it receives an error result stating the call is not recorded in this replay, and the run output counts it

#### Scenario: A handoff in replay is captured, not published
- **WHEN** the replayed model calls `publish_handoff`
- **THEN** the document is stored in the run output and the model receives a replay-marked result

### Requirement: Replay keeps the boundary and cannot reach live outputs or state
A replay session SHALL be constructed with the same closed-toolset hook, the same empty
auto-approve list, and the same permission callback and approval gate as a live session.
The gate SHALL be framed for a tainted, non-announceable event turn, so every mutating call
is denied as out of scope.

No tool SHALL be able to originate a network request in replay. The replay SHALL be
structurally unable to publish to ntfy, send over Signal, write the audit log, or write
Henk's store. The model call made by the agent SDK is the replay's only network traffic.
The replay entry point SHALL construct no channel adapter, event intake, ntfy client,
audit writer or store connection. Tool definitions SHALL be built over a transport that
refuses every request. Authorization decisions made in replay SHALL be written to the run
output and never to the audit log.

#### Scenario: A replay that tries everything reaches nothing
- **WHEN** a replayed model attempts every registered tool, including the handoff, notify and mutating tools
- **THEN** no tool originates an HTTP request, no store file is opened, no audit line is written, no channel send occurs, and every mutating attempt is denied with an out-of-scope decision recorded in the run output

#### Scenario: The hook still blocks built-ins
- **WHEN** a replayed model attempts a host built-in tool
- **THEN** the closed-toolset hook blocks it exactly as in a live session

### Requirement: Replays are graded side by side and by a no-tool judge against a versioned rubric
The entry point SHALL print, to the owner's terminal, a side-by-side comparison of the
original triage and chosen replay runs. For each it shows:
- the model and effort;
- the ending, arc completeness and confidence;
- the arc lines;
- the start of the handoff;
- the tool calls in order, with unrecorded calls marked;
- token usage.

It SHALL send nothing anywhere.

The entry point SHALL provide an LLM judge that runs on a configured judge model (default
`claude-fable-5-1`). The judge scores each candidate handoff and reply against a written
rubric, committed to the repository under a version. The rubric's criteria SHALL be:
- evidence use;
- rule-branch correctness;
- confidence calibration;
- fix quality;
- honesty about missing evidence.

Each criterion has anchored score levels. The rubric SHALL state that a "not recorded in
this replay" result is a harness limit and not the candidate's failure.

The judge SHALL have no tools. Its session SHALL be built with an empty tool registry
behind the same closed-toolset hook. The judge SHALL receive, all inside a delimited data
block:
- the rubric;
- the recorded incident;
- the original evidence;
- the case's verified reference, when the case has one;
- the candidates.

Candidates SHALL be labelled without model names, in a randomized order whose seed is
recorded. When a reference is present, the judge SHALL be told that it is the owner's
verified ground truth, and candidates SHALL be scored against it.

A grade SHALL record:
- per candidate and criterion, a score and a one-line reason;
- the rubric's version and content hash;
- whether a reference was used;
- the judge model;
- the seed.

Judge output that does not parse SHALL be recorded as unparseable, with the raw text kept.
A judge refusal SHALL be recorded as refused. Neither SHALL ever be converted into scores.
Grades SHALL be written beneath the replay-output directory.

#### Scenario: Side-by-side comparison
- **WHEN** the owner compares a recording's original triage with two replay runs
- **THEN** the terminal shows each one's model, effort, ending, arc, handoff start, tool calls with unrecorded ones marked, and tokens, and nothing is sent to any channel or topic

#### Scenario: The judge scores against the committed rubric
- **WHEN** the owner grades two runs of a recording
- **THEN** a grade file holds a score and reason for each of the five criteria per candidate, the rubric version and hash, the judge model, and the ordering seed

#### Scenario: A verified reference sharpens the grade
- **WHEN** the owner grades runs of a case that carries a verified reference naming the branch that fired and the culprit
- **THEN** the judge's input carries the reference marked as verified ground truth, and the grade records that a reference was used

#### Scenario: The judge has no tools
- **WHEN** the judge session is constructed
- **THEN** its tool registry is empty, and any tool the judge attempts is denied by the closed-toolset hook

#### Scenario: The judge is blind to models
- **WHEN** the judge's input is inspected
- **THEN** candidates are labelled without model names, in an order matching the recorded seed

#### Scenario: Unparseable judge output is not a score
- **WHEN** the judge returns text that does not parse as the required structure
- **THEN** the grade is recorded as unparseable with the raw text, and no score is recorded

#### Scenario: A judge refusal is recorded as refused
- **WHEN** the judge's session ends with a refusal
- **THEN** the grade is recorded as refused, with no scores

#### Scenario: A rubric change is a new version
- **WHEN** the rubric's criteria or anchors change
- **THEN** the change is a new rubric version file, the previous version remains committed, and grades name the version they used

### Requirement: Reconstructed cases are built from captured backend data and say what they are
A case rebuilt after the fact SHALL be built only from evidence that exists, and SHALL be
flagged as reconstructed.

**Its `homelab_query` results** SHALL come only from **Prometheus expressions**:
- The results are raw Prometheus responses captured at a stated evaluation time, across
  every in-domain argument combination of every Prometheus-backed query, and rendered
  through the current renderers.
- They SHALL NOT come from inferred or remembered tool output, and SHALL NOT be derived
  by re-evaluating a range export.
- A query that is only partly Prometheus-backed SHALL be rendered from its captured
  expressions, with its non-Prometheus part stated as unavailable in reconstruction.
  `scrape_targets` is such a query: its targets API has no historical form.
- A query backed by another system SHALL be unavailable in reconstruction.
  `endpoint_history`, which reads Gatus, is such a query.
- Before serving a captured response, the rebuild SHALL compare the captured expression
  with the registry's current expression for the same role and arguments. On a mismatch
  it SHALL serve an "unavailable in reconstruction" result for that call, stating the
  mismatch, and SHALL list the mismatch in the case.

**Every other tool** SHALL return an explicit "unavailable in reconstruction" result.
That covers tools whose backend data was not captured, and any tool whose content would
reveal the answer, such as documentation updated after the incident. The rubric SHALL
treat such a result as a harness limit, not a model fault.

The **original call sequence** SHALL keep the tool names from the audit record, with
arguments recorded as unknown.

The **composed content** SHALL be built from the preserved event payload through the
current composer, with no recall block and no digest, so that neither memory nor history
can carry the answer.

The case's run and grade output SHALL state:
- that it is reconstructed;
- its capture time and the capture-time uncertainty;
- that it grades the current renderers' evidence, not the evidence the original triage
  saw.

A reconstructed case SHALL be stored as a reference case on the audit volume. Only a
placeholder-safe fixture of the same shape SHALL be committed.

#### Scenario: A reconstructed case says what it is
- **WHEN** a reconstructed case is replayed and graded
- **THEN** the run and grade output state that it is reconstructed, its capture time and uncertainty, and that it grades the current renderers rather than the original evidence

#### Scenario: Any in-domain query is answerable from the capture
- **WHEN** a replayed model calls a Prometheus-backed `homelab_query` with any in-domain argument combination on a reconstructed case
- **THEN** it receives the captured response for that combination rendered through the current renderer

#### Scenario: scrape_targets is rendered from up, with the targets part unavailable
- **WHEN** a replayed model calls `scrape_targets` on a reconstructed case
- **THEN** it receives targets and values rendered from the captured `up` and `up_over_window` at the case's evaluation time, and the last-scrape-error part is stated as unavailable in reconstruction

#### Scenario: A missing targets payload is not read as "no error"
- **WHEN** a reconstructed `scrape_targets` result is rendered for a down target
- **THEN** its error part reads "unavailable in reconstruction", and it never reads "no scrape error recorded by the backend"

#### Scenario: A Gatus-backed query is unavailable
- **WHEN** a replayed model calls `endpoint_history` on a reconstructed case
- **THEN** it receives an "unavailable in reconstruction" result

#### Scenario: A drifted template is not served
- **WHEN** a captured expression differs from the registry's current expression for the same role and arguments
- **THEN** that call receives an "unavailable in reconstruction" result naming the mismatch, and the case lists it

#### Scenario: Uncaptured tools are unavailable, not guessed
- **WHEN** a replayed model calls `homelab_health` or `homelab_docs` on a reconstructed case
- **THEN** it receives an "unavailable in reconstruction" result, and the grade does not count it against the candidate

#### Scenario: The case cannot leak the answer through memory or history
- **WHEN** a reconstructed case's composed content is inspected
- **THEN** it contains no recall block and no digest

#### Scenario: Original arguments are not invented
- **WHEN** a reconstructed case's original call sequence is inspected
- **THEN** each call carries its tool name and arguments recorded as unknown

### Requirement: The capture covers the closed argument space at a stated time
The capture used to reconstruct a case SHALL record the raw Prometheus JSON for every
PromQL expression of every registry entry, for every combination of its parameter values,
plus any template this change adds that is not yet in the registry. HTTP routes that have
no historical form, such as the targets API, and non-Prometheus backends SHALL NOT be
captured. Each saved response SHALL record the exact expression sent, its role, and the
range point budget and step it used, so that a rebuild can detect drift. The capture
SHALL use the deployed instance's effective range point budget.

Templates this change adds SHALL have a single canonical spelling, which is the text sent
on the wire, and fixed role names. The registry's templates SHALL be byte-equal to the
templates the capture used. Named-container follow-ups SHALL be captured per container
name, for every name present in that evaluation time's own container results.

Instant queries SHALL carry the evaluation time, and range queries SHALL end at it, with
the registry's step. The capture SHALL run at several evaluation times spanning the
interval in which the original triage could have queried. That interval runs from the
alert's notification time plus the debounce, to the triage record's audit time. The
capture SHALL record, for each response, the evaluation time, template, arguments, HTTP
status and response body.

Captured responses carry tailnet addresses. They SHALL be persisted only on rp5's audit
volume, and SHALL NOT be committed. Any transit copy, such as a workstation scratch
directory or an rp5 staging directory, SHALL be deleted after transfer, and the transfer
SHALL be recorded in the change's evidence-probe notes. The capture SHALL refuse any
output path that is not an existing mode-700 directory owned by the invoking user.

#### Scenario: Instant captures are pinned to the evaluation time
- **WHEN** the capture issues an instant query for evaluation time T
- **THEN** the request carries `time=T`, and a range query carries `end=T` with the registry's step

#### Scenario: Registry and capture templates are byte-equal
- **WHEN** the registry's added templates and role names are compared with those the capture sent
- **THEN** every template is byte-equal and every role name matches

#### Scenario: A point-budget change is drift
- **WHEN** the current range point budget differs from the one recorded in a captured range response
- **THEN** the rebuild serves "unavailable in reconstruction" for that call and lists the mismatch

#### Scenario: The capture refuses an unsafe output path
- **WHEN** the capture is given an output path that does not exist, is not mode 700, or is owned by another user
- **THEN** it refuses before issuing any request

#### Scenario: The capture covers every argument combination
- **WHEN** the capture's request set is compared with the registry's parameter domains and the added templates
- **THEN** every PromQL expression of every entry is present for every in-domain combination, and no targets-API or Gatus request is present

### Requirement: Replay test fixtures carry no real homelab data
Every recording, run, case and grade fixture in the test suite SHALL use placeholder
hostnames and documentation-range addresses (RFC 5737). No fixture SHALL contain a tailnet
address, a real phone number, an account identifier, or memory content from the live
store.

#### Scenario: Fixtures pass the publication checks
- **WHEN** the replay fixtures are scanned by the repository's pre-commit checks
- **THEN** no tailnet address, real phone number or account identifier is found
