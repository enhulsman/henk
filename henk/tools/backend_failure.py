"""One home for the three sentences a tool renders when a backend fails.

The wording was born in `homelab_query`'s transport boundary and is now shared,
because two tools rendering "almost the same" failure sentence is how a result
starts sounding like two different systems. Three shapes, no more:

- a timeout, which states the bound the caller actually used;
- a non-2xx, which states the status and nothing about its body — an error body
  is backend-authored text and is not rendered;
- anything else the transport raised, whose reason IS rendered because the cause
  is what the owner needs, and is therefore passed through
  :func:`~henk.tools.query_projection.scrub_addresses` first: httpx error text
  routinely carries the address it failed to reach.
"""

from __future__ import annotations

import httpx

from henk.tools.query_projection import scrub_addresses


def backend_failure_reason(backend: str, exc: Exception, *, timeout: float) -> str:
    """The sentence for one failed backend call.

    ``timeout`` is the bound the call was made with, not a configured default:
    the sentence claims what was waited for, so it is passed in rather than read
    from anywhere.
    """
    if isinstance(exc, httpx.TimeoutException):
        return f"{backend} timed out after {timeout:.0f}s"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{backend} returned HTTP {exc.response.status_code}"
    return f"{backend} request failed: {scrub_addresses(str(exc))}"
