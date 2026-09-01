"""The projection rule as a shared primitive: how a target is named safely.

This module exists because two modules need it and neither may import the other:
the registry (which builds templates and plans) and the renderers (which turn a
backend response into text). Keeping the rule in one place is also the point —
six renderers each remembering to drop `instance` is six chances to forget, and
the failure is silent: an address that reaches a Signal reply looks exactly like
a correct result.

What it enforces, from `specs/homelab-tools`:

- a target is named by its **`job` label** and its friendly node value, together
  with any distinguishing **non-address** label (`name`, `mountpoint`);
- `instance`, `scrapeUrl`, `globalUrl`, `__address__`, `server`, `hostname` and
  `upstream` never reach output — six of the seven were measured on live
  responses, and `server` is listed precisely because it *looks* like a safe
  discriminator and is an `http://<addr>:<port>` URL;
- and backend-authored **free text** is scrubbed as well as labels, because
  Prometheus's `lastError` — which the spec requires surfacing — quotes the
  scrape URL in the ordinary case.
"""

from __future__ import annotations

import re
from typing import Mapping

#: Node enum value -> node-exporter job label (record 1.3). The registry stores
#: job names because they carry no address; `instance` labels do.
NODE_EXPORTER_JOBS: Mapping[str, str] = {
    "rp5": "node-exporter-pi5",
    "vps": "node-exporter-vps",
    "rp2": "node-exporter-pi2",
}

#: Node enum value -> cadvisor job label. **rp2 is absent on purpose**: it runs
#: no cadvisor, which is why `container_state`'s node domain is narrower.
CADVISOR_JOBS: Mapping[str, str] = {
    "rp5": "cadvisor-pi5",
    "vps": "cadvisor-vps",
}

#: The reverse map, so a result can name a target by its friendly enum value.
#: Jobs with no node of their own (`adguard-exporter`, `pushgateway`) are absent
#: and are named by their job label instead.
NODE_FOR_JOB: Mapping[str, str] = {
    **{job: node for node, job in NODE_EXPORTER_JOBS.items()},
    **{job: node for node, job in CADVISOR_JOBS.items()},
}

#: Labels and fields that carry an address and must never be rendered. Six of the
#: seven were measured on live responses; `upstream` comes from the AdGuard
#: metric the registry deliberately does not use. `server` is listed because it
#: LOOKS like a safe discriminator and is not: it is a URL containing a tailnet
#: address.
ADDRESS_BEARING_LABELS: frozenset[str] = frozenset(
    {
        "instance",
        "server",
        "scrapeUrl",
        "globalUrl",
        "__address__",
        "hostname",
        "upstream",
    }
)

_ADDRESS_BEARING_LOWER = frozenset(label.lower() for label in ADDRESS_BEARING_LABELS)

#: Free text a backend hands back can carry an address even when no label does.
#: The clearest case is Prometheus's `lastError`, which reads
#: `Get "http://<addr>:9100/metrics": dial tcp <addr>: connect: connection
#: refused` — the cause is exactly what the owner needs and the address is
#: exactly what must not be rendered. Dropping the field would lose the answer;
#: scrubbing keeps it. Order matters: a whole URL is redacted before the bare
#: address inside it is reached.
_SCRUB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
    re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net(?::\d+)?\b"),
)

_REDACTED = "<address redacted>"


def is_address_shaped(value: str) -> bool:
    """A value that looks like an address, whatever label it arrives under.

    The name-based denial above only catches labels someone enumerated; a new
    exporter label is exactly how the next address would reach a result.
    """
    if "://" in value:
        return True
    parts = value.split(":", 1)[0].split(".")
    return len(parts) == 4 and all(part.isdigit() for part in parts)


def scrub_addresses(text: str) -> str:
    """Redact address-shaped tokens from backend free text, keeping the rest.

    Applied to every string a backend authors that reaches a result — scrape
    errors, Gatus condition text — so the projection rule holds for prose as well
    as for labels. Text carrying no address comes back byte-identical.
    """
    if not isinstance(text, str) or not text:
        return text
    scrubbed = text
    for pattern in _SCRUB_PATTERNS:
        scrubbed = pattern.sub(_REDACTED, scrubbed)
    return scrubbed


def project_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Drop every address-bearing label from a backend's own label set.

    Two filters, because either alone leaks. The name denylist catches the six
    labels measured on live responses; the value-shape check catches the label
    nobody has seen yet, which is how the next address would arrive.
    """
    projected: dict[str, str] = {}
    for name, value in labels.items():
        if name.startswith("__"):
            continue
        if name.lower() in _ADDRESS_BEARING_LOWER:
            continue
        if isinstance(value, str) and is_address_shaped(value):
            continue
        projected[name] = value
    return projected


def friendly_target(job: str) -> str:
    """The node enum value for a job, or the job label when it has no node.

    `adguard-exporter` and `pushgateway` have no node of their own, so they are
    named by their job — which is still address-free.
    """
    return NODE_FOR_JOB.get(job, job)


def describe_target(job: str, labels: Mapping[str, str] | None = None) -> str:
    """Name a target by its friendly value, its job, and its safe labels."""
    projected = project_labels(labels or {})
    projected.pop("job", None)
    parts = [f"job={job}"] + [f"{k}={v}" for k, v in sorted(projected.items())]
    return f"{friendly_target(job)} ({', '.join(parts)})"
