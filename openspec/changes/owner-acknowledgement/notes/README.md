# Owner Acknowledgement — status

**Not yet written as a full change.** `proposal.md` carries the intent, the dependency on
`channel-integrity`, the moved `agent-core` delta, and thirteen carried-over findings that were
verified against source during `channel-integrity`'s review. Those findings are the specification
work already done — read them before writing `design.md`, `specs/channel-adapter/spec.md` and
`tasks.md`, or they will be rediscovered one at a time.

Still to author:
- `design.md` — the decisions (dispatcher-side receipt after the allowlist; the opaque channel
  reference and its argued encapsulation exception; the refresh cadence; best-effort-and-bounded).
- `specs/channel-adapter/spec.md` — MODIFY the narrowed `Swappable channel-adapter contract` to
  re-add `acknowledge` and the channel reference; MODIFY `Owner-only allowlist` and the `##
  Purpose` per finding 3; ADD `Owner-only acknowledgement of inbound messages`; MODIFY `Signal
  transport` for the endpoints and the channel reference on the inbound scenario.
- `tasks.md` — config and transport first, then the tests; the stranger-gets-nothing test re-run
  with acknowledgement **enabled** is the one that matters.

`specs/agent-core/spec.md` is already written and was moved here intact from `channel-integrity`.
