## Summary for the owner

You answer Henk's approval prompts by reacting to them, and each prompt turns into a
one-line record of what happened. Typing `yes` or `no` keeps working. The prompt also
reads like a normal message: a bold header, a plain label for the action, and the
values in monospace instead of between `<<<` and `>>>`.

**What you will see.** The prompt, verbatim (the first line bold, each value monospace):

```
Approval needed
Add a calendar event (calendar_create_event)
title: Homelab: patch round
start: Sat 2026-11-07 14:00
end: Sat 2026-11-07 16:00
React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it.
```

After you answer, the same message changes into one of these (the first word bold,
each value monospace, Signal shows a small "edited" label):

```
Approved: Add a calendar event, Homelab: patch round, Sat 2026-11-07 14:00, Sat 2026-11-07 16:00
Denied: Add a calendar event, Homelab: patch round, Sat 2026-11-07 14:00, Sat 2026-11-07 16:00
Cancelled: Add a calendar event, Homelab: patch round, Sat 2026-11-07 14:00, Sat 2026-11-07 16:00
Timed out: Add a calendar event, Homelab: patch round, Sat 2026-11-07 14:00, Sat 2026-11-07 16:00
```

Any skin tone of 👍 or 👎 counts. A reaction counts only on the prompt that is waiting
right now; reacting to an old prompt, or to any other message, does nothing.

**Decisions you already made (2026-10-30):** reactions approve and deny; the prompt is
edited, not deleted; other reactions are ignored and leave the prompt waiting; taking a
reaction back after the action ran does nothing; styled text is used in approval prompts
only; the `<<< >>>` markers go.

**Decisions still yours** (each with my pick):

1. **The labels**, shown verbatim in every prompt. Pick, one per action that can ask:
   | Action | Label |
   |---|---|
   | `calendar_create_event` | Add a calendar event |
   | `store_memory` | Remember a fact |
   | `capture` | Add to your inbox |
   | `remind` | Set a reminder |
   | `cancel_reminder` | Cancel a reminder |

   Only the calendar asks today; the other four ask only when the standing tier is
   switched off by the kill-switch. Confidence: high that they need labels, low that
   these words are the ones you want.
2. **Values in the outcome line are monospace, not in quotes.** Earlier I showed
   `"Homelab patch round"` in quotes. Quotes come from the same model text they
   surround, so a title containing `"` could blur its own edge; monospace cannot be
   faked that way. Pick: monospace. Confidence: moderate.

3. **Keep "Any other message cancels it." in the footer.** Today's prompt ends with
   that sentence, and it is still true: typing anything other than yes or no cancels.
   The footer I showed you earlier dropped it. Pick: keep it; the prompt above already
   shows it that way. Alternative: drop the sentence. Confidence: moderate.

**A correction to what I told you.** I said Henk can check that the styling showed up.
He cannot: Signal reports that it accepted the message, not how your phone drew it.
What he can do: a config switch turns styling on or off. (I also planned an automatic
switch to the old form when the bridge refuses a styled message; reading the bridge's
code showed it never refuses one for its styling, so that part is gone.) Styling starts off:
the first deploy brings reactions, edits and labels with the old `<<< >>>` values, and
styling is switched on only after a live check with deliberately awkward titles (stars,
backticks, a trailing backslash) shows them literally. A backslash, which Signal's
styling may not be able to show as itself, appears as the look-alike `⧵`.

**Two smaller things you should know.** A 👍 sent in the split second before Henk has
registered his own prompt is ignored; react again or type yes. And this change also
closes an existing gap: a crafted argument name could add a line to today's prompt;
Henk now refuses any argument name the action does not declare.

**Left out on purpose:** bold or monospace anywhere else (triage, replies, reminders);
deleting prompts; reactions as answers to anything but approval prompts; any change to
which actions ask. Reacting from your laptop or any other device you have Signal on
works the same as from your phone.

## Why

Approving today means typing `yes` into a prompt that looks machine-generated
(`<<<Homelab: patch round>>>`), and the prompt then sits in the chat forever saying
"Approval needed" with no record of the answer. A reaction is one tap, and an edited
prompt leaves an honest record on the phone. It is also tighter than a typed `yes`: a
reaction names the exact message it answers, while a `yes` answers whatever happens to
be pending.

## What Changes

- Per-instance approval prompts are sent as one styled message: a bold header, a fixed
  per-tool label with the technical name in brackets, one line per argument with the
  value in monospace, and a footer naming both ways to answer.
- Styling characters inside argument values are escaped so a value cannot restyle
  itself. A config switch selects the unstyled form with today's `<<< >>>`
  delimiters.
- The owner's 👍 (any skin tone) on the pending prompt approves; 👎 denies. Any other
  reaction, any reaction on another message, and any reaction removal are ignored and
  never cancel the pending approval. The owner-only allowlist applies to reactions.
- Typed `yes` / `approve` / `no` / `deny` keep working exactly as today, and an
  unrelated typed message still cancels.
- After every decided prompt (approved, denied, cancelled, timed out) Henk edits the
  prompt into a one-line outcome. The edit is best-effort: its failure is logged and
  never changes the decision.
- Every mutating tool declares an owner-facing label; startup refuses a mutating tool
  without one.
- The authorization receipt's `detail` names how the owner answered (`answered by
  reaction` / `answered by keyword`). No audit schema change.
- A rendered prompt that does not fit one message fails closed as `cancelled` rather
  than being split (no tool can produce one today).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `approval-gate`: the prompt's rendering (labels, visibly bounded values instead of
  mandatory delimiters), reactions as a second way to answer, the outcome edit, the
  single-message bound, and the receipt detail.
- `channel-adapter`: a prompt send that returns a reference, an edit of that prompt,
  inbound reactions normalised to channel-neutral events under the same owner-only
  allowlist, and the styled rendering with its configuration switch.

## Impact

- Code: `henk/gate/approval.py` (rendering moves to a structured prompt, reaction
  routing, outcome edit), `henk/channel/base.py` (prompt, edit, reaction types),
  `henk/channel/signal.py` (styled send, `edit_timestamp`, reaction parsing, send
  response timestamp), `henk/channel/allowlist.py` (reactions), `henk/app.py` (routing
  reactions), the five mutating tools (labels), `config.yaml` / `henk/config.py`
  (`signal.styled_prompts`, default off until the live check, then on).
- Bridge API used: `/v2/send` with `text_mode: styled` and `edit_timestamp`, its
  response `timestamp`, and inbound `dataMessage.reaction`. All present in the deployed
  signal-cli-rest-api; checked against its published API on 2026-10-30.
- No new dependency, port, mount, token or audit schema version. No stored data
  changes.
- Sequencing: lands after the calendar archive (`chore/archive-maintenance-calendar`)
  merges, because it updates a calendar scenario that quotes today's prompt.
- Deploy: one rebuild on rp5, then a live check by the owner (styling renders, both
  reactions work, an unrelated reaction is ignored).
