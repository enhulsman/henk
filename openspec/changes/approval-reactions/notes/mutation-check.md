# Mutation check (task 2.7)

Run 2026-10-31 on the green implementation, one mutation at a time, against
`test_approval_reactions.py`, `test_prompt_rendering.py`, `test_reaction_intake.py`,
`test_gate_authorization.py` and `test_approval_gate.py`. Each file was restored after
its run.

| Mutation | Where | Result |
|---|---|---|
| Drop the reaction-removal check | `ApprovalGate.deliver_reaction` | killed |
| Accept any target reference | `deliver_reaction` | killed |
| Skip escaping styling characters | `signal._escape_styled` | killed |
| Skip the `⧵` backslash stand-in | `signal._escape_styled` | killed |
| Skip the undeclared-key check | `ApprovalGate._authorize` path | killed |
| Hold the pending slot through the edit | `_prompt_and_wait` `finally` | killed |
| Edit after execution (fire-and-forget) | `_record_outcome` call | killed |
| Drop the first-answer-wins guard | `ApprovalGate._answer` | killed |

8 of 8 killed. Full suite afterwards: 4652 passed.
