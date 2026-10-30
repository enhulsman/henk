# Bridge facts (tasks 1.1, 1.2)

Read 2026-10-30 from the deployed bridge and its source at the matching tag.

**Version.** rp5 runs `bbernhard/signal-cli-rest-api:latest`, image
`sha256:23bf2c8d…`, `BUILD_VERSION=0.101`, `MODE=json-rpc`. Source read at tag `0.101`:
`src/api/api.go`, `src/client/client.go`, `src/datastructs/datastructs.go`,
`src/utils/textstyleparser.go`.

**`/v2/send` response.** `201` with an object `{"timestamp": "<decimal string>"}`
(`ds.SendMessageResponse`, `Timestamp string`). Henk parses it to an int.

**Errors.** Every signal-cli failure from `SendV2` is answered `400 {"error": ...}`, except
a rate limit (`429`). Request-shape errors are also `400`. A `400` therefore does not
identify a styling problem, which is why the automatic unstyled fallback was dropped
(design D5).

**Edits.** `edit_timestamp` (int64) is passed to signal-cli as `edit-timestamp` /
`--edit-timestamp`. An edit is an ordinary `/v2/send` with that field.

**Styled text.** `text_mode: "styled"` runs `TextstyleParser`:
- markers `*` italic, `**` bold, `***` bold+italic, `` ` `` monospace, `~` strike,
  `||` spoiler; tokens nest on a stack, so markers inside a monospace span still parse;
- an unclosed marker never errors: it is dropped from the text;
- escape: a single `\` character (decoded string) immediately before `*`, `` ` ``, `|`
  or `~` is swallowed and the next marker is literal; any other backslash is literal;
- a backslash cannot be escaped: `\\` before a backtick outputs one `\` and still
  escapes the backtick. Hence the `⧵` stand-in for every content backslash;
- style ranges are computed in UTF-16 units over the output text, so emoji before a
  span are handled by the bridge.

**Reactions (json-rpc mode).** The bridge forwards signal-cli's envelope; a reaction is
`dataMessage.reaction` with `emoji`, `targetAuthor`, `targetAuthorNumber`,
`targetAuthorUuid`, `targetSentTimestamp` (int), `isRemove`, and `dataMessage.message`
null. signal-cli fills `targetAuthorNumber` when the author's number is known, which it
always is for the account's own messages. Confidence: moderate from source; the live
check in task 7.1 confirms it, and the failure mode is fail-closed (reactions ignored,
keywords still work).
