"""The incidents a session is triaging, as the application knows them.

``publish_handoff`` stores each handoff with the identity keys, rule keys and nodes
of the incidents its session was started for (triage-handoff spec). Those come from
here, never from the model: the tool's interface accepts only the document
(NORTH-STAR principle 4), and the core is the only writer of the context — it
publishes one when an event starts a session and clears it when that session
closes (design D8). An owner follow-up inside an event-started session therefore
still carries the incidents; a session no event started carries none, and its
handoffs are not retained.

The derivations are the related-handoff digest's (design D9):

- **rule key**: the identity key with its per-subject ``identity_scope`` suffix
  removed, so one rule's handoffs for different subjects relate;
- **nodes**: whole-word matches of the closed node set and the known exporter job
  names in the incident's title and message. The result is always a subset of
  :data:`NODES` — a node *name*, never an address. URLs are removed before
  matching: a Grafana ``Source:``/``Silence:`` link names the Grafana host, not the
  alert's subject (evidence-probe 1.5), and a host name holding a node token would
  otherwise tag every alert with that node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from henk.events.types import AlertIdentity, Event
from henk.tools.query_projection import NODE_FOR_JOB

#: The closed node set, in the order derived nodes are reported.
NODES: tuple[str, ...] = ("rp5", "vps", "rp2")

#: Every token that names a node, mapped to that node: the node names themselves,
#: plus the exporter job names (`node-exporter-vps` -> `vps`). Nothing else.
_NODE_TOKENS: dict[str, str] = {
    **{node: node for node in NODES},
    **{job: node for job, node in NODE_FOR_JOB.items() if node in NODES},
}

# Whole word: not preceded or followed by a letter, digit or underscore. A hyphen,
# dot, slash or space is a boundary, so `vps` matches inside `node-exporter-vps`
# (both routes give the same node, per the 1.5 probe) but not inside `myvps`,
# `vps2` or `rp5_backup`. ASCII on purpose, so a Unicode letter next to a token is
# a boundary rather than silently part of a word.
_WORD_LEFT = r"(?<![A-Za-z0-9_])"
_WORD_RIGHT = r"(?![A-Za-z0-9_])"
_NODE_PATTERN = re.compile(
    _WORD_LEFT
    + "(?P<token>"
    # Longest first, so a job name wins over a node token inside it.
    + "|".join(re.escape(t) for t in sorted(_NODE_TOKENS, key=len, reverse=True))
    + ")"
    + _WORD_RIGHT,
    re.IGNORECASE,
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")


def derive_nodes(title: str, message: str) -> frozenset[str]:
    """The closed-set nodes an incident's title and message name, as whole words."""
    found: set[str] = set()
    for text in (title or "", message or ""):
        for match in _NODE_PATTERN.finditer(_URL.sub(" ", text)):
            found.add(_NODE_TOKENS[match.group("token").lower()])
    return frozenset(found)


def derive_rule_key(identity: AlertIdentity) -> str:
    """The identity key without its per-subject scope suffix (design D9).

    Only Grafana keys carry a scope (``grafana:{name}/{value}``,
    henk/events/identity.py:101-110), so for Grafana the rule key is
    ``grafana:{name}``. Every other source's key has no suffix and is its own rule
    key. That is deliberately not ``f"{source}:{name}"`` for them: the ``other``
    fallback keys on the *normalized* title while its name is the raw one, and two
    events of one identity must never get two rule keys.
    """
    if identity.source == "grafana":
        return f"grafana:{identity.name}"
    return identity.key


class _Item(Protocol):
    """What the context reads from an incident: an event and its identity."""

    @property
    def event(self) -> Event: ...

    @property
    def identity(self) -> AlertIdentity: ...


class _Turn(Protocol):
    @property
    def items(self) -> Iterable[_Item]: ...


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class IncidentContext:
    """The identity keys, rule keys and nodes of one session's incidents.

    Keys are de-duplicated in first-seen order; nodes are in :data:`NODES` order.
    ``empty`` means no incident: the session was not started by an event.
    """

    identity_keys: tuple[str, ...] = ()
    rule_keys: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.identity_keys

    @classmethod
    def from_items(cls, items: Iterable[_Item]) -> "IncidentContext":
        items = list(items)
        nodes: set[str] = set()
        for item in items:
            nodes |= derive_nodes(item.event.title, item.event.message)
        return cls(
            identity_keys=_unique(item.identity.key for item in items),
            rule_keys=_unique(derive_rule_key(item.identity) for item in items),
            nodes=tuple(node for node in NODES if node in nodes),
        )

    @classmethod
    def from_turn(cls, turn: _Turn) -> "IncidentContext":
        """The context of an :class:`~henk.agent.turns.EventTurn`'s incidents."""
        return cls.from_items(turn.items)


EMPTY_INCIDENT_CONTEXT = IncidentContext()


class IncidentContextProvider:
    """Holds the current session's :class:`IncidentContext`.

    One instance is shared by the runtime: the core writes it (``publish`` when an
    event starts a session, ``clear`` when the session closes) and
    ``publish_handoff`` reads it (``current``). Nothing a tool call carries can
    reach ``publish``. Single-threaded by design, like the core's serial queue.
    """

    def __init__(self) -> None:
        self._current = EMPTY_INCIDENT_CONTEXT

    def publish(self, context: IncidentContext) -> None:
        self._current = context

    def clear(self) -> None:
        self._current = EMPTY_INCIDENT_CONTEXT

    def current(self) -> IncidentContext:
        return self._current
