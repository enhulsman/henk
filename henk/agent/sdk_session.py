"""Claude Agent SDK wrapper — the only module that imports ``claude_agent_sdk``.

``build_closed_toolset_config`` is a pure function: it produces the closed-toolset
configuration (exactly the registered Henk tools exposed, every host-touching
built-in disabled) and is unit-tested without the SDK installed. The real
``SdkSessionFactory`` translates that config into SDK options and is exercised at
deploy time (task 1.4 pins the SDK version and confirms the exact option names).
"""

from __future__ import annotations

from dataclasses import dataclass

from henk.tools.base import ToolRegistry

#: In-process MCP server name under which Henk tools are exposed to the SDK.
MCP_SERVER_NAME = "henk"

#: Host-touching built-in tools the SDK ships. All are disabled so the agent can
#: act only through registered Henk tools (agent-core "closed, explicit toolset").
BUILTIN_HOST_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
)


def mcp_tool_name(tool_name: str) -> str:
    """SDK-visible name for an in-process MCP tool."""
    return f"mcp__{MCP_SERVER_NAME}__{tool_name}"


@dataclass(frozen=True)
class ClosedToolsetConfig:
    """SDK-agnostic description of the session's closed toolset."""

    model: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]

    def exposes_builtin(self) -> bool:
        allowed = set(self.allowed_tools)
        return any(builtin in allowed for builtin in BUILTIN_HOST_TOOLS)


def build_closed_toolset_config(
    registry: ToolRegistry, *, model: str, system_prompt: str
) -> ClosedToolsetConfig:
    """Build the closed-toolset config from a registry.

    ``allowed_tools`` is exactly the registered Henk tools (MCP-namespaced);
    every host-touching built-in is placed in ``disallowed_tools``.
    """
    allowed = tuple(mcp_tool_name(name) for name in registry.names())
    return ClosedToolsetConfig(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=allowed,
        disallowed_tools=tuple(BUILTIN_HOST_TOOLS),
    )


class SdkSessionFactory:
    """Deploy-time factory that builds real Claude Agent SDK sessions.

    Imports ``claude_agent_sdk`` lazily so that importing this module (for the
    pure config helpers above) never requires the SDK to be installed.
    """

    def __init__(self, registry: ToolRegistry, *, model: str, system_prompt: str):
        self._registry = registry
        self._config = build_closed_toolset_config(
            registry, model=model, system_prompt=system_prompt
        )

    @property
    def config(self) -> ClosedToolsetConfig:
        return self._config

    def create(self):  # pragma: no cover - requires the SDK + live credentials
        raise NotImplementedError(
            "SdkSessionFactory.create is wired at deploy time against "
            "claude_agent_sdk; see task 1.4/3.4. Tests use a fake SessionFactory."
        )
