"""todo_read — read-only todos from obsidian-todo-api (vps:8089), GET only.

Personal-data-scoping (Tier-W): the obsidian vault mixes personal and work/Anamata
notes, so this tool enforces a **default-deny note-path allowlist** in Henk's own
process. Only todos whose source note matches an allowlisted folder-boundary prefix
are surfaced; everything else is dropped. An empty/unset allowlist surfaces nothing
(fail closed). The backend's own ``source_note`` query filter is defense-in-depth
only (substring, single-valued, fail-open) — never the security boundary.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Sequence

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.todo_read")


class _Prefix(NamedTuple):
    """A normalized allowlist entry.

    ``wire`` is the pre-trailing-slash form sent to the backend (the largest
    substring that actually occurs in real note paths). ``with_slash`` /
    ``sans_slash`` drive the tool-side folder-boundary match.
    """

    wire: str
    with_slash: str
    sans_slash: str


def _normalize_allowlist(entries: Sequence[str]) -> list[_Prefix]:
    """Strict normalization (design D3): strip whitespace → strip a leading ``/`` →
    discard now-empty entries with a WARNING. Surviving entries are folder-boundary
    forms. If nothing survives, the allowlist is empty → default-deny (D2)."""
    result: list[_Prefix] = []
    for raw in entries:
        stripped = raw.strip()
        if stripped.startswith("/"):
            stripped = stripped[1:]
        stripped = stripped.strip()
        if not stripped:
            logger.warning(
                "todo_read: dropped empty/whitespace allowlist entry %r "
                "(does not broaden scope)",
                raw,
            )
            continue
        with_slash = stripped if stripped.endswith("/") else stripped + "/"
        result.append(
            _Prefix(wire=stripped, with_slash=with_slash, sans_slash=with_slash[:-1])
        )
    return result


class TodoReadTool(Tool):
    name = "todo_read"
    description = "Fetch current todos from obsidian-todo-api. Read-only, no arguments."
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str = "",
        path: str = "/api/todos",
        timeout: float = 10.0,
        note_allowlist: Sequence[str] = (),
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._path = path if path.startswith("/") else f"/{path}"
        self._timeout = timeout
        self._allowlist = _normalize_allowlist(note_allowlist)

    @property
    def effective_allowlist(self) -> tuple[str, ...]:
        """The wire forms of the surviving allowlist entries (empty → fail closed)."""
        return tuple(p.wire for p in self._allowlist)

    async def _run(self) -> ToolResult:  # type: ignore[override]
        # Default-deny: with no effective allowlist, surface nothing — and don't even
        # fetch, so no work-note text crosses into Henk's process (data minimization).
        if not self._allowlist:
            return ToolResult.success("No allowlisted todos.")

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        # Defense-in-depth only: send the backend filter iff the effective allowlist
        # has exactly one entry (a single-valued substring filter cannot express a
        # multi-prefix allowlist without silently dropping the others). Sent in its
        # pre-trailing-slash form so it matches real note paths (D1).
        params: dict[str, str] = {}
        if len(self._allowlist) == 1:
            params["source_note"] = self._allowlist[0].wire
        try:
            resp = await self._client.get(
                f"{self._base_url}{self._path}",
                headers=headers,
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            return ToolResult.failure(
                f"obsidian-todo-api timed out after {self._timeout:.0f}s"
            )
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(
                f"obsidian-todo-api returned HTTP {exc.response.status_code}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.failure(f"obsidian-todo-api request failed: {exc}")

        return self._summarize(data)

    def _allow_match(self, note_path: str) -> bool:
        """Folder-boundary match on an already-normalized note path (D3)."""
        return any(
            note_path == p.sans_slash or note_path.startswith(p.with_slash)
            for p in self._allowlist
        )

    def _summarize(self, data: object) -> ToolResult:
        unexpected = ToolResult.failure(
            "obsidian-todo-api returned an unexpected response shape"
        )
        if not isinstance(data, dict):
            return unexpected
        todos = data.get("todos")
        if isinstance(todos, dict):
            groups = list(todos.items())
        elif isinstance(todos, list):
            # Defensive fallback for a flat/older shape: each item's source_note is
            # its own key (no group key to fall back to).
            groups = [(None, todos)]
        else:
            return unexpected

        survivors: list[dict] = []
        for group_key, items in groups:
            if not isinstance(items, list):
                return unexpected
            for item in items:
                if not isinstance(item, dict):
                    return unexpected
                raw_path = item.get("source_note") or group_key
                # No usable scope key → never surface (D4).
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                note_path = raw_path.strip()
                if note_path.startswith("/"):
                    note_path = note_path[1:]
                # `..` path-segment rejection (Tier-W insurance, D3): segment check,
                # not a substring — `notes..archive.md` is fine, `../Work` is not.
                if ".." in note_path.split("/"):
                    continue
                if not self._allow_match(note_path):
                    continue
                survivors.append(item)

        if not survivors:
            return ToolResult.success("No allowlisted todos.")

        # Report the ALLOWLISTED count, never the vault-wide total_count (which would
        # itself leak the existence/volume of work notes).
        lines = [f"{len(survivors)} todo(s)"]
        for item in survivors:
            text = item.get("text") or item.get("title") or item.get("task")
            done = item.get("done") or item.get("completed")
            mark = "x" if done else " "
            lines.append(f"- [{mark}] {text}")
        return ToolResult.success("\n".join(lines))
