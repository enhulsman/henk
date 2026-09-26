"""Reasoning settings (agent.effort / agent.thinking): loaded, validated, passed to the SDK.

Before this, no effort or thinking setting reached ``ClaudeAgentOptions``, so every
session ran on whatever the bundled CLI defaulted to — unstated and unverified. The
settings are explicit now, and the defaults are the effective values on rp5, whose
locally modified config.yaml carries neither key.
"""

from __future__ import annotations

import pytest

from henk.agent.sdk_session import build_closed_toolset_config, reasoning_options
from henk.config import AgentConfig, Config, ConfigError
from henk.tools.base import ToolRegistry
from tests.test_config import _minimal_raw


def _load(agent_section):
    raw = _minimal_raw("+31600000000")
    if agent_section is not None:
        raw["agent"] = agent_section
    return Config.from_dict(raw, env={})


def test_defaults_are_high_effort_and_adaptive_thinking():
    config = _load(None)
    assert config.agent.effort == "high"
    assert config.agent.thinking == "adaptive"
    # The dataclass default and the loader default must agree: rp5 relies on it.
    assert AgentConfig.effort == "high"
    assert AgentConfig.thinking == "adaptive"


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_every_sdk_effort_level_is_accepted(level):
    assert _load({"effort": level}).agent.effort == level


def test_null_effort_defers_to_the_cli_default():
    assert _load({"effort": None}).agent.effort is None


def test_unknown_effort_is_refused_and_named():
    with pytest.raises(ConfigError) as excinfo:
        _load({"effort": "extreme"})
    assert "agent.effort" in str(excinfo.value)
    assert "extreme" in str(excinfo.value)


def test_thinking_disabled_is_accepted():
    assert _load({"thinking": "disabled"}).agent.thinking == "disabled"


def test_unknown_thinking_mode_is_refused_and_named():
    # "enabled" needs a token budget, which current models reject; not offered.
    with pytest.raises(ConfigError) as excinfo:
        _load({"thinking": "enabled"})
    assert "agent.thinking" in str(excinfo.value)


def test_reasoning_options_carry_both_settings():
    cfg = build_closed_toolset_config(
        ToolRegistry(), model="m", system_prompt="p", effort="high", thinking="adaptive"
    )
    assert reasoning_options(cfg) == {
        "effort": "high",
        "thinking": {"type": "adaptive"},
    }


def test_reasoning_options_omit_unset_settings():
    # None means "leave it to the CLI": the key must be absent, not passed as None.
    cfg = build_closed_toolset_config(ToolRegistry(), model="m", system_prompt="p")
    assert reasoning_options(cfg) == {}


def test_thinking_disabled_maps_to_the_sdk_shape():
    cfg = build_closed_toolset_config(
        ToolRegistry(), model="m", system_prompt="p", thinking="disabled"
    )
    assert reasoning_options(cfg) == {"thinking": {"type": "disabled"}}


def test_reasoning_options_are_valid_claude_agent_options():
    # Guards the field names against the pinned SDK, which create() cannot be
    # unit-tested against (it needs live credentials).
    sdk = pytest.importorskip("claude_agent_sdk")
    cfg = build_closed_toolset_config(
        ToolRegistry(), model="m", system_prompt="p", effort="max", thinking="adaptive"
    )
    options = sdk.ClaudeAgentOptions(**reasoning_options(cfg))
    assert options.effort == "max"
    assert options.thinking == {"type": "adaptive"}


# Opus 5.5 is refused by the bundled CLI before 2.1.280 ("API Error: 400 Claude Code
# 2.1.277 does not support this model"), measured on rp5 2026-09-26. The pin is what
# decides the CLI version in the image, so both are guarded: the pin here, where the
# SDK is not installed, and the bundled CLI itself inside the image.
_MIN_CLI = (2, 1, 280)
_MIN_SDK = (0, 2, 158)  # the first release bundling CLI 2.1.280


def _version(text):
    return tuple(int(part) for part in text.split("."))


def test_pinned_sdk_bundles_a_cli_that_accepts_opus_5_5():
    import re
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    pin = re.search(r'"claude-agent-sdk==([0-9.]+)"', pyproject)
    assert pin is not None, "claude-agent-sdk must stay an exact pin"
    assert _version(pin.group(1)) >= _MIN_SDK


def test_bundled_cli_accepts_opus_5_5():
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk._cli_version import __cli_version__

    assert _version(__cli_version__) >= _MIN_CLI
