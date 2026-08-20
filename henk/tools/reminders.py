"""The three reminder tools: `remind`, `cancel_reminder`, `reminders_read`.

`remind` and `cancel_reminder` are mutating, **standing** tier, **owner-turn-only** —
the same containment argument as `capture` and `store_memory` (approval-gate spec): a
write into a Henk-local store whose only external effect is a message to the
configured owner, receipted every time, denied in event turns and in any turn of a
tainted session.

**Cancellation earns standing tier because it is not removal.** The row survives with
its text and due time, this tool's result echoes both, and `/reminders reinstate <id>`
puts it back — so a mis-cancel is visible in the same reply and undone with one
command. What separates that from the imaginary-time rejection, where the project
fails closed, is **determinacy of the target**: `cancel_reminder(12)` names exactly
what it affects, so the owner can name it back.

**There is deliberately no reinstate, reschedule, edit or delete tool.** Reinstating
re-arms a message the owner deliberately killed and as a tool would need a
pending-cap bypass; as a command it needs neither. Rescheduling is
`cancel_reminder` + `remind`, two calls with two echoes — and the echoes are the
safety mechanism, not a nicety.

**The echo is the safety mechanism for the whole capability.** A mis-resolved time
becomes a wrong-but-*visible* confirmation in the same reply, correctable with one
`cancel_reminder`, instead of a silent surprise a week later. Which is why a store
failure must never produce a confirmation naming a due time: an owner who believes a
reminder is set when it is not is this capability's worst failure.
"""

from __future__ import annotations

import logging

from henk.reminders.timeparse import TOOL as TOOL_PATH
from henk.reminders.timeparse import TimeResolutionError, TimeResolver
from henk.store.errors import ContentTooLongError, EmptyContentError, StoreError
from henk.store.reminders import (
    SOURCE_TOOL,
    Reminder,
    ReminderCapReachedError,
    ReminderStore,
)
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolResult,
    TurnType,
)

logger = logging.getLogger("henk.tools.reminders")

#: The owner command that undoes a cancellation. Named in the cancel result so the
#: undo is one message away rather than something the owner has to remember exists.
REINSTATE_COMMAND = "/reminders reinstate"


class _ReminderToolBase(Tool):
    """Shared wiring: the repository, the resolver, and the lifecycle receipt.

    ``receipts`` writes the `reminder` lifecycle record **after** the store
    transaction commits (audit-log spec). The gate has already written the
    `authorization` record by the time a tool body runs, so a store failure leaves
    exactly the asymmetry the spec wants visible: an authorization with no
    transition means the tool was allowed and then failed.
    """

    def __init__(
        self,
        reminders: ReminderStore,
        resolver: TimeResolver,
        *,
        receipts=None,
    ) -> None:
        self._reminders = reminders
        self._resolver = resolver
        self._receipts = receipts

    def _receipt(self, reminder: Reminder, transition: str) -> None:
        if self._receipts is None:
            return
        try:
            self._receipts.record(
                reminder_id=reminder.id,
                due_at=reminder.due_at,
                transition=transition,
                initiated_by="model",
            )
        except Exception:  # pragma: no cover - audit is never blocking
            logger.error("could not record a reminder receipt", exc_info=True)


class RemindTool(_ReminderToolBase):
    name = "remind"
    description = (
        "Schedule a one-off reminder for the owner. `when` must be either a local "
        "date and time with an explicit time of day and NO UTC offset "
        "(2026-08-25 07:30, or 2026-08-25T07:30), or a relative offset (+90m, +2h, "
        "+3d) counted from now. Do not add an offset or a Z suffix and do not "
        "convert to UTC — give the owner's local reading, or an elapsed interval. A "
        "bare clock time with no date is not accepted here. The result echoes the "
        "resolved due time; read it back to the owner so a wrong time is caught "
        "straight away."
    )
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.STANDING
    turn_scope = (TurnType.OWNER,)
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "What to remind the owner about, as one short line in their "
                    "own words where possible."
                ),
            },
            "when": {
                "type": "string",
                "description": (
                    "Either a local date-time with no UTC offset "
                    "(YYYY-MM-DD HH:MM) or a relative offset (+90m, +2h, +3d)."
                ),
            },
        },
        "required": ["text", "when"],
        "additionalProperties": False,
    }

    async def _run(self, text: str = "", when: str = "", **_extra) -> ToolResult:
        # Resolve BEFORE the store write: a rejected time must store nothing, and a
        # rejection is cheaper to report than a row to undo.
        try:
            resolution = self._resolver.resolve(when, path=TOOL_PATH)
        except TimeResolutionError as exc:
            return ToolResult.failure(f"nothing was scheduled: {exc}")

        try:
            stored = self._reminders.schedule(
                text,
                due_at=resolution.due_at,
                due_tz=self._resolver.zone_key,
                # The tool's `when` argument, not any surrounding text — the
                # forensic column has to mean the same thing on every row.
                input_spec=when,
                source=SOURCE_TOOL,
            )
        except EmptyContentError:
            return ToolResult.failure(
                "nothing was scheduled: the reminder text was empty. Call this with "
                "what the owner should be reminded about."
            )
        except ContentTooLongError as exc:
            return ToolResult.failure(f"nothing was scheduled: {exc}")
        except ReminderCapReachedError as exc:
            return ToolResult.failure(f"nothing was scheduled: {exc}")
        except StoreError as exc:
            # Never a confirmation naming a due time. An owner who believes a
            # reminder is set when it is not is this capability's worst failure.
            logger.error("remind write failed: %s", exc)
            return ToolResult.failure(
                f"nothing was scheduled: the reminder could not be stored ({exc})."
            )

        self._receipt(stored, "scheduled")
        reply = (
            f"Reminder #{stored.id} set for {self._resolver.render(stored.due_at)}: "
            f"{stored.text}"
        )
        if resolution.disclosure:
            reply += f" — note that {resolution.disclosure}."
        return ToolResult.success(reply)


class CancelReminderTool(_ReminderToolBase):
    name = "cancel_reminder"
    description = (
        "Cancel a pending reminder by its id. This is a status change, not a "
        "deletion: the reminder keeps its text and its due time, and the owner can "
        "put it back with `/reminders reinstate <id>`. The result echoes what was "
        "cancelled; read it back so a wrong cancellation is caught straight away. "
        "Use reminders_read first if you do not know the id."
    )
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.STANDING
    turn_scope = (TurnType.OWNER,)
    parameters = {
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "integer",
                "description": "The id of the pending reminder to cancel.",
            }
        },
        "required": ["reminder_id"],
        "additionalProperties": False,
    }

    async def _run(self, reminder_id: int | None = None, **_extra) -> ToolResult:
        try:
            target = int(reminder_id)
        except (TypeError, ValueError):
            return ToolResult.failure(
                "nothing was cancelled: cancel_reminder needs the reminder's "
                "numeric id. Use reminders_read to find it."
            )
        try:
            cancelled = self._reminders.cancel(target)
        except StoreError as exc:
            logger.error("cancel_reminder write failed: %s", exc)
            return ToolResult.failure(
                f"nothing was cancelled: the store could not be written ({exc})."
            )
        if cancelled is None:
            return ToolResult.failure(
                f"nothing was cancelled: no pending reminder has id {target}."
            )
        self._receipt(cancelled, "cancelled")
        return ToolResult.success(
            f"Cancelled reminder #{cancelled.id}, which was set for "
            f"{self._resolver.render(cancelled.due_at)}: {cancelled.text}. "
            f"The owner can put it back with {REINSTATE_COMMAND} {cancelled.id}."
        )


class RemindersReadTool(_ReminderToolBase):
    name = "reminders_read"
    description = (
        "List the owner's pending reminders, soonest-due first, with their ids and "
        "resolved due times. Read-only: use it to answer questions about what is "
        "scheduled, and to find an id before cancelling."
    )
    tool_class = ToolClass.READ_ONLY
    #: No `limit` parameter on purpose: the bound is configured, one number shared
    #: with `/reminders`, so the owner and the model see the same slice of the
    #: schedule and the model cannot widen it.
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def _run(self, **_extra) -> ToolResult:
        try:
            page = self._reminders.list_pending()
        except StoreError as exc:
            # An unreadable schedule is not an empty one and must never be reported
            # as one: the model would tell the owner they have nothing scheduled.
            logger.error("reminders_read failed: %s", exc)
            return ToolResult.failure(
                f"the reminders could not be read ({exc}); this is a failure, not "
                "an empty schedule."
            )
        if not page.items:
            return ToolResult.success("There are no pending reminders.")
        lines = [f"{len(page.items)} pending reminder(s), soonest first:"]
        lines.extend(
            f"- #{item.id} at {self._resolver.render(item.due_at)}: {item.text}"
            for item in page.items
        )
        if page.remainder:
            lines.append(
                f"...and {page.remainder} later reminder(s) not shown."
            )
        return ToolResult.success("\n".join(lines))


#: The names this module registers. Named here so the registry, the system prompt's
#: enumeration and the "no reinstate/reschedule/edit/delete tool" test all read one
#: list instead of three.
REMINDER_TOOL_NAMES = ("remind", "cancel_reminder", "reminders_read")
