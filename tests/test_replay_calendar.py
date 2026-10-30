"""maintenance-calendar group 7: replay with the calendar enabled.

``python -m henk.replay`` loads ``Config`` from its own environment
(``henk/replay/__main__.py:88-89``), so on rp5, with the calendar enabled, it decodes
the service-account key exactly like the live process does. Two requirements follow
(notes/apply-decisions.md, "Replay CLI"):

- the key never reaches the replay's SDK subprocess: ``main`` removes
  ``GOOGLE_CALENDAR_SA_KEY_B64`` from ``os.environ`` once the configuration is read,
  the way ``build_runtime`` does, and before any command runs;
- a replay still works: the definition registry holds both calendar tools as
  stubs, their definitions hash like the live ones, and no request leaves.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from henk.config import Config
from henk.replay import __main__ as cli
from henk.replay.recorder import system_prompt_hash, tool_definitions_hash
from henk.tools.calendar import CALENDAR_TOOL_NAMES
from tests.calendar_fixtures import CALENDAR_ID, ENV_VAR, key_b64
from tests.replay_fakes import (
    OTHER_MODEL,
    T,
    SessionMaker,
    forbidden_session_maker,
    live_recording,
    sample_raw,
    write_recording,
)


def _calendar_config(tmp_path: Path) -> Config:
    raw = sample_raw(tmp_path)
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    raw["calendar"]["enabled"] = True
    raw["calendar"]["calendar_id"] = CALENDAR_ID
    config = Config.from_dict(raw, env={ENV_VAR: key_b64()})
    Path(config.audit.path).write_text('{"record_type":"session"}\n')
    return config


def _hashes(config: Config) -> dict:
    import httpx

    from henk.replay import harness

    client = httpx.AsyncClient(transport=harness.RefusingTransport())
    registry = harness.build_definition_registry(config, client)
    assert set(CALENDAR_TOOL_NAMES) <= set(registry.names())
    return {"system_prompt": system_prompt_hash(config.agent.system_prompt),
            "tool_definitions": tool_definitions_hash(registry.tools())}


def _main(argv, *, load_config, create_session=forbidden_session_maker):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, load_config=load_config, create_session=create_session,
                    clock=lambda: float(T + 3600), stdout=out, stderr=err,
                    environ={"CLAUDE_CONFIG_DIR": "/tmp/henk-replay"})
    return code, out.getvalue(), err.getvalue()


class EnvSpyingMaker(SessionMaker):
    """The ``create_session`` seam: notes whether the key is in the environment
    when the replay's SDK session is created."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.key_present: list[bool] = []

    def __call__(self, factory):
        self.key_present.append(ENV_VAR in os.environ)
        return super().__call__(factory)


def test_a_replay_with_the_calendar_enabled_never_hands_on_the_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(ENV_VAR, key_b64())
    config = _calendar_config(tmp_path)
    rid = write_recording(config, live_recording([], hashes=_hashes(config)))
    maker = EnvSpyingMaker()
    code, out, err = _main(
        ["run", rid, "--model", OTHER_MODEL, "--effort", "low"],
        load_config=lambda: config, create_session=maker,
    )
    assert code == 0, err
    assert maker.key_present == [False]
    assert ENV_VAR not in os.environ
    # It still works: the stubs carry the calendar tools' current definitions.
    [factory] = maker.factories
    assert set(CALENDAR_TOOL_NAMES) <= set(factory.registry.names())
    assert "DRIFT" not in out


def test_every_command_removes_the_key_once_the_config_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, key_b64())
    config = _calendar_config(tmp_path)
    code, _, err = _main(["list"], load_config=lambda: config)
    assert code == 0, err
    assert ENV_VAR not in os.environ


def test_the_key_is_still_there_when_the_config_is_read(tmp_path, monkeypatch):
    # The real loader decodes the key from os.environ, so it must be removed after
    # the read, never before.
    monkeypatch.setenv(ENV_VAR, key_b64())
    config = _calendar_config(tmp_path)
    present: list[bool] = []

    def load_config() -> Config:
        present.append(ENV_VAR in os.environ)
        return Config.from_dict(
            _raw_for(tmp_path), env=os.environ
        )

    code, _, err = _main(["list"], load_config=load_config)
    assert code == 0, err
    assert present == [True]
    assert ENV_VAR not in os.environ
    assert config.secrets.google_calendar_key is not None


def test_a_refused_command_removes_the_key_too(tmp_path, monkeypatch):
    # The audit-log guard refuses after the config is read; nothing may keep the
    # key around for a later step.
    monkeypatch.setenv(ENV_VAR, key_b64())
    config = _calendar_config(tmp_path)
    Path(config.audit.path).unlink()
    code, _, err = _main(["list"], load_config=lambda: config)
    assert code == cli.REFUSED, err
    assert ENV_VAR not in os.environ


def _raw_for(tmp_path: Path) -> dict:
    raw = sample_raw(tmp_path)
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    raw["calendar"]["enabled"] = True
    raw["calendar"]["calendar_id"] = CALENDAR_ID
    return raw
