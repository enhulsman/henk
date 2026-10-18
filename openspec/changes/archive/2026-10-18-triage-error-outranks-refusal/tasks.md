# Tasks — Triage error outranks refusal

- [x] Cover mixed error and refusal signals, the CLI-rendered API error text
      heuristic, and owner session setup failure in tests.
- [x] Classify errors before refusals and send the generic owner error reply
      when session setup fails (commit 86f5043).
- [x] Update the incident-triage and agent-core delta specs to match the
      implemented behavior.
