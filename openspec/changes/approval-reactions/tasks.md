## 0. Sequencing

- [x] 0.1 Before implementing, rebase this branch onto `main` once `chore/archive-maintenance-calendar` has merged, and confirm the MODIFIED block *Calendar tools carry a declared tier and scope* still matches the archived base text word for word apart from this change's edits.

## 1. Bridge facts (read-only, before any code)

- [x] 1.1 Pin the deployed signal-cli-rest-api version on rp5 (`remote-read rp5 docker inspect` on the bridge container's image) and read that version's `/v2/send` handler: the response shape (object or array, `timestamp` type), `edit_timestamp` handling, and how `text_mode: styled` parses an escaped formatting character (backslash count, and whether a backslash itself can be escaped), and how style offsets are computed for characters outside the basic plane. Record findings in `notes/bridge-facts.md`.
- [x] 1.2 From the same version's receive model, record which of `targetAuthor`, `targetAuthorNumber`, `targetAuthorUuid` is filled for a reaction to a message sent by a number-registered account, and whether a reaction envelope carries `message: null`. If `targetAuthorNumber` is not reliably filled, stop and raise D3's match with the owner before building it.

## 2. Tests first, from the spec scenarios

- [x] 2.1 Gate tests (channel double): approve and deny by reaction, each skin tone of 👍 and 👎; ignored reactions (other emoji, other target, removal, nothing pending, prompt without a reference, reaction arriving before the reference is stored) leave the approval pending and queue no turn; a second 👍 authorizes nothing; typed keywords and unrelated-message cancel unchanged; an undeclared argument key (including one with a line break) cancels with no prompt sent, a reason naming the bounded key and detail `undeclared argument`; a 👍 and a typed `no` back to back: the first routed decides outcome and detail.
- [x] 2.2 Outcome-edit tests: each of `approved`, `denied`, `cancelled`, `timeout` edits with the right verb, label and values in order; the edit happens before the tool runs (ordering asserted on a shared event log); a failed edit leaves execution and receipt unchanged; `authorized`, `rejected-busy`, `suppressed`, `out-of-scope` and a failed prompt send edit nothing; receipt `detail` is `answered by reaction` / `answered by keyword`; with a slow edit double, a typed `yes` or other text arriving during the edit is queued as a normal turn and the action runs once.
- [x] 2.3 Prompt-bound tests: a prompt over one message cancels without sending, with the too-large reason and detail `prompt too large`; registration refuses a mutating tool without a label; the five production labels match the proposal's table verbatim.
- [x] 2.4 Signal adapter tests (`FakeBridge`): exact styled prompt text and outcome text; crafted values (`**`, `` ` ``, `~`, `||`, `\`, a trailing `\`, `<<<`, newlines) escaped, replaced or stripped; the size bound measured on the form actually sent; `styled_prompts: false` gives the unstyled prompt and edit; a send-response timestamp as JSON string and as integer mints the same reference as an integer `targetSentTimestamp`; `styled_prompts: false` sends unstyled for prompts and edits; replies, proactive sends and notices never styled; prompt reference minted from the response timestamp, absent when missing; reaction conversion with matching and non-matching target author, removal and group flags; edit request carries `edit_timestamp` and goes to the owner only.
- [x] 2.5 Real bridge client test with a mocked HTTP layer: `send` returns the response's string `timestamp` as an int and sends `text_mode` only when styled; the edit request carries `edit_timestamp` as an integer; an error response is still a retried `SignalBridgeError` for styled sends.
- [x] 2.6 Dispatcher and contract tests: non-owner and group reactions dropped before the gate; reactions never reach `core.submit` and get no receipt; the encapsulation scan covers styling markup and reaction/edit fields; parameter-shape tests for `send_prompt` and `edit_prompt`; a distinctive prompt reference appears in no audit record, store file or turn.
- [x] 2.7 Mutation check: remove the removal check, accept any target reference, skip escaping, skip the key check, hold the pending slot through the edit, and edit after execution instead of before; each mutation must fail at least one test. Record the result in `notes/mutation-check.md`.

## 3. Channel contract

- [x] 3.1 Add `ApprovalPrompt`, `PromptOutcome`, `PromptSent`, `InboundReaction` and the `send_prompt` / `edit_prompt` operations to `henk/channel/base.py`; `messages()` yields messages and reactions.
- [x] 3.2 Extend `AllowlistFilter.allows` to reactions (same group drop and exact sender match, same log line shape).

## 4. Signal adapter

- [x] 4.1 `SignalBridge.send` returns the response timestamp; add the edit request (`edit_timestamp`) and the `text_mode` parameter; the bridge stays the only place that builds a request.
- [x] 4.2 Styled and unstyled renderers, the escaper (one constant from task 1.1), the `⧵` stand-in for every backslash, and delimiter stripping.
- [x] 4.3 `send_prompt` (never split, send lock, reply retry budget, size refusal on the form sent, configured mode) and `edit_prompt` (one attempt, send lock, same mode as the prompt, never raises).
- [x] 4.4 Reaction envelopes in `_convert` → `InboundReaction`, target reference per task 1.2.

## 5. Gate and tools

- [x] 5.1 `Tool.label`, registration check, labels on `calendar_create_event`, `store_memory`, `capture`, `remind`, `cancel_reminder`.
- [x] 5.2 Gate refuses undeclared argument keys, builds `ApprovalPrompt` (collapse, truncate) instead of `format_prompt`; pending approval stores the prompt reference and how it was answered, set atomically with the result.
- [x] 5.3 `deliver_reaction` with `Classification.IGNORED`; release the pending slot, then the outcome edit; receipt detail.

## 6. Wiring and config

- [x] 6.1 `Dispatcher.on_inbound` routes reactions to the gate only; no receipt for reactions.
- [x] 6.2 `signal.styled_prompts` (bool, default `false` until task 7.2) in `henk/config.py` and `config.yaml`, refused when not a bool.
- [x] 6.3 Full suite green; the private local checks over added lines; README section on approvals updated with the new prompt.

## 7. Deploy and live checks (owner-run)

- [ ] 7.1 Deploy on rp5 with `signal.styled_prompts: false`. Over Signal: 👍 approves a calendar event and the prompt edits to `Approved`; 👎 denies; ❤️ leaves it pending and a later `yes` approves; a reaction on an older prompt does nothing; a timeout edits to `Timed out`. Note whether an edit raises a notification on the phone (Signal and Molly), since an edit that buzzes counts against the attention contract.
- [ ] 7.2 Set `styled_prompts: true` in rp5's `config.yaml` (it is locally modified there; add the key explicitly), redeploy, then run the crafted-value check with styling on: ask for an event whose title contains `**x**`, a backtick, `||s||`, a trailing `\`, and an emoji before the value, confirm the prompt shows each literally inside its monospace span, and deny it. Check the styled prompt and outcome by eye in Signal and in Molly.
- [ ] 7.3 Read the edit latency from the logs for the approvals in 7.1–7.2 and note it here.
- [ ] 7.4 Flip the code default of `signal.styled_prompts` to `true` (tested), then run `/docs-update` for the Henk page, then validate (`openspec validate approval-reactions --strict`) and archive.
