"""Configuration loading: non-secret settings from ``config.yaml``, secrets from env.

Secrets never live in the YAML file. The YAML holds endpoints, identities, and
timeouts; every credential is read from the environment (``.env`` mode-600 in
deployment, per design D7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


class ConfigError(ValueError):
    """Raised when configuration is missing a required value."""


@dataclass(frozen=True)
class OwnerConfig:
    id: str  # channel-neutral owner identity (Signal number/UUID for the Signal adapter)


@dataclass(frozen=True)
class AgentConfig:
    model: str = "claude-sonnet-5"
    idle_timeout_seconds: int = 3600
    approval_timeout_seconds: int = 300
    system_prompt: str = (
        "You are Henk, the owner's personal homelab assistant, reached over "
        "Signal.\n\n"
        "Your complete toolset is exactly these three — you have no other tools "
        "or capabilities (no scheduling, cron, workflows, web, files, or "
        "shell):\n"
        "- homelab_health — report homelab health and status.\n"
        "- todo_read — read the owner's to-do list.\n"
        "- notify — send the owner a push notification via ntfy.\n\n"
        "Use them to give real, current answers — when a request maps to a "
        "tool, call it. If something falls outside these three, say so plainly; "
        "don't describe capabilities you don't have, and only report results "
        "you actually got from a tool (never invent outcomes).\n\n"
        "Reply in plain text suited to Signal — avoid Markdown code blocks and "
        "tables."
    )


@dataclass(frozen=True)
class SignalConfig:
    bridge_url: str  # e.g. http://signal-cli-rest-api:8080 (compose-internal only)
    account: str  # Henk's dedicated Signal number
    safe_length: int = 2000


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class NtfyConfig:
    base_url: str
    topic: str
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class Secrets:
    """Credentials pulled from the environment. Values may be empty in tests."""

    anthropic_credential: str = ""
    taiga_token: str = ""
    todo_token: str = ""
    ntfy_token: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Secrets":
        return cls(
            anthropic_credential=env.get("ANTHROPIC_CREDENTIAL", ""),
            taiga_token=env.get("TAIGA_TOKEN", ""),
            todo_token=env.get("TODO_TOKEN", ""),
            ntfy_token=env.get("NTFY_TOKEN", ""),
        )


@dataclass(frozen=True)
class Config:
    owner: OwnerConfig
    agent: AgentConfig
    signal: SignalConfig
    gatus: EndpointConfig
    prometheus: EndpointConfig
    taiga: EndpointConfig
    todo: EndpointConfig
    ntfy: NtfyConfig
    secrets: Secrets = field(default_factory=Secrets)

    @classmethod
    def load(
        cls, path: str | Path, env: Mapping[str, str] | None = None
    ) -> "Config":
        env = os.environ if env is None else env
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(raw, env)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], env: Mapping[str, str]) -> "Config":
        def section(name: str) -> Mapping[str, Any]:
            value = raw.get(name)
            if not isinstance(value, Mapping):
                raise ConfigError(f"missing or invalid config section: {name!r}")
            return value

        def require(sec: Mapping[str, Any], key: str, name: str) -> Any:
            if key not in sec:
                raise ConfigError(f"missing required key {name}.{key}")
            return sec[key]

        def require_nonempty(sec: Mapping[str, Any], key: str, name: str) -> str:
            value = require(sec, key, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{name}.{key} must be a non-empty string")
            return value

        owner_sec = section("owner")
        signal_sec = section("signal")
        agent_sec = raw.get("agent", {}) or {}
        endpoints = section("endpoints")

        def endpoint(key: str) -> EndpointConfig:
            sec = endpoints.get(key)
            if not isinstance(sec, Mapping):
                raise ConfigError(f"missing endpoints.{key}")
            return EndpointConfig(
                base_url=require(sec, "base_url", f"endpoints.{key}"),
                timeout_seconds=float(sec.get("timeout_seconds", 10.0)),
            )

        ntfy_sec = endpoints.get("ntfy")
        if not isinstance(ntfy_sec, Mapping):
            raise ConfigError("missing endpoints.ntfy")

        return cls(
            owner=OwnerConfig(id=require_nonempty(owner_sec, "id", "owner")),
            agent=AgentConfig(
                model=agent_sec.get("model", AgentConfig.model),
                idle_timeout_seconds=int(
                    agent_sec.get("idle_timeout_seconds", AgentConfig.idle_timeout_seconds)
                ),
                approval_timeout_seconds=int(
                    agent_sec.get(
                        "approval_timeout_seconds", AgentConfig.approval_timeout_seconds
                    )
                ),
                system_prompt=agent_sec.get("system_prompt", AgentConfig.system_prompt),
            ),
            signal=SignalConfig(
                bridge_url=require_nonempty(signal_sec, "bridge_url", "signal"),
                account=require_nonempty(signal_sec, "account", "signal"),
                safe_length=int(signal_sec.get("safe_length", 2000)),
            ),
            gatus=endpoint("gatus"),
            prometheus=endpoint("prometheus"),
            taiga=endpoint("taiga"),
            todo=endpoint("todo"),
            ntfy=NtfyConfig(
                base_url=require(ntfy_sec, "base_url", "endpoints.ntfy"),
                topic=require(ntfy_sec, "topic", "endpoints.ntfy"),
                timeout_seconds=float(ntfy_sec.get("timeout_seconds", 10.0)),
            ),
            secrets=Secrets.from_env(env),
        )
