"""BUILTIN_HOST_TOOLS names every built-in the bundled CLI puts in the model's context.

The list is hygiene, not the boundary (the ``PreToolUse`` hook is): a built-in it
misses is still blocked, but the model sees it and describes it. That happened live
on 2026-09-26, when Henk told the owner it "shows me some general-purpose tools, like
scheduling, multi-agent workflows and project syncing" — the list dated from
2026-07-20 and the CLI had grown Cron*, Workflow and DesignSync since.

The snapshot below is the ``tools`` array of the CLI's ``system``/``init`` message,
taken 2026-09-27 from the CLI bundled in the pinned SDK wheel (sha256 as in
``uv.lock``), driven through ``ClaudeSDKClient`` with Henk's own option shape: empty
``setting_sources``, ``strict_mcp_config``, an in-process ``henk`` MCP server,
``can_use_tool`` and a ``PreToolUse`` hook, no ``disallowed_tools``, a dummy API key
and an empty config dir. ``can_use_tool`` matters: without it AskUserQuestion and the
plan-mode tools are absent. The live test at the bottom repeats that probe whenever
the SDK is installed; the pin test makes an SDK bump fail here until it is re-taken.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from henk.agent.sdk_session import BUILTIN_HOST_TOOLS

SNAPSHOT_SDK = "0.2.160"
SNAPSHOT_CLI = "2.1.283"
SNAPSHOT_BUILTINS = frozenset(
    {
        "AskUserQuestion",
        "Bash",
        "CronCreate",
        "CronDelete",
        "CronList",
        "DesignSync",
        "Edit",
        "EnterPlanMode",
        "EnterWorktree",
        "ExitPlanMode",
        "ExitWorktree",
        "ListAgents",
        "Monitor",
        "NotebookEdit",
        "PushNotification",
        "Read",
        "ReportFindings",
        "ScheduleWakeup",
        "SendMessage",
        "Skill",
        "Task",
        "TaskStop",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Workflow",
        "Write",
    }
)


def test_list_names_every_builtin_of_the_pinned_cli():
    missing = SNAPSHOT_BUILTINS - set(BUILTIN_HOST_TOOLS)
    assert not missing, f"CLI {SNAPSHOT_CLI} built-ins not in BUILTIN_HOST_TOOLS: {sorted(missing)}"


def test_list_names_remote_trigger():
    # Declared in the 2.1.283 bundle but enabled only under subscription auth,
    # which the dummy-key snapshot cannot reach and rp5's OAuth token does.
    assert "RemoteTrigger" in BUILTIN_HOST_TOOLS


def test_snapshot_is_of_the_pinned_sdk():
    # A bump brings a new bundled CLI and possibly new built-ins: re-take the
    # snapshot (see the module docstring), then move these constants.
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    pin = re.search(r'"claude-agent-sdk==([0-9.]+)"', pyproject)
    assert pin is not None, "claude-agent-sdk must stay an exact pin"
    assert pin.group(1) == SNAPSHOT_SDK, (
        f"SDK pin moved to {pin.group(1)}; re-take the built-in snapshot for its CLI"
    )


def test_list_has_no_duplicates():
    assert len(BUILTIN_HOST_TOOLS) == len(set(BUILTIN_HOST_TOOLS))


async def _announced_tools(config_dir: Path, disallowed: list[str]) -> list[str]:
    """The ``tools`` of the installed CLI's init message, under Henk's option shape.

    No request leaves the machine: the base URL is a closed loopback port and the
    probe stops at the init message. That also hides first-party-only built-ins
    (ToolSearch), which the pinned snapshot covers.
    """
    sdk = pytest.importorskip("claude_agent_sdk")

    @sdk.tool("probe", "no-op", {})
    async def probe(args):
        return {"content": [{"type": "text", "text": ""}]}

    async def deny(name, data, context):
        return sdk.PermissionResultDeny(message="probe")

    async def pass_through(input_data, tool_use_id, context):
        return {}

    options = sdk.ClaudeAgentOptions(
        model="claude-opus-5-5",
        system_prompt="probe",
        mcp_servers={"henk": sdk.create_sdk_mcp_server(name="henk", tools=[probe])},
        allowed_tools=[],
        disallowed_tools=disallowed,
        permission_mode="default",
        can_use_tool=deny,
        hooks={"PreToolUse": [sdk.HookMatcher(matcher="*", hooks=[pass_through])]},
        setting_sources=[],
        strict_mcp_config=True,
        cwd=str(config_dir),
        env={
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "HOME": str(config_dir),
            "ANTHROPIC_API_KEY": "sk-ant-probe",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
        },
    )

    async def first_init():
        async with sdk.ClaudeSDKClient(options=options) as client:
            await client.query("probe")
            async for message in client.receive_messages():
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    return list(message.data["tools"])
        raise AssertionError("CLI exited without an init message")

    return await asyncio.wait_for(first_init(), 90)


async def test_installed_cli_ships_no_unlisted_builtin(tmp_path):
    announced = await _announced_tools(tmp_path, disallowed=[])
    builtins = {name for name in announced if not name.startswith("mcp__")}
    assert builtins, "probe saw no built-ins at all; the probe itself is broken"
    unlisted = builtins - set(BUILTIN_HOST_TOOLS)
    assert not unlisted, f"installed CLI ships built-ins not in BUILTIN_HOST_TOOLS: {sorted(unlisted)}"


async def test_installed_cli_shows_only_henk_tools_under_the_list(tmp_path):
    announced = await _announced_tools(tmp_path, disallowed=list(BUILTIN_HOST_TOOLS))
    assert announced == ["mcp__henk__probe"]
