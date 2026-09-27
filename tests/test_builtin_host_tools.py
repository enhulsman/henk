"""No CLI built-in reaches the model's context: ``tools=[]`` first, BUILTIN_HOST_TOOLS second.

Both layers are hygiene, not the boundary (the ``PreToolUse`` hook is): a built-in
they miss is still blocked, but the model sees it and describes it. That happened live
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

The list alone proved incomplete on its first deploy (2026-09-27): rp5 runs on a
subscription token, and there Henk still saw ``ShareOnboardingGuide``, which the CLI
enables per account and no dummy-key probe can show. A deny-list cannot enumerate
tools gated on an account it cannot reproduce, so the closed toolset now also passes
``tools=[]`` (``--tools ""``: enable no built-in), which needs no enumeration. The
list stays as the second layer. The subscription-only case is verified on the phone
at deploy, not here.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from henk.agent.sdk_session import (
    BUILTIN_HOST_TOOLS,
    SdkSessionFactory,
    build_closed_toolset_config,
    toolset_options,
)
from henk.tools.base import ToolRegistry

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


def _henk_config():
    return build_closed_toolset_config(ToolRegistry(), model="m", system_prompt="p")


def test_the_closed_toolset_enables_no_builtin():
    cfg = _henk_config()
    assert cfg.builtin_tools == ()
    options = toolset_options(cfg)
    # An empty list, not None: None leaves the CLI's default set enabled.
    assert options["tools"] == []
    assert options["disallowed_tools"] == list(BUILTIN_HOST_TOOLS)
    assert options["allowed_tools"] == []
    assert options["permission_mode"] == "default"
    assert options["setting_sources"] == []
    assert options["strict_mcp_config"] is True


def test_toolset_options_are_valid_claude_agent_options():
    sdk = pytest.importorskip("claude_agent_sdk")
    options = sdk.ClaudeAgentOptions(**toolset_options(_henk_config()))
    assert options.tools == []
    assert options.disallowed_tools == list(BUILTIN_HOST_TOOLS)


def test_a_created_session_carries_the_toolset():
    # create() itself is deploy-only; this pins that it spreads toolset_options
    # into the options its client gets, not a hand-copied subset of them.
    pytest.importorskip("claude_agent_sdk")
    factory = SdkSessionFactory(ToolRegistry(), gate=None, model="m", system_prompt="p")
    options = factory.create()._client.options
    assert options.tools == []
    assert options.disallowed_tools == list(BUILTIN_HOST_TOOLS)
    assert options.allowed_tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "default"


async def _announced_tools(config_dir: Path, toolset: dict) -> list[str]:
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
        can_use_tool=deny,
        hooks={"PreToolUse": [sdk.HookMatcher(matcher="*", hooks=[pass_through])]},
        **toolset,
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


def _toolset(**overrides) -> dict:
    """Henk's real toolset kwargs, with one layer switched off for a probe."""
    toolset = toolset_options(_henk_config())
    toolset.update(overrides)
    return {k: v for k, v in toolset.items() if v is not None}


async def test_installed_cli_ships_no_unlisted_builtin(tmp_path):
    # Both layers off, so every built-in shows: the list's own drift check.
    announced = await _announced_tools(tmp_path, _toolset(tools=None, disallowed_tools=[]))
    builtins = {name for name in announced if not name.startswith("mcp__")}
    assert builtins, "probe saw no built-ins at all; the probe itself is broken"
    unlisted = builtins - set(BUILTIN_HOST_TOOLS)
    assert not unlisted, f"installed CLI ships built-ins not in BUILTIN_HOST_TOOLS: {sorted(unlisted)}"


async def test_installed_cli_shows_only_henk_tools_under_the_list(tmp_path):
    announced = await _announced_tools(tmp_path, _toolset(tools=None))
    assert announced == ["mcp__henk__probe"]


async def test_installed_cli_shows_only_henk_tools_with_no_builtin_enabled(tmp_path):
    # tools=[] on its own, list empty: the layer that needs no enumeration.
    announced = await _announced_tools(tmp_path, _toolset(disallowed_tools=[]))
    assert announced == ["mcp__henk__probe"]


async def test_installed_cli_shows_only_henk_tools_under_henks_toolset(tmp_path):
    announced = await _announced_tools(tmp_path, toolset_options(_henk_config()))
    assert announced == ["mcp__henk__probe"]
