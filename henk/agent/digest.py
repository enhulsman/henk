"""The related-handoff digest an event turn carries (triage-quality design D9).

Handoffs used to live only in ntfy for 72 h, so a triage could not see what an
earlier triage of a related incident had found. The archive (D8,
:mod:`henk.store.handoffs`) keeps them; this module decides which ones a new event
turn is shown, renders them inside the untrusted-data block, and reports which ids
it showed so the audit record can name them.

What is load-bearing:

- **Retained handoffs are model output from tainted sessions.** They are rendered
  only here, only inside the untrusted block (the composer places the digest there,
  :mod:`henk.agent.triage`), under :data:`PRIOR_HANDOFFS_HEADER`, and every string
  taken from a retained handoff goes through :func:`neutralise_markers`. No owner
  turn reads the archive: the core calls :func:`read_digest` from its event path
  only, and no tool calls the archive's reads.
- **Related**, ranked: the same identity key, then the same rule key, then an
  intersecting node. Each handoff appears once, at its best rank, newest first
  within a rank.
- **Bounds are module constants** (D17): at most 3 entries, 1,200 characters per
  excerpt, 4,000 for the recurrence reference, and 6,000 for the whole digest *as
  rendered* — header, entry headers, excerpts and every marker. The recurrence
  reference is the first entry and counts toward the total. An entry that does not
  fit is omitted whole and counted, never cut below its excerpt bound.
- **The recurrence lookup goes through the persistent archive**, by the ref the
  pipeline rebuilt from the audit log, so recurrence framing survives a restart.
- **A read failure never fails the triage.** It is logged, and the turn carries no
  digest; the recurrence note then says the content is not available.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Sequence

from henk.agent.markers import PRIOR_HANDOFFS_HEADER, neutralise_markers
from henk.events.incident_context import IncidentContext
from henk.store.handoffs import RetainedHandoff, parse_handoff_message_id
from henk.tools.query_renderers import _stamp

logger = logging.getLogger("henk.agent.digest")

#: At most this many entries, the recurrence reference included (D9).
DIGEST_MAX_ENTRIES = 3
#: An ordinary entry's excerpt is at most this many characters (D9).
DIGEST_ENTRY_MAX_CHARS = 1_200
#: The recurrence reference's excerpt is at most this many characters (D9).
DIGEST_RECURRENCE_MAX_CHARS = 4_000
#: The whole digest, as rendered, is at most this many characters (D9).
DIGEST_MAX_CHARS = 6_000

#: An entry header's identity list is cut to this many characters. Identity keys
#: are derived from payload titles, so they are neither short nor trusted; the cut
#: keeps one handoff's header from spending the digest's budget.
_IDENTITIES_MAX_CHARS = 240


class Relation(enum.Enum):
    """Why an entry was selected, in rank order, as its header states it."""

    RECURRENCE = "recurrence reference"
    SAME_IDENTITY = "same identity"
    SAME_RULE = "same rule"
    SAME_NODE = "same node"


#: Rank of each relation; lower is better. The recurrence reference is placed
#: first by construction, not by rank.
_RANK = {Relation.SAME_IDENTITY: 0, Relation.SAME_RULE: 1, Relation.SAME_NODE: 2}


class RefState(enum.Enum):
    """What a recurrence ref resolved to, and what the digest did with it."""

    #: Retained, and rendered as the digest's first entry, the recurrence reference.
    REFERENCE = "reference"
    #: Retained, and rendered as an ordinary entry.
    SHOWN = "shown"
    #: Retained, but the digest's bounds left it out.
    OMITTED = "omitted"
    #: Not in the archive (never retained, pruned, or past retention).
    NOT_RETAINED = "not-retained"
    #: The archive could not be read, so nothing is known about it.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class RecurrenceRef:
    """One distinct recurrence ref of the turn, as the note describes it.

    ``message_id`` is the bare ntfy id (the ref as given when it has no id form).
    It is not neutralised here; the note neutralises what it renders.
    """

    message_id: str
    state: RefState


@dataclass(frozen=True)
class Digest:
    """A rendered digest, or its absence, and what it showed.

    ``text`` is None when no entry was rendered: the turn then carries no digest
    header at all. ``shown_ids`` are the archive row ids actually rendered, in
    order, which is what the audit record's ``prior_handoff_ids`` lists.
    ``omitted`` counts related handoffs that were not shown.
    """

    text: str | None = None
    shown_ids: tuple[int, ...] = ()
    omitted: int = 0
    refs: tuple[RecurrenceRef, ...] = ()
    read_failed: bool = False


EMPTY_DIGEST = Digest()


# --- Relations ---------------------------------------------------------------------


def relation_of(handoff: RetainedHandoff, context: IncidentContext) -> Relation | None:
    """The best relation between ``handoff`` and the turn's incidents, or None."""
    if set(handoff.identity_keys) & set(context.identity_keys):
        return Relation.SAME_IDENTITY
    if set(handoff.rule_keys) & set(context.rule_keys):
        return Relation.SAME_RULE
    if set(handoff.nodes) & set(context.nodes):
        return Relation.SAME_NODE
    return None


def rank_related(
    handoffs: Iterable[RetainedHandoff],
    context: IncidentContext,
    *,
    exclude_ids: Collection[int] = (),
) -> list[tuple[RetainedHandoff, Relation]]:
    """Every related handoff once, at its best rank, newest first within a rank.

    The order is computed here rather than trusted from the caller: by tier, then
    publish time descending, then row id descending (the later insert of a tie).
    """
    seen: set[int] = set(exclude_ids)
    related: list[tuple[RetainedHandoff, Relation]] = []
    for handoff in handoffs:
        if handoff.id in seen:
            continue
        relation = relation_of(handoff, context)
        if relation is None:
            continue
        seen.add(handoff.id)
        related.append((handoff, relation))
    related.sort(key=lambda hr: (_RANK[hr[1]], -hr[0].published_at, -hr[0].id))
    return related


# --- Rendering ---------------------------------------------------------------------


def _age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _published(epoch: float) -> str:
    try:
        return _stamp(epoch)
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _identities(keys: Sequence[str]) -> str:
    joined = ", ".join(neutralise_markers(k) for k in keys)
    if len(joined) > _IDENTITIES_MAX_CHARS:
        joined = joined[:_IDENTITIES_MAX_CHARS] + "…"
    # Closed with "]" so nothing a key ends with can join the next line's text.
    return f"[{joined}]"


def _entry(
    number: int, handoff: RetainedHandoff, relation: Relation, bound: int, now: float
) -> str:
    """One entry: its header line, its excerpt, and a marker if it was shortened."""
    header = (
        f"[prior handoff {number}] relation={relation.value} "
        f"published={_published(handoff.published_at)} "
        f"age={_age(now - handoff.published_at)} "
        f"nodes=[{', '.join(handoff.nodes)}] "
        f"identities={_identities(handoff.identity_keys)}"
    )
    # Neutralised before it is cut, so the count is of what is rendered.
    text = neutralise_markers(handoff.document).strip()
    lines = [header, text[:bound]]
    if len(text) > bound:
        lines.append(f"[excerpt shortened: {bound} of {len(text)} characters shown]")
    return "\n".join(lines)


def _omission(count: int) -> str:
    return f"[related handoffs omitted to stay within the digest's bounds: {count}]"


def _fill(
    candidates: Sequence[tuple[RetainedHandoff, Relation, int]],
    now: float,
    max_chars: int,
    reserve: int,
) -> tuple[list[str], list[int]]:
    """Greedy fill in order: an entry that does not fit is skipped whole."""
    used = len(PRIOR_HANDOFFS_HEADER) + reserve
    parts: list[str] = [PRIOR_HANDOFFS_HEADER]
    shown: list[int] = []
    for handoff, relation, bound in candidates:
        if len(shown) >= DIGEST_MAX_ENTRIES:
            break
        entry = _entry(len(shown) + 1, handoff, relation, bound, now)
        cost = 1 + len(entry)  # the newline that joins it
        if used + cost > max_chars:
            continue
        parts.append(entry)
        used += cost
        shown.append(handoff.id)
    return parts, shown


def render_digest(
    *,
    related: Sequence[tuple[RetainedHandoff, Relation]],
    recurrence: RetainedHandoff | None = None,
    now: float,
    max_chars: int = DIGEST_MAX_CHARS,
) -> Digest:
    """Render the digest within its bounds.

    ``recurrence`` is the recurrence reference, placed first at its own excerpt
    bound; ``related`` is the ranked rest (:func:`rank_related`, which must exclude
    the reference). Entries are tried in order; one that does not fit what is left
    is omitted whole and counted, and later, smaller entries may still fit.

    The budget counts every character rendered. The omission marker is budgeted
    too: when a first fill without it leaves anything out, the fill is redone with
    room reserved for the marker, sized for the largest count it could state, so
    appending it can never push the digest over ``max_chars``. A digest that omits
    nothing reserves nothing.
    """
    candidates: list[tuple[RetainedHandoff, Relation, int]] = []
    if recurrence is not None:
        candidates.append((recurrence, Relation.RECURRENCE, DIGEST_RECURRENCE_MAX_CHARS))
    candidates += [(h, rel, DIGEST_ENTRY_MAX_CHARS) for h, rel in related]
    if not candidates:
        return EMPTY_DIGEST

    parts, shown = _fill(candidates, now, max_chars, reserve=0)
    if len(shown) < len(candidates):
        reserve = 1 + len(_omission(len(candidates)))
        parts, shown = _fill(candidates, now, max_chars, reserve=reserve)
    if not shown:
        return EMPTY_DIGEST
    omitted = len(candidates) - len(shown)
    if omitted:
        parts.append(_omission(omitted))
    return Digest(text="\n".join(parts), shown_ids=tuple(shown), omitted=omitted)


# --- Reading -------------------------------------------------------------------------


def _distinct_refs(turn: Any) -> list[tuple[str, str]]:
    """``(ref, message_id)`` for each distinct recurrence ref, in item order."""
    refs: dict[str, str] = {}
    for item in turn.items:
        if not item.recurrence or not item.prior_handoff_ref:
            continue
        ref = str(item.prior_handoff_ref)
        message_id = parse_handoff_message_id(ref) or ref
        refs.setdefault(message_id, ref)
    return [(ref, message_id) for message_id, ref in refs.items()]


def unresolved_refs(
    turn: Any, state: RefState = RefState.NOT_RETAINED
) -> tuple[RecurrenceRef, ...]:
    """Every recurrence ref of ``turn``, all in ``state``: nothing was looked up."""
    return tuple(RecurrenceRef(mid, state) for _, mid in _distinct_refs(turn))


def read_digest(archive: Any | None, turn: Any, *, now: float | None = None) -> Digest:
    """The digest for an event turn, read from ``archive``. Never raises.

    ``archive`` is the :class:`~henk.store.handoffs.HandoffStore` (None when no
    archive is wired: then nothing is retained locally, and every ref says so).
    ``now`` defaults to the archive store's clock, the clock retention runs on, so
    eligibility and the ages shown agree with pruning.

    Any failure, a store read or anything after it, is logged and yields no digest
    with ``read_failed`` set: history is context for a triage, never a
    precondition for running one.
    """
    if archive is None:
        return Digest(refs=unresolved_refs(turn))
    try:
        return _read(archive, turn, now)
    except Exception:
        # No content in the log: handoff text does not belong there.
        logger.error(
            "could not read the handoff archive for the prior-handoff digest; "
            "the triage proceeds without it",
            exc_info=True,
        )
        return Digest(refs=unresolved_refs(turn, RefState.UNREADABLE), read_failed=True)


def _read(archive: Any, turn: Any, now: float | None) -> Digest:
    at = float(archive.store.clock() if now is None else now)
    context = IncidentContext.from_turn(turn)
    # The recurrence lookup goes through the persistent archive, by the ref the
    # pipeline rebuilt from the audit log: that is what survives a restart.
    found: list[tuple[str, RetainedHandoff | None]] = [
        (mid, archive.find_by_message_ref(ref, now=at))
        for ref, mid in _distinct_refs(turn)
    ]
    eligible = archive.eligible(now=at)

    reference = next((h for _, h in found if h is not None), None)
    exclude = {reference.id} if reference is not None else set()
    # A ref that resolved but is not the reference competes as ordinary history.
    extra = [h for _, h in found if h is not None and h.id not in exclude]
    by_id = {h.id: h for h in [*extra, *eligible]}
    related = rank_related(by_id.values(), context, exclude_ids=exclude)
    digest = render_digest(related=related, recurrence=reference, now=at)

    refs: list[RecurrenceRef] = []
    for mid, handoff in found:
        if handoff is None:
            state = RefState.NOT_RETAINED
        elif handoff.id not in digest.shown_ids:
            state = RefState.OMITTED
        elif reference is not None and handoff.id == reference.id and (
            digest.shown_ids[0] == reference.id
        ):
            state = RefState.REFERENCE
        else:
            state = RefState.SHOWN
        refs.append(RecurrenceRef(mid, state))
    return Digest(
        text=digest.text,
        shown_ids=digest.shown_ids,
        omitted=digest.omitted,
        refs=tuple(refs),
    )
