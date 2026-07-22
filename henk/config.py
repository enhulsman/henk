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
        "Your complete toolset is exactly these four — you have no other tools "
        "or capabilities (no scheduling, cron, workflows, web, files, or "
        "shell):\n"
        "- homelab_health — report homelab health and status.\n"
        "- todo_read — read the owner's to-do list.\n"
        "- notify — send the owner a push notification via ntfy.\n"
        "- publish_handoff — publish a triage handoff document to the owner's "
        "handoffs topic.\n\n"
        "Use them to give real, current answers — when a request maps to a "
        "tool, call it. If something falls outside these four, say so plainly; "
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
class EventsConfig:
    """Event-intake settings (henk-events v1.2). Absent/``enabled: false`` → v1.

    ``enabled`` is the rollback flag (design migration step 5): when false the
    subscriber never starts and Henk behaves exactly as v1. Topics ride the
    single ntfy credential (read on events, write on handoffs); cadence values
    are the design D6 defaults, tunable without code changes.
    """

    enabled: bool = False
    events_topic: str = "henk-events"
    handoffs_topic: str = "henk-handoffs"
    audit_path: str = "/data/audit/henk-audit.jsonl"
    debounce_seconds: float = 120.0
    cooldown_seconds: float = 6 * 3600.0
    recurrence_window_seconds: float = 24 * 3600.0
    cap_per_24h: int = 3
    cooldown_overrides: tuple[Mapping[str, Any], ...] = ()


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
    events: EventsConfig = field(default_factory=EventsConfig)
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

        events_sec = raw.get("events", {}) or {}
        overrides = events_sec.get("cooldown_overrides", []) or []
        events = EventsConfig(
            enabled=bool(events_sec.get("enabled", EventsConfig.enabled)),
            events_topic=events_sec.get("events_topic", EventsConfig.events_topic),
            handoffs_topic=events_sec.get(
                "handoffs_topic", EventsConfig.handoffs_topic
            ),
            audit_path=events_sec.get("audit_path", EventsConfig.audit_path),
            debounce_seconds=float(
                events_sec.get("debounce_seconds", EventsConfig.debounce_seconds)
            ),
            cooldown_seconds=float(
                events_sec.get("cooldown_seconds", EventsConfig.cooldown_seconds)
            ),
            recurrence_window_seconds=float(
                events_sec.get(
                    "recurrence_window_seconds",
                    EventsConfig.recurrence_window_seconds,
                )
            ),
            cap_per_24h=int(events_sec.get("cap_per_24h", EventsConfig.cap_per_24h)),
            cooldown_overrides=tuple(dict(o) for o in overrides),
        )

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
            events=events,
            secrets=Secrets.from_env(env),
        )
