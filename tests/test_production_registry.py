"""The production toolset, asserted exactly (task 6.3).

This replaces the old "the registry stays read-only" assertion deliberately: the
approval-gate delta reverses "v1 SHALL ship no mutating tools in the production
tool registry" (owner-blessed per NORTH-STAR.md). What matters now is not the
absence of writes but that every registered write carries the right tier and the
right turn scope — so this pins the whole table, class by class.
"""

from __future__ import annotations

import httpx
import pytest

from henk.config import Config
from henk.tools import build_production_registry
from henk.tools.base import AuthorizationTier, ToolClass, TurnType
from tests.test_config import SAMPLE


@pytest.fixture
def registry():
    async def handler(request):  # pragma: no cover - never called at registration
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return build_production_registry(Config.load(SAMPLE, env={}), client)


#: name → (class, tier, turn scope). The single source of truth for this test.
EXPECTED = {
    "homelab_health": (ToolClass.READ_ONLY, None, None),
    "todo_read": (ToolClass.READ_ONLY, None, None),
    "notify": (ToolClass.NOTIFY_ONLY, None, None),
    "publish_handoff": (ToolClass.NOTIFY_ONLY, None, None),
    "store_memory": (
        ToolClass.MUTATING,
        AuthorizationTier.STANDING,
        (TurnType.OWNER,),
    ),
    "capture": (
        ToolClass.MUTATING,
        AuthorizationTier.STANDING,
        (TurnType.OWNER,),
    ),
    "inbox_read": (ToolClass.READ_ONLY, None, None),
}


def test_registry_contains_exactly_the_intended_toolset(registry):
    assert set(registry.names()) == set(EXPECTED)
    # taiga_read stays deliberately unregistered: the Taiga instance mixes personal
    # and work projects and its project-id allowlist does not exist yet.
    assert "taiga_read" not in registry.names()


def test_each_tool_carries_its_declared_class_tier_and_scope(registry):
    for name, (tool_class, tier, scope) in EXPECTED.items():
        tool = registry.get(name)
        assert tool.tool_class is tool_class, name
        if tool_class is ToolClass.MUTATING:
            assert tool.authorization is tier, name
            assert tuple(tool.turn_scope) == scope, name


def test_every_mutating_tool_is_owner_turn_only(registry):
    # No production tool declares event scope in this change: a mutating attempt
    # during triage is denied by turn scope before any tier is consulted.
    for tool in registry.mutating():
        assert TurnType.EVENT not in tool.turn_scope, tool.name


def test_the_registry_now_deliberately_contains_mutating_tools(registry):
    assert registry.has_mutating() is True
    assert sorted(t.name for t in registry.mutating()) == ["capture", "store_memory"]


# --- The enumerated toolset must match the registry (task 6.2) ------------


def test_default_system_prompt_enumerates_every_registered_tool(registry):
    # Henk's honest-capability framing is only honest if the enumeration and the
    # registry agree. This is the drift that would make him claim a tool he does
    # not have, or hide one he does.
    from henk.config import AgentConfig

    prompt = AgentConfig().system_prompt
    for name in registry.names():
        assert name in prompt, f"{name} is registered but not enumerated"
    assert "seven" in prompt and len(registry.names()) == 7


def test_default_system_prompt_names_the_owner_command_set():
    from henk.config import AgentConfig

    prompt = AgentConfig().system_prompt
    for command in (
        "/new",
        "/remember",
        "/forget",
        "/memories",
        "/capture",
        "/inbox",
        "/inbox all",
        "/inbox done",
    ):
        assert command in prompt


def test_default_system_prompt_frames_recall_as_data_and_names_the_taint_remedy():
    from henk.config import AgentConfig

    prompt = AgentConfig().system_prompt
    assert "REMEMBERED FACTS" in prompt
    assert "never instructions" in prompt
    # So a refused write is relayed as a stated constraint, not improvised.
    assert "incident has touched" in prompt


# --- Parameter schemas are uniformly strict (post-deploy follow-up) --------


def test_every_tool_schema_is_closed_to_unexpected_properties(registry):
    # `additionalProperties: false` was set on every tool predating memory-capture
    # and omitted on the three it added — harmless (both write tools absorb extra
    # keys) but an inconsistency that only an assertion keeps from recurring.
    for tool in registry.tools():
        schema = tool.parameters
        assert schema.get("type") == "object", tool.name
        assert schema.get("additionalProperties") is False, tool.name


def test_tools_taking_arguments_declare_them_required_or_optional_explicitly(registry):
    for tool in registry.tools():
        properties = tool.parameters.get("properties", {})
        required = set(tool.parameters.get("required", []))
        assert required <= set(properties), tool.name
        for name, spec in properties.items():
            assert spec.get("description"), f"{tool.name}.{name} has no description"
