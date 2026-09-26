# Owner Acknowledgement — status

**Written as a full change on 2026-09-26. Scrutiny round 1 returned NEEDS REWORK; the review
confirmed the architecture, and all its findings are folded in (document edits only).**
`openspec validate owner-acknowledgement --strict` passes.

- `proposal.md`: intent, the (now satisfied) dependency on `channel-integrity`, and the
  thirteen carried-over findings. The ones later decisions changed are annotated inline:
  - finding 2's default `False` is now `True` (owner decision), with the mechanism kept (D8);
  - finding 3's Purpose half is dropped (D9);
  - finding 5's cadence is now 7 s (D4);
  - finding 12's "linked device" is corrected to Henk's dedicated number (design Context).
- `design.md`: decisions D1-D10, Risks, Migration, Open Questions. Round-1 changes:
  - D4 is now one task owning every typing request, woken at once on close, with the close as
    one bounded await, `asyncio.wait({task})` so an outer cancel is never swallowed, and the
    exhausted-bound path stated;
  - the refresh cadence is 7 s;
  - D5 states the approval residual honestly;
  - D2's rules are narrowed to "never in a turn, never audited or persisted";
  - D8 adds the config edges and the misspelt-rollback-key check;
  - D9 now leaves the Purpose as it is: no hand edit of `openspec/specs/`.
- `specs/channel-adapter/spec.md`: MODIFIES `Owner-only allowlist`, `Signal transport via
  signal-cli-rest-api` and `Swappable channel-adapter contract` (full current text, every
  baseline scenario kept; *Bridge unreachable* is scoped to receive and send), and ADDS
  `Owner-only acknowledgement of inbound messages`.
- `specs/agent-core/spec.md`: moved from `channel-integrity`, adjusted for the bracket
  placement, cancellation (including during entry), approval suspension and the hung-stop
  bound.
- `tasks.md`: groups 1-12. It specifies the `App.run` test harness (`FakeBridge(hold_open=True)`,
  cancel, then check files after `core.aclose()`) and mutants M1-M20, and it ends in owner-run
  rp5 checks plus a rollback drill.

**What remains:**

1. A second `/scrutinize` round, to APPROVED.
2. One owner decision: whether to run the optional live approval-suspension check (task 12.8),
   which needs a temporary `gate.demote_standing` edit on rp5.
3. Then `/opsx:apply` in a fresh session.
