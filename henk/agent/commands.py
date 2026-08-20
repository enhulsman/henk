"""Owner commands, executed app-side with no agent turn (design D8).

`/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all`,
`/inbox done <id>`, `/remind <when> <text>`, `/reminders`,
`/reminders cancel <id>` and `/reminders reinstate <id>` are deterministic,
instant, and cost zero tokens. They are also
**owner-initiated by construction**: the text never passes through the model, so
the gate — which governs model-initiated tool calls — is not involved, and session
taint does not apply. That is the whole point of `/capture` existing as a command:
the change's headline verb must not cost a model turn on its fastest path, and it
has to keep working in the middle of an incident interrogation.

Mutating commands write their own receipt at execution time (design D5). Read-only
commands, and mutating ones that changed nothing, write none: receipts record
mutations, and none occurred.

Failures are loud but honest. A write that failed never reads as success, and an
unreadable store is never reported as an empty one.

The reminder commands add two rules of their own:

- **`/remind` accepts explicit time forms only**, matched longest-first so
  `/remind 2026-08-25 07:30 buy bread` splits without a heuristic. A command must be
  deterministic, and a command is not the place to guess — `/remind sometime next
  week …` is a sentence for the agent, not for the dispatcher.
- **Reinstating is command-only, and refuses a past due time.** As a tool it would
  need a pending-cap bypass and a counter reset; as a command it needs neither. The
  past-due refusal is what keeps the entire late/missed question inside
  `reminder-delivery` instead of leaking into a core command.
"""

from __future__ import annotations

import logging
import re

from henk.reminders.timeparse import COMMAND as COMMAND_PATH
from henk.reminders.timeparse import TimeResolutionError
from henk.store import (
    DEFAULT_PAGE_SIZE,
    ContentTooLongError,
    EmptyContentError,
    StoreError,
    format_created_at,
)
from henk.store.reminders import (
    CANCELLED,
    PENDING,
    SOURCE_COMMAND,
    ReminderCapReachedError,
)

logger = logging.getLogger("henk.agent.commands")

#: How many removed memories `/forget` echoes in full before switching to a count.
#: Bounded so a mistaken bulk forget is still recoverable by re-adding, without a
#: hundred-line reply.
FORGET_ECHO_LIMIT = 10

#: Ordered labels for `/memories`, matching the recall block's grouping.
_TYPE_LABELS = {
    "pinned": "Facts you told me (pinned)",
    "agent": "Facts I noted myself (agent)",
}
_TYPE_ORDER = ("pinned", "agent")

_UNAVAILABLE = (
    "That command needs the durable store, which isn't configured in this "
    "deployment. Nothing was changed."
)

#: Covers BOTH reasons honestly — the capability is switched off, or no store is
#: wired — because from the owner's side they are the same fact. Nothing is
#: scheduled, nothing is changed, and every stored reminder is left alone: they
#: become operable again the moment the capability is re-enabled.
_REMINDERS_UNAVAILABLE = (
    "Reminders aren't configured in this deployment, so nothing is scheduled and "
    "nothing was changed. Any reminders already stored are untouched."
)

#: The accepted `<when>` forms, in one place so every refusal names the same set.
_ACCEPTED_WHEN = (
    "Accepted times are +90m / +2h / +3d, a clock time like 07:30 (the next time "
    "it comes round), or a dated time like 2026-08-25 07:30."
)

#: The two-token dated form, tried FIRST so a dated time is never mistaken for a
#: clock reading followed by text.
_DATE_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_TOKEN = re.compile(r"^\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$")


class OwnerCommands:
    """Recognizes and executes the owner command set. Returns the reply text.

    ``handle`` returns ``None`` for anything it does not recognize — including an
    unknown slash word — so that text goes on to be a normal agent turn exactly as
    before.
    """

    def __init__(
        self,
        *,
        memories=None,
        inbox=None,
        reminders=None,
        resolver=None,
        receipts=None,
        reminder_receipts=None,
        inbox_page_size: int = DEFAULT_PAGE_SIZE,
        forget_echo_limit: int = FORGET_ECHO_LIMIT,
    ) -> None:
        self.memories = memories
        self.inbox = inbox
        # Both None when the capability is disabled: the runtime passes them only
        # when `reminders.enabled`, so the four commands reply honestly rather than
        # half-working. The same repository and the SAME resolver instance the tools
        # got, so a due time can never read differently between the two paths.
        self.reminders = reminders
        self._resolver = resolver
        self._receipts = receipts
        self._reminder_receipts = reminder_receipts
        self._page_size = inbox_page_size
        self._echo_limit = forget_echo_limit

    def handle(self, text: str) -> str | None:
        raw = (text or "").strip()
        if not raw.startswith("/"):
            return None
        verb, _, rest = raw.partition(" ")
        rest = rest.strip()
        handler = {
            "/remember": self._remember,
            "/forget": self._forget,
            "/memories": self._list_memories,
            "/capture": self._capture,
            "/inbox": self._inbox,
            "/remind": self._remind,
            "/reminders": self._reminders,
        }.get(verb.lower())
        if handler is None:
            return None
        return handler(rest)

    # --- memory -----------------------------------------------------------

    def _remember(self, rest: str) -> str:
        if self.memories is None:
            return _UNAVAILABLE
        try:
            write = self.memories.add(rest, "pinned")
        except EmptyContentError:
            return "That was empty — /remember needs the fact to remember after it."
        except ContentTooLongError as exc:
            return f"Nothing stored: {exc}"
        except StoreError as exc:
            logger.error("/remember failed: %s", exc)
            return f"Couldn't store that fact — the store could not be written ({exc})."
        reply = f"Got it, I'll remember: {write.memory.content}"
        if write.evicted:
            dropped = "; ".join(m.content for m in write.evicted)
            reply += (
                f"\nThat filled the pinned-memory cap, so the oldest fact was "
                f"dropped: {dropped}"
            )
        self._receipt("/remember", f"stored a pinned memory (id {write.memory.id})")
        return reply

    def _forget(self, rest: str) -> str:
        if self.memories is None:
            return _UNAVAILABLE
        if not rest:
            return (
                "That was empty — /forget needs some text to match, and I won't "
                "match everything. Nothing was removed."
            )
        try:
            removed = self.memories.delete_containing(rest)
        except StoreError as exc:
            logger.error("/forget failed: %s", exc)
            return f"Couldn't remove anything — the store could not be written ({exc})."
        if not removed:
            return f'Nothing matched "{rest}", so nothing was removed.'

        echoed = removed[: self._echo_limit]
        lines = [f"Forgot {len(removed)} memor{'y' if len(removed) == 1 else 'ies'}:"]
        lines.extend(f"- {m.content}" for m in echoed)
        if len(removed) > len(echoed):
            lines.append(
                f"...and {len(removed) - len(echoed)} more removed (not listed)."
            )
        lines.append("Re-add anything that shouldn't have gone with /remember.")
        self._receipt("/forget", f"removed {len(removed)} memories matching {rest!r}")
        return "\n".join(lines)

    def _list_memories(self, _rest: str) -> str:
        if self.memories is None:
            return _UNAVAILABLE
        try:
            stored = self.memories.list_all()
        except StoreError as exc:
            logger.error("/memories failed: %s", exc)
            return (
                f"Couldn't read the store ({exc}). Your memories are probably fine — "
                "I just can't list them right now."
            )
        if not stored:
            return "No memories stored yet. Add one with /remember <fact>."

        by_type: dict[str, list] = {}
        for memory in stored:
            by_type.setdefault(memory.memory_type, []).append(memory)
        lines = [f"{len(stored)} memor{'y' if len(stored) == 1 else 'ies'} stored:"]
        known = [t for t in _TYPE_ORDER if t in by_type]
        for memory_type in known + sorted(t for t in by_type if t not in _TYPE_ORDER):
            lines.append("")
            lines.append(_TYPE_LABELS.get(memory_type, memory_type))
            lines.extend(f"- [{m.id}] {m.content}" for m in by_type[memory_type])
        lines.append("")
        lines.append("Remove one with /forget <text it contains>.")
        return "\n".join(lines)

    # --- capture inbox ----------------------------------------------------

    def _capture(self, rest: str) -> str:
        if self.inbox is None:
            return _UNAVAILABLE
        try:
            item = self.inbox.append(rest, source="owner-command")
        except EmptyContentError:
            return "That was empty — /capture needs the thought to capture after it."
        except StoreError as exc:
            logger.error("/capture failed: %s", exc)
            return f"Couldn't capture that — the store could not be written ({exc})."
        self._receipt("/capture", f"captured inbox item {item.id}")
        return f"Captured as #{item.id}: {item.text}"

    def _inbox(self, rest: str) -> str:
        if self.inbox is None:
            return _UNAVAILABLE
        argument = rest.strip()
        if not argument:
            return self._inbox_list(limit=self._page_size)
        verb, _, tail = argument.partition(" ")
        verb = verb.lower()
        if verb == "all":
            return self._inbox_list(limit=None)
        if verb == "done":
            return self._inbox_done(tail.strip())
        return (
            f'I don\'t know "/inbox {argument}". Use /inbox, /inbox all, or '
            "/inbox done <id>."
        )

    def _inbox_list(self, *, limit: int | None) -> str:
        try:
            page = self.inbox.list_open(limit=limit)
        except StoreError as exc:
            logger.error("/inbox failed: %s", exc)
            return (
                f"Couldn't read the inbox ({exc}). Nothing is lost — I just can't "
                "list it right now."
            )
        if not page.items:
            return "The inbox is empty. Capture something with /capture <thought>."
        lines = [f"{len(page.items)} open item{'' if len(page.items) == 1 else 's'}"]
        lines[0] += " (oldest first):"
        lines.extend(
            f"- [{item.id}] {item.text} ({format_created_at(item.created_at)})"
            for item in page.items
        )
        if page.newer_remainder:
            lines.append(
                f"...and {page.newer_remainder} newer. See everything with "
                "/inbox all."
            )
        lines.append("Mark one done with /inbox done <id>.")
        return "\n".join(lines)

    def _inbox_done(self, raw_id: str) -> str:
        if not raw_id.isdigit():
            return "/inbox done needs an item id, e.g. /inbox done 12."
        item_id = int(raw_id)
        try:
            item = self.inbox.mark_done(item_id)
        except StoreError as exc:
            logger.error("/inbox done failed: %s", exc)
            return f"Couldn't update the inbox — the store could not be written ({exc})."
        if item is None:
            return f"No open inbox item has id {item_id}, so nothing changed."
        self._receipt("/inbox done", f"marked inbox item {item_id} done")
        return f"Done: #{item.id} {item.text}"

    # --- reminders --------------------------------------------------------

    def _remind(self, rest: str) -> str:
        if self.reminders is None or self._resolver is None:
            return _REMINDERS_UNAVAILABLE
        when, text = self._split_remind(rest)
        if when is None:
            return f"I couldn't read a time in that. {_ACCEPTED_WHEN}"
        # Resolve BEFORE checking the text: resolution has no side effects, and this
        # is what makes "unrecognized form" and "recognized form, text missing" two
        # distinct, honest replies rather than one vague one.
        try:
            resolution = self._resolver.resolve(when, path=COMMAND_PATH)
        except TimeResolutionError as exc:
            return f"Nothing scheduled: {exc}"
        if not text.strip():
            return (
                "That time reads fine, but the reminder text is required — "
                f"try /remind {when} <what to remind you about>."
            )
        try:
            stored = self.reminders.schedule(
                text,
                due_at=resolution.due_at,
                due_tz=self._resolver.zone_key,
                # The `<when>` TOKEN, not the whole command line: a forensic column
                # whose meaning varies per row cannot be read at all.
                input_spec=when,
                source=SOURCE_COMMAND,
            )
        except EmptyContentError:
            return "That was empty — /remind needs the reminder text after the time."
        except (ContentTooLongError, ReminderCapReachedError) as exc:
            return f"Nothing scheduled: {exc}"
        except StoreError as exc:
            logger.error("/remind failed: %s", exc)
            return (
                f"Couldn't schedule that — the store could not be written ({exc}). "
                "Nothing was scheduled."
            )
        self._receipt("/remind", f"scheduled reminder {stored.id}")
        self._reminder_receipt(stored, "scheduled")
        reply = (
            f"Reminder #{stored.id} set for "
            f"{self._resolver.render(stored.due_at)}: {stored.text}"
        )
        if resolution.disclosure:
            reply += f"\nNote: {resolution.disclosure}."
        return reply

    @staticmethod
    def _split_remind(rest: str) -> tuple[str | None, str]:
        """Split `<when> <text>`, trying the two-token dated form first.

        Longest-first is what makes `/remind 2026-08-25 07:30 buy bread` work without
        a heuristic: had the one-token form won, `<when>` would be a date with no
        time of day (refused) and the text would begin with the clock reading.
        """
        parts = rest.split()
        if not parts:
            return None, ""
        if (
            len(parts) >= 2
            and _DATE_TOKEN.match(parts[0])
            and _TIME_TOKEN.match(parts[1])
        ):
            return f"{parts[0]} {parts[1]}", " ".join(parts[2:])
        return parts[0], " ".join(parts[1:])

    def _reminders(self, rest: str) -> str:
        if self.reminders is None or self._resolver is None:
            return _REMINDERS_UNAVAILABLE
        argument = rest.strip()
        if not argument:
            return self._reminders_list()
        verb, _, tail = argument.partition(" ")
        verb = verb.lower()
        raw_id = tail.strip()
        if verb in ("cancel", "reinstate") and raw_id.isdigit():
            if verb == "cancel":
                return self._reminders_cancel(int(raw_id))
            return self._reminders_reinstate(int(raw_id))
        return (
            f'I don\'t know "/reminders {argument}". Use /reminders, '
            "/reminders cancel <id>, or /reminders reinstate <id>."
        )

    def _reminders_list(self) -> str:
        try:
            page = self.reminders.list_pending()
        except StoreError as exc:
            logger.error("/reminders failed: %s", exc)
            # An unreadable schedule is NOT an empty one, and must never read as
            # one: the owner would conclude nothing is coming.
            return (
                f"Couldn't read your reminders ({exc}). They aren't lost — I just "
                "can't list them right now, so don't take this as an empty schedule."
            )
        if not page.items:
            return "Nothing scheduled. Add one with /remind <when> <what>."
        count = len(page.items)
        lines = [f"{count} pending reminder{'' if count == 1 else 's'} (soonest first):"]
        lines.extend(
            f"- [{item.id}] {self._resolver.render(item.due_at)}: {item.text}"
            for item in page.items
        )
        if page.remainder:
            lines.append(f"...and {page.remainder} further out, not shown.")
        lines.append("Cancel one with /reminders cancel <id>.")
        return "\n".join(lines)

    def _reminders_cancel(self, reminder_id: int) -> str:
        try:
            cancelled = self.reminders.cancel(reminder_id)
        except StoreError as exc:
            logger.error("/reminders cancel failed: %s", exc)
            return (
                f"Couldn't cancel that — the store could not be written ({exc}). "
                "Nothing changed."
            )
        if cancelled is None:
            return f"No pending reminder has id {reminder_id}, so nothing changed."
        self._receipt("/reminders cancel", f"cancelled reminder {reminder_id}")
        self._reminder_receipt(cancelled, "cancelled")
        return (
            f"Cancelled #{cancelled.id}, which was set for "
            f"{self._resolver.render(cancelled.due_at)}: {cancelled.text}\n"
            f"Put it back with /reminders reinstate {cancelled.id}."
        )

    def _reminders_reinstate(self, reminder_id: int) -> str:
        try:
            existing = self.reminders.get(reminder_id)
        except StoreError as exc:
            logger.error("/reminders reinstate failed: %s", exc)
            return f"Couldn't read that reminder ({exc}). Nothing changed."
        if existing is None or existing.status != CANCELLED:
            return (
                f"No cancelled reminder has id {reminder_id}, so nothing changed. "
                "See /reminders for what is pending."
            )
        # Refused rather than reinstated into the past. This is the line that keeps
        # the whole late/missed question inside reminder-delivery: a core command
        # never has to decide what a reminder that is already due should do.
        if existing.due_at <= self._resolver.current_instant():
            return (
                f"#{existing.id} was due at "
                f"{self._resolver.render(existing.due_at)}, which has already "
                "passed, so nothing changed. Set a new time with "
                f"/remind <when> {existing.text}"
            )
        try:
            back = self.reminders.reinstate(reminder_id)
        except ReminderCapReachedError as exc:
            return f"Nothing changed: {exc}"
        except StoreError as exc:
            logger.error("/reminders reinstate failed: %s", exc)
            return (
                f"Couldn't reinstate that — the store could not be written "
                f"({exc}). Nothing changed."
            )
        if back is None:  # pragma: no cover - the status was checked above
            return f"No cancelled reminder has id {reminder_id}, so nothing changed."
        self._receipt("/reminders reinstate", f"reinstated reminder {reminder_id}")
        self._reminder_receipt(back, "reinstated")
        assert back.status == PENDING
        return (
            f"Reinstated #{back.id} for {self._resolver.render(back.due_at)}: "
            f"{back.text}"
        )

    def _reminder_receipt(self, reminder, transition: str) -> None:
        """Append the lifecycle record — AFTER the store transaction committed.

        A crash between the two costs a receipt for a real transition, which is the
        preferable direction: a log that claims state the store does not have is
        worse than a log with a gap.
        """
        if self._reminder_receipts is None:
            return
        try:
            self._reminder_receipts.record(
                reminder_id=reminder.id,
                due_at=reminder.due_at,
                transition=transition,
                initiated_by="owner-command",
            )
        except Exception:  # pragma: no cover - audit is never blocking
            logger.error("could not record a reminder receipt", exc_info=True)

    # --- receipts ---------------------------------------------------------

    def _receipt(self, command: str, detail: str) -> None:
        """Write a mutation receipt for a command that actually changed state."""
        if self._receipts is None:
            return
        try:
            self._receipts.record(
                tool=command,
                tier=None,  # a tier is a TOOL property; a command is not a tool
                outcome="authorized",
                turn_type="command",  # commands run outside any turn or session
                initiated_by="owner-command",
                detail=detail,
            )
        except Exception:  # pragma: no cover - audit is never blocking
            logger.error("could not record a command receipt", exc_info=True)
