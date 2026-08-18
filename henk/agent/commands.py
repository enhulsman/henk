"""Owner commands, executed app-side with no agent turn (design D8).

`/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all` and
`/inbox done <id>` are deterministic, instant, and cost zero tokens. They are also
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
"""

from __future__ import annotations

import logging

from henk.store import (
    DEFAULT_PAGE_SIZE,
    ContentTooLongError,
    EmptyContentError,
    StoreError,
    format_created_at,
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
        receipts=None,
        inbox_page_size: int = DEFAULT_PAGE_SIZE,
        forget_echo_limit: int = FORGET_ECHO_LIMIT,
    ) -> None:
        self.memories = memories
        self.inbox = inbox
        self._receipts = receipts
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
