## MODIFIED Requirements

### Requirement: Triage framing directs the method, not only the arc
The triage-mode framing SHALL direct the investigation's method as well as its output.
It SHALL instruct the agent to:
- determine **which condition of the rule fired, and when**, from the alert's values and
  its notification time, bearing in mind that the notification can come later than the
  condition's onset. When a rule has more than one branch, **check every branch's own
  measurement**, because an alert's value does not always identify the branch that
  fired;
- use, for each measurement, **the shortest available window that reaches back past the
  alert's notification time minus the rule's `for` window**, and read longer windows as
  context rather than as the incident. Every window ends at the time of the query, so a
  window matching the `for` alone would miss the alert's onset;
- name the responsible process, container, host systemd unit or node where a tool can
  show it;
- write **"evidence not available: <what>"** where the evidence is not available from any
  tool, instead of inferring it;
- treat remembered facts, when the turn carries them, as **context and not an override of
  what the evidence shows**;
- when the digest shows a **same-window** prior handoff, treat it as shown **for its
  timing alone**: state whether it shares this incident's cause, and build on it only
  when the evidence gathered supports that;
- consult `homelab_docs` before diagnosing, **only when that tool is registered** in the
  session.

The framing SHALL name no tool the session does not have. It SHALL keep the existing arc
mandate: the `Diagnosis:` line with confidence, `Fix:`, and `Pickup:`, each on its own
line, checked by the application after the turn. It SHALL keep the handoff instruction,
which SHALL ask for the time each evidence figure refers to. The framing SHALL sit after
the untrusted-data block. It is defence-in-depth only: the closed-toolset hook and the
approval gate remain the boundary, whatever the framing says.

#### Scenario: Framing directs branches, window, and missing evidence
- **WHEN** an event turn is composed
- **THEN** the framing instructs the agent to check every branch's measurement, to use the shortest window reaching back past the notification time minus the rule's `for`, and to write "evidence not available: <what>" rather than infer missing evidence

#### Scenario: The handoff instruction asks for times
- **WHEN** an event turn is composed
- **THEN** the handoff instruction asks for the time each evidence figure refers to

#### Scenario: Memory is framed as context only when present
- **WHEN** an event turn is composed with a recall block, and another without one
- **THEN** the first framing states that remembered facts are context and not an override of the evidence, and the second carries no such line

#### Scenario: Docs are named only when registered
- **WHEN** an event turn is composed for a session whose registry includes `homelab_docs`, and another for a session whose registry does not
- **THEN** the first framing instructs the agent to consult `homelab_docs`, and the second framing does not mention `homelab_docs`

#### Scenario: The arc and its check are unchanged
- **WHEN** a triage reply carries the `Diagnosis:` (with confidence), `Fix:` and `Pickup:` lines under the new framing
- **THEN** the application's arc check reports the arc complete exactly as before

#### Scenario: Framing follows the untrusted block
- **WHEN** an event turn is composed
- **THEN** the method framing appears after the end marker of the untrusted-data block, and no framing text appears inside it

#### Scenario: A same-window handoff is framed as timing only, only when shown
- **WHEN** an event turn is composed with a digest showing a same-window entry, and another whose digest shows none
- **THEN** the first framing states that the same-window handoff is shown for its timing alone and asks whether it shares this incident's cause, and the second framing is identical to the framing without that step
