# Triage error outranks refusal

## Why

Review 028 found that a refusal stop reason could mask a real error in the same
turn. On owner turns, the masked error could allow raw API error text to reach the
owner instead of the generic error reply. The ending precedence and session
setup failure reply were corrected in commit 86f5043; the specs need to record
the shipped behavior.

## What Changes

- Classify every triage error signal, including the CLI-rendered API error text
  heuristic, before a refusal. Preserve the existing incomplete-triage notice
  and audit outcome behavior.
- Apply the same precedence to owner agent turns; an error sends the generic
  error reply instead of model text, even when the turn also reports a refusal.
- State that a failed owner session setup sends the generic error reply before
  the working indicator stops.
- Record the implemented precedence and owner-turn behavior in delta specs.

## Capabilities

### Modified Capabilities

- `incident-triage`: error signals outrank refusal in the incomplete-triage
  ending requirement.
- `agent-core`: owner errors outrank refusals, and failed session setup sends
  an error reply within the working-indicator bracket.
