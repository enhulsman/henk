"""Shared doubles for the replay tests (triage-quality group 11).

Standing rule 5: no test runs a real model. The replay builds a real
``SdkSessionFactory`` over its stub registry and its gate, and the tests hand it
a ``create_session`` that returns the real ``_SdkAgentSession`` over a
:class:`DrivingClient`. That client plays a scripted model against the factory's
own boundary, in the order the SDK applies it:

1. the factory's ``PreToolUse`` hook (``_build_pretooluse_hook``, the production
   builder, which needs no SDK import);
2. the ``can_use_tool`` decision, ``decide_tool_permission`` over the factory's
   registry and gate (``_build_can_use_tool`` only wraps its result in SDK types);
3. the tool handler as ``_adapt_tool`` shapes it: ``content`` on success,
   ``ERROR: <error>`` on failure.

The stream it yields uses the SDK block shapes the recording tests already pin
against the installed SDK (``tests/test_triage_recording.py``,
``tests/test_triage_ending.py``), so the real stats, ending and transcript
accumulators run over it.

Placeholders only (standing rule 1): ``host-a.example``, ``example-a.service``,
RFC 5737 addresses.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs

import httpx
import yaml

from henk.agent.permission import base_tool_name, decide_tool_permission
from henk.agent.sdk_session import _SdkAgentSession
from henk.agent.triage import compose_event_turn_content
from henk.agent.turns import EventTurn, EventTurnItem
from henk.config import Config
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.replay import capture
from henk.replay.recorder import build_recording, new_recording_id
from tests.test_config import SAMPLE
from tests.test_triage_ending import _assistant, _result
from tests.test_triage_recording import ToolResultBlock, ToolUseBlock, UserMessage
from tests.test_triage_ending import AssistantMessage

MODEL = "claude-opus-5-5"
OTHER_MODEL = "claude-fable-5-1"

#: The evaluation time of the synthetic capture (2026-09-23T06:29:58Z).
T = 1790144998
INTERVAL = (1790144990, 1790145026)
NOTIFIED = 1790144870

TITLE = "[FIRING:1] HenkSwapPressure henk (host-a.example)"
MESSAGE = (
    "Value: A=98.39\nLabels:\n - alertname = HenkSwapPressure\n"
    " - identity_scope = host\n - host = host-a.example\n - node = vps\n"
)

PROMETHEUS = "http://192.0.2.10:9090"


# --- Config -------------------------------------------------------------------


def sample_raw(tmp_path: Path, *, everything: bool = False) -> dict:
    """The committed sample config, with every path under ``tmp_path``.

    ``everything`` turns on every optional tool (reminders, docs, sessions), so a
    registry built from it holds every tool the production builder can register.
    """
    raw = yaml.safe_load(SAMPLE.read_text())
    audit = str(tmp_path / "henk-audit.jsonl")
    raw["audit"]["path"] = audit
    raw["events"]["audit_path"] = audit
    raw["store"]["path"] = str(tmp_path / "henk-store.db")
    if everything:
        raw["owner"]["timezone"] = "Europe/Amsterdam"
        raw["reminders"]["enabled"] = True
        raw["homelab_docs"]["enabled"] = True
        raw["homelab_docs"]["path"] = str(tmp_path / "docs")
        raw["sessions"]["enabled"] = True
    return raw


def make_config(tmp_path: Path, *, everything: bool = False, audit: bool = True,
                **overrides: Any) -> Config:
    raw = sample_raw(tmp_path, everything=everything)
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        raw.setdefault(section, {})[key] = value
    config = Config.from_dict(raw, env={})
    if audit:
        Path(config.audit.path).write_text('{"record_type":"session"}\n')
    return config


# --- The driving client ---------------------------------------------------------


class DrivingClient:
    """A ``ClaudeSDKClient`` double that plays ``script`` against the factory.

    Each script step is ``(sdk_tool_name, arguments)``; a bare registry name is
    given the ``mcp__henk__`` prefix, and a built-in name (``Bash``) is sent as
    is. ``raise_after`` raises that exception once the script has run.
    """

    def __init__(self, factory, script: Sequence[tuple[str, dict]], *,
                 reply: str = "Diagnosis: placeholder (confidence: moderate)\n"
                 "Fix: placeholder\nPickup: run henk-pickup",
                 raise_after: BaseException | None = None) -> None:
        self.factory = factory
        self.script = list(script)
        self.reply = reply
        self.raise_after = raise_after
        self.queries: list[str] = []
        self.results: list[tuple[str, str, bool | None]] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive_response(self):
        hook = self.factory._build_pretooluse_hook()
        model = self.factory.config.model
        for n, (name, arguments) in enumerate(self.script, start=1):
            sdk_name = name if name[:1].isupper() or name.startswith("mcp__") \
                else f"mcp__henk__{name}"
            tool_use_id = f"tu-{n}"
            yield AssistantMessage(
                content=[ToolUseBlock(tool_use_id, sdk_name, dict(arguments))],
                model=model,
            )
            text, is_error = await self._answer(hook, sdk_name, arguments, tool_use_id)
            self.results.append((sdk_name, text, is_error))
            yield UserMessage(content=[ToolResultBlock(
                tool_use_id, [{"type": "text", "text": text}], is_error)])
        if self.raise_after is not None:
            raise self.raise_after
        yield _assistant(self.reply, model=model)
        yield _result()

    async def _answer(self, hook, sdk_name: str, arguments: dict, tool_use_id: str):
        decision = await hook({"tool_name": sdk_name, "tool_input": arguments},
                              tool_use_id, None)
        if decision:
            return decision["hookSpecificOutput"]["permissionDecisionReason"], True
        permission = await decide_tool_permission(
            self.factory.registry, self.factory.gate, sdk_name, arguments
        )
        if not permission.allow:
            return permission.reason, True
        tool = self.factory.registry.get(base_tool_name(sdk_name))
        result = await tool.run(**arguments)
        # `_adapt_tool`'s shaping, exactly (sdk_session.py `_handler`).
        return (result.content if result.ok else f"ERROR: {result.error}"), None

    async def disconnect(self) -> None:
        self.connected = False


class SessionMaker:
    """The ``create_session`` seam: records every factory it was handed."""

    def __init__(self, script: Sequence[tuple[str, dict]] = (), **client_kw) -> None:
        self.script = list(script)
        self.client_kw = client_kw
        self.factories: list[Any] = []
        self.clients: list[DrivingClient] = []

    def __call__(self, factory):
        self.factories.append(factory)
        client = DrivingClient(factory, self.script, **self.client_kw)
        self.clients.append(client)
        return _SdkAgentSession(client, tool_classes={
            t.name: t.tool_class.value for t in factory.registry.tools()})


def forbidden_session_maker(factory):  # pragma: no cover - failing is the point
    raise AssertionError("a session was created: the replay would have spent")


# --- Recordings -------------------------------------------------------------------


def _turn() -> EventTurn:
    event = Event(id="evt-0001", title=TITLE, message=MESSAGE,
                  arrival_time=float(NOTIFIED + 60),
                  raw={"event": "message", "time": NOTIFIED})
    item = EventTurnItem(event=event, identity=derive_identity(event))
    return EventTurn(items=(item,), announceable=True, offset="off-1")


class _Ending:
    outcome = "completed"
    error_class = None
    http_status = None


class _Call:
    def __init__(self, name, arguments, result, is_error=None, tool_use_id=None):
        self.name = name
        self.arguments = arguments
        self.result = result
        self.is_error = is_error
        self.tool_use_id = tool_use_id


def live_recording(calls: Sequence[tuple], *, content: str | None = None,
                   hashes: dict | None = None, at: float = T + 30.0) -> dict:
    """A live (non-reconstructed) v1 recording holding ``calls``.

    Each call is ``(name, arguments, result[, is_error])``.
    """
    turn = _turn()
    record = build_recording(
        recording_id=new_recording_id(at),
        at=at,
        turn=turn,
        content=content if content is not None else compose_event_turn_content(turn),
        reply="Diagnosis: original (confidence: low)\nFix: x\nPickup: y",
        ending=_Ending(),
        fingerprint={"profile": "event", "model": MODEL, "effort": "high",
                     "thinking": "adaptive",
                     "system_prompt_sha256": (hashes or {}).get("system_prompt"),
                     "tool_definitions_sha256": (hashes or {}).get("tool_definitions")},
        transcript=[
            _Call(c[0], c[1], c[2], c[3] if len(c) > 3 else None, f"tu-{i}")
            for i, c in enumerate(calls, start=1)
        ],
    )
    return record


def reconstructed_recording(original_names: Sequence[str] = ("homelab_query",
                                                              "homelab_health",
                                                              "publish_handoff")) -> dict:
    """A reconstructed v1 recording: composed with no recall and no digest, the
    original call sequence as names with arguments ``unknown``, null hashes."""
    turn = _turn()
    record = build_recording(
        recording_id=new_recording_id(float(INTERVAL[1])),
        at=float(INTERVAL[1]),
        turn=turn,
        content=compose_event_turn_content(turn, recall=None, digest=None),
        reply="Diagnosis: the original candidate (confidence: moderate)",
        ending=_Ending(),
        fingerprint={"profile": "event", "model": None, "effort": None,
                     "thinking": None, "system_prompt_sha256": None,
                     "tool_definitions_sha256": None},
        transcript=[],
    )
    record["reconstructed"] = True
    record["transcript"] = [
        {"name": name, "arguments": "unknown", "result": None, "is_error": None,
         "tool_use_id": None}
        for name in original_names
    ]
    record["reference"] = {
        "branch": "fullness", "culprit": "example-a.service page-cache burst",
        "mechanism": "a package upgrade", "fix": "a boot-only drop-in",
    }
    return record


def write_recording(config: Config, record: dict) -> str:
    directory = config.audit.triage_recordings_dir
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / f"{record['recording_id']}.json").write_text(
        json.dumps(record, ensure_ascii=True, sort_keys=True, indent=1))
    return record["recording_id"]


# --- The synthetic capture --------------------------------------------------------


def _params(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


class FakePrometheus:
    """Answers every expression with a deterministic series, from the request's
    route and expression alone (never its `time`), so the live tool and the
    capture receive byte-identical bodies for the same question.

    Bare `up` reports one target up and one down, so the down-target error line
    of `scrape_targets` is exercised.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/targets":
            return httpx.Response(200, json={"status": "success",
                                             "data": {"activeTargets": []}})
        query = _params(request).get("query", "")
        ranged = request.url.path.endswith("query_range")
        job = "node-exporter-vps"
        for candidate in ("cadvisor-pi5", "cadvisor-vps", "node-exporter-pi5",
                          "node-exporter-vps", "node-exporter-pi2"):
            if f'job="{candidate}"' in query:
                job = candidate
        if query in ("up", "max_over_time(up[24h])"):
            series = [
                {"metric": {"job": "node-exporter-vps", "instance": "192.0.2.20:9100"},
                 "value": [T, "1"]},
                {"metric": {"job": "cadvisor-vps", "instance": "192.0.2.21:8080"},
                 "value": [T, "0"]},
            ]
            return httpx.Response(200, json={"status": "success", "data": {
                "resultType": "vector", "result": series}})
        metric = {"job": job, "name": "example-a", "state": "activating",
                  "id": "/system.slice/example-a.service", "type": "cpu-thermal"}
        if ranged:
            params = _params(request)
            end, step = int(params["end"]), int(params["step"])
            values = [[end - 2 * step, "40"], [end - step, "70"], [end, "98.39"]]
            result = [{"metric": metric, "values": values}]
            kind = "matrix"
        else:
            result = [{"metric": metric, "value": [T, "1"]}]
            kind = "vector"
        return httpx.Response(200, json={"status": "success",
                                         "data": {"resultType": kind, "result": result}})


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


CAPTURE_DIR_NAME = "2026-09-23-capture"


def write_capture(cases_dir: Path, *, t: int = T, max_points: int = 60,
                  handler: Callable | None = None) -> Path:
    """Run the real capture (`capture.run_capture`) against the fake Prometheus,
    into `<cases_dir>/2026-09-23-capture/<label>/`. Returns that directory."""
    root = private_dir(cases_dir / CAPTURE_DIR_NAME)
    capture.run_capture(
        httpx.Client(transport=httpx.MockTransport(handler or FakePrometheus())),
        prometheus_url=PROMETHEUS,
        evaluation_times=[t],
        max_points=max_points,
        out_dir=root,
    )
    return root / capture.time_label(t)


def capture_records(directory: Path) -> dict[Path, dict]:
    return {
        path: json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
        if path.name != capture.MANIFEST_NAME
    }


def edit_capture(directory: Path, match: Callable[[dict], bool],
                 change: Callable[[dict], None]) -> int:
    """Rewrite every captured record ``match`` selects; returns how many."""
    count = 0
    for path, record in capture_records(directory).items():
        if match(record):
            change(record)
            os.chmod(path, 0o600)
            path.write_text(json.dumps(record))
            count += 1
    return count


def write_case(config: Config, case_id: str, *, recording: dict | None = None,
               capture_dir: Path | None = None, t: int = T,
               interval: tuple[int, int] = INTERVAL, extra: dict | None = None) -> Path:
    """`triage-cases/<case_id>/case.json` in the layout `henk.replay.case` reads."""
    cases_dir = config.audit.triage_cases_dir
    directory = private_dir(cases_dir / case_id)
    case: dict[str, Any] = {
        "schema": "henk.triage-case.v1",
        "case_id": case_id,
        "recording": copy.deepcopy(recording or reconstructed_recording()),
    }
    if capture_dir is not None:
        case["capture"] = {
            "directory": str(capture_dir.relative_to(cases_dir)),
            "T": t,
            "interval": {"start": interval[0], "end": interval[1]},
        }
    case.update(extra or {})
    path = directory / "case.json"
    path.write_text(json.dumps(case, ensure_ascii=True, sort_keys=True, indent=1))
    return path
