"""todo_read tests — personal-data-scoping (spec: homelab-tools delta).

Default-deny note-path allowlist + note-grouped parsing + no-raw-dump, plus the
preserved v1 invariants (GET-only, bearer, honest errors, READ_ONLY).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from henk.tools.base import ToolClass
from henk.tools.todo_read import TodoReadTool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "obsidian_todos"
SENTINEL = "SYNTHETIC-WORK-SENTINEL"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _make_tool(handler, *, token: str = "", note_allowlist=()) -> tuple[TodoReadTool, list]:
    calls: list = []

    def recording(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    tool = TodoReadTool(
        client, base_url="http://vps:8089", token=token, note_allowlist=note_allowlist
    )
    return tool, calls


def _serve(data):
    def handler(request):
        return httpx.Response(200, json=data)

    return handler


# --- 2.1 Note-grouped response is parsed, never dumped --------------------


async def test_note_grouped_is_parsed_not_dumped():
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/", "Homelab/"])
    result = await tool._run()
    assert result.ok
    # Individual todos formatted from the groups.
    assert "buy cat food" in result.content
    assert "verify restic snapshot" in result.content
    # No code path returns the raw stringified backend response.
    assert result.content != str(data)
    assert "total_count" not in result.content
    # Raw-dump regression guard: the work sentinel is never surfaced.
    assert SENTINEL not in result.content


# --- 2.2 Default-deny: empty allowlist surfaces nothing -------------------


async def test_empty_allowlist_surfaces_nothing():
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=[])
    result = await tool._run()
    assert result.ok  # honest "nothing in scope", not an error
    assert "buy cat food" not in result.content
    assert "verify restic snapshot" not in result.content
    assert SENTINEL not in result.content
    assert "Personal/inbox.md" not in result.content


# --- 2.3 Prefix allowlist match + allowlisted count -----------------------


async def test_prefix_allowlist_surfaces_only_matches_and_counts_them():
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/", "Homelab/"])
    result = await tool._run()
    assert result.ok
    assert "buy cat food" in result.content
    assert "renew passport" in result.content
    assert "plant tomatoes" in result.content
    assert "verify restic snapshot" in result.content
    # Work dropped.
    assert SENTINEL not in result.content
    # Reported count is the allowlisted count (4), not total_count (5).
    assert "4 todo" in result.content
    assert "5 todo" not in result.content


# --- 2.4 Work-note drop, missing-key drop, ..-segment drop ----------------


async def test_work_note_is_dropped_entirely():
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert SENTINEL not in result.content
    assert "Work/sprint-planning.md" not in result.content
    assert "finalize the client invoice" not in result.content


async def test_item_with_no_usable_key_is_dropped():
    # Flat-list fallback with no source_note and no group key → no scope key → drop.
    data = {"todos": [{"text": "orphan todo"}], "total_count": 1, "note_count": 0}
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert "orphan todo" not in result.content


async def test_dotdot_path_segment_is_dropped_but_literal_dots_are_kept():
    data = {
        "todos": {
            "Personal/../Work/secret.md": [
                {"text": "SNEAKY leak", "source_note": "Personal/../Work/secret.md"}
            ],
            "Personal/notes..archive.md": [
                {"text": "archived note", "source_note": "Personal/notes..archive.md"}
            ],
        },
        "total_count": 2,
        "note_count": 2,
    }
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert "SNEAKY leak" not in result.content  # `..` segment dropped
    assert "archived note" in result.content  # literal `..` in a name is fine


# --- 2.5 API filter is not the boundary + send-gate -----------------------


async def test_backend_filter_is_not_the_boundary():
    # Fail-open backend: returns the whole (work-containing) vault regardless of param.
    data = _load("note_grouped")
    tool, calls = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    # In-process re-filter drops the out-of-scope Work todos the backend leaked.
    assert SENTINEL not in result.content
    assert "buy cat food" in result.content


async def test_query_param_sent_iff_single_entry():
    data = _load("note_grouped")

    # len == 1 → param sent, pre-trailing-slash form.
    tool1, calls1 = _make_tool(_serve(data), note_allowlist=["Personal/"])
    await tool1._run()
    assert calls1[0].url.params.get("source_note") == "Personal/"

    # len >= 2 → param omitted, and all in-scope prefixes are surfaced.
    tool2, calls2 = _make_tool(_serve(data), note_allowlist=["Personal/", "Homelab/"])
    result2 = await tool2._run()
    assert calls2[0].url.params.get("source_note") is None
    assert "buy cat food" in result2.content  # Personal/
    assert "verify restic snapshot" in result2.content  # Homelab/


async def test_single_file_path_entry_sent_pre_trailing_slash():
    data = _load("note_grouped")
    tool, calls = _make_tool(_serve(data), note_allowlist=["Personal/inbox.md"])
    result = await tool._run()
    # Sent as the real note path, NOT `Personal/inbox.md/` (which would match nothing
    # → fail-open whole-vault fetch).
    assert calls[0].url.params.get("source_note") == "Personal/inbox.md"
    # Tool-side re-filter yields exactly that file's todos.
    assert "buy cat food" in result.content
    assert "renew passport" in result.content
    assert "plant tomatoes" not in result.content  # different file
    assert SENTINEL not in result.content


# --- 2.6 Unexpected shape fails safe --------------------------------------


async def test_unexpected_top_level_shape_errors_without_content():
    tool, _ = _make_tool(_serve(["not", "a", "dict"]), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok is False
    assert "unexpected" in (result.error or "").lower()


async def test_unexpected_todos_shape_errors_without_content():
    data = {"todos": "garbage-string", "total_count": 0}
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok is False
    assert "unexpected" in (result.error or "").lower()
    assert "garbage-string" not in (result.error or "")


# --- 2.7 Preserved v1 invariants ------------------------------------------


async def test_todos_fetched_note_grouped_shape():
    # Moved to the note-grouped shape with a NON-EMPTY allowlist (else default-deny).
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert "buy cat food" in result.content
    assert "renew passport" in result.content


async def test_description_field_and_done_state_parsed_from_raw_line():
    # Regression: the real obsidian-todo-api item carries text in `description`
    # (not `text`), and the checkbox state only in `raw_line` (no done/completed
    # bool). A wrong field map rendered every todo as "None" in prod.
    data = _load("note_grouped")
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert "None" not in result.content
    assert "- [ ] buy cat food" in result.content  # raw_line "- [ ] ..."
    assert "- [x] renew passport" in result.content  # raw_line "- [x] ..."


async def test_requests_api_todos_path():
    # obsidian-todo-api serves todos at /api/todos, not /todos (a /todos GET 404s).
    def handler(request):
        assert request.url.path == "/api/todos"
        return httpx.Response(200, json={"todos": {}, "total_count": 0, "note_count": 0})

    tool, calls = _make_tool(handler, note_allowlist=["Personal/"])
    assert (await tool._run()).ok
    assert calls[0].url.path == "/api/todos"


async def test_only_get_method_used():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"todos": {}, "total_count": 0, "note_count": 0})

    tool, calls = _make_tool(handler, note_allowlist=["Personal/"])
    await tool._run()
    assert calls and all(r.method == "GET" for r in calls)


async def test_token_used_as_bearer():
    def handler(request):
        assert request.headers.get("authorization") == "Bearer todo-tk"
        return httpx.Response(200, json={"todos": {}, "total_count": 0, "note_count": 0})

    tool, _ = _make_tool(handler, token="todo-tk", note_allowlist=["Personal/"])
    assert (await tool._run()).ok


async def test_timeout_is_honest():
    def handler(request):
        raise httpx.TimeoutException("slow", request=request)

    tool, _ = _make_tool(handler, note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok is False
    assert "timed out" in (result.error or "")


async def test_non_2xx_is_honest():
    def handler(request):
        return httpx.Response(503)

    tool, _ = _make_tool(handler, note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok is False
    assert "503" in (result.error or "")


def test_classification_is_read_only():
    assert TodoReadTool.tool_class is ToolClass.READ_ONLY


# --- 2.8 Empty-entry normalization ----------------------------------------


async def test_empty_and_slash_entries_collapse_to_default_deny(caplog):
    data = _load("note_grouped")
    with caplog.at_level(logging.WARNING, logger="henk.tools.todo_read"):
        tool, _ = _make_tool(_serve(data), note_allowlist=["", "/"])
    result = await tool._run()
    assert result.ok
    assert "buy cat food" not in result.content  # collapses to []
    assert SENTINEL not in result.content
    # A WARNING names each dropped empty/whitespace entry.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("''" in m or '""' in m for m in warnings)
    assert any("'/'" in m or '"/"' in m for m in warnings)


async def test_slash_entry_dropped_leaves_real_prefix(caplog):
    data = _load("note_grouped")
    with caplog.at_level(logging.WARNING, logger="henk.tools.todo_read"):
        tool, _ = _make_tool(_serve(data), note_allowlist=["/", "Personal/"])
    result = await tool._run()
    assert result.ok
    assert "buy cat food" in result.content  # Personal/ survives
    assert "verify restic snapshot" not in result.content  # Homelab/ not allowed
    assert SENTINEL not in result.content


# --- 2.9 Folder-boundary match --------------------------------------------


async def test_folder_boundary_match_rejects_siblings_and_root():
    data = {
        "todos": {
            "Personal/x.md": [{"text": "px", "source_note": "Personal/x.md"}],
            "Personal/sub/y.md": [{"text": "py", "source_note": "Personal/sub/y.md"}],
            "Personal-work/z.md": [{"text": "pw", "source_note": "Personal-work/z.md"}],
            "PersonalNotes/w.md": [{"text": "pn", "source_note": "PersonalNotes/w.md"}],
            "Personal.md": [{"text": "proot", "source_note": "Personal.md"}],
        },
        "total_count": 5,
        "note_count": 5,
    }
    tool, _ = _make_tool(_serve(data), note_allowlist=["Personal/"])
    result = await tool._run()
    assert result.ok
    assert "px" in result.content
    assert "py" in result.content
    assert "pw" not in result.content  # Personal-work/ sibling folder
    assert "pn" not in result.content  # PersonalNotes/ same-prefix name
    assert "proot" not in result.content  # root Personal.md
    assert "2 todo" in result.content
