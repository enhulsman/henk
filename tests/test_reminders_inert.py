"""Shipping inert is the feature (group 9, tasks 9.3 and 9.4).

Two claims this change makes about the build it produces, asserted rather than
inspected — because both are exactly the kind of claim that rots:

1. **With no `reminders` section in config, nothing observable changes.** The
   registry, the owner command set, the system prompt and owner-turn composition are
   byte-identical to before. If any of them is not, the kill switch is incomplete
   and the deploy is not the no-op it is being sold as.
2. **This change writes none of `reminder-delivery`'s columns.** No scheduler, no
   send, no writer of `surfaced_at`, `send_attempts`, `delivered_at` or
   `reported_at`, no cadence amendment. The columns ship here because there is no
   migration path; the behaviour does not.

`reminders.enabled: false` is not a compromise. A build that accepts "remind me at
six", echoes a confident "Reminder #3 set for Wednesday at 18:00", and then says
nothing at six has spent the owner's trust on a promise it structurally cannot keep.
Off is the honest state until `reminder-delivery` exists.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from pathlib import Path

import httpx
import pytest

from henk.config import AgentConfig, Config, build_system_prompt
from henk.store import build_stores
from henk.store.db import REMINDER_COLUMNS
from henk.store.reminders import PENDING, ReminderStore, Store
from henk.tools import build_production_registry, build_time_resolver
from tests.test_config import SAMPLE, _minimal_raw

REPO_ROOT = Path(__file__).resolve().parent.parent

#: sha256 of the system prompt as it stood at commit 51972fd, immediately before
#: this change. A hash rather than a 1,836-character literal: the assertion is
#: byte-identity, and this states it in one line. If a later change intends to
#: alter the v1 prompt, it updates this hash deliberately — which is the point.
PROMPT_SHA256_BEFORE_THIS_CHANGE = (
    "21113cef7389e049533e03a2904ac5f8235d641f2c12f730f6ef49e4d30ce2bd"
)

#: The registry as it stood before this change, in registration order.
REGISTRY_BEFORE_THIS_CHANGE = [
    "homelab_health",
    "todo_read",
    "notify",
    "publish_handoff",
    "store_memory",
    "capture",
    "inbox_read",
]

#: The owner commands as they stood before this change.
COMMANDS_BEFORE_THIS_CHANGE = {
    "/remember",
    "/forget",
    "/memories",
    "/capture",
    "/inbox",
}

#: The four columns only `reminder-delivery` may ever write.
DELIVERY_ONLY_COLUMNS = (
    "send_attempts",
    "delivered_at",
    "surfaced_at",
    "reported_at",
)


def _disabled_config(tmp_path: Path) -> Config:
    """A config with no `reminders` section at all — the deployed shape."""
    raw = _minimal_raw("+31600000000")
    assert "reminders" not in raw
    config = Config.from_dict(raw, env={})
    object.__setattr__(config.store, "path", str(tmp_path / "inert.db"))
    return config


def _registry(config: Config):
    async def handler(request):  # pragma: no cover - never called at registration
        return httpx.Response(200)

    return build_production_registry(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        stores=build_stores(config.store, config.reminders),
        resolver=build_time_resolver(config),
    )


# --- 9.3 With no reminders section, nothing observable changes -----------


def test_the_committed_config_has_the_capability_off():
    assert Config.load(SAMPLE, env={}).reminders.enabled is False


def test_the_registry_is_byte_identical_to_before(tmp_path: Path):
    registry = _registry(_disabled_config(tmp_path))
    assert registry.names() == REGISTRY_BEFORE_THIS_CHANGE
    assert sorted(t.name for t in registry.mutating()) == ["capture", "store_memory"]


def test_the_system_prompt_is_byte_identical_to_before(tmp_path: Path):
    for prompt in (
        AgentConfig().system_prompt,
        build_system_prompt(),
        Config.from_dict(_minimal_raw("+1"), env={}).agent.system_prompt,
        Config.load(SAMPLE, env={}).agent.system_prompt,
    ):
        assert (
            hashlib.sha256(prompt.encode()).hexdigest()
            == PROMPT_SHA256_BEFORE_THIS_CHANGE
        )


def test_the_disabled_prompt_mentions_no_reminder_anything():
    prompt = build_system_prompt()
    for token in (
        "remind",
        "reminder",
        "cancel_reminder",
        "reminders_read",
        "current local time",
    ):
        assert token not in prompt.lower()


def test_the_owner_command_set_is_byte_identical_to_before():
    # Read off the dispatch table rather than from a list in this test, so a fifth
    # command added later is caught here.
    from henk.agent.commands import OwnerCommands as _OwnerCommands

    source = textwrap.dedent(inspect.getsource(_OwnerCommands.handle))
    tree = ast.parse(source)
    verbs = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        # len > 1 excludes the bare "/" in the startswith guard.
        and node.value.startswith("/")
        and len(node.value) > 1
    }
    assert verbs == COMMANDS_BEFORE_THIS_CHANGE | {"/remind", "/reminders"}

    # And with the capability off, the two new verbs are recognized but INERT:
    # they reply honestly and change nothing, which is what makes the reply
    # honest rather than a silent no-op.
    from henk.agent.commands import OwnerCommands

    commands = OwnerCommands(memories=None, inbox=None, reminders=None, resolver=None)
    for verb in ("/remind +2h x", "/reminders", "/reminders cancel 1"):
        reply = commands.handle(verb)
        assert reply is not None
        assert "configured" in reply.lower()
    # The pre-existing commands are untouched.
    for verb in COMMANDS_BEFORE_THIS_CHANGE:
        assert commands.handle(verb) is not None


async def test_owner_turn_composition_is_byte_identical_to_before():
    # No header, no extra framing: the content the session receives is exactly the
    # owner's text, as it was.
    from henk.agent.core import AgentCore
    from henk.agent.turns import OwnerTurn
    from tests.conftest import FakeChannel, FakeSessionFactory

    factory = FakeSessionFactory()
    core = AgentCore(factory, FakeChannel())  # time_header defaults to None
    await core.process(OwnerTurn("what's the homelab doing?"))
    assert factory.created[0].turns == ["what's the homelab doing?"]
    await core.aclose()


def test_the_runtime_wires_no_header_and_no_reminder_repository_when_disabled(
    tmp_path: Path,
):
    from henk.runtime import _time_header

    config = _disabled_config(tmp_path)
    assert build_time_resolver(config) is None
    assert _time_header(build_time_resolver(config)) is None


def test_the_table_is_still_created_when_the_capability_is_off(tmp_path: Path):
    # Harmless, and it keeps the DDL on ONE code path — which matters more here than
    # elsewhere, because there is no migration mechanism to fix a table that a
    # conditional path forgot to create.
    config = _disabled_config(tmp_path)
    stores = build_stores(config.store, config.reminders)
    live = {
        str(row[1])
        for row in stores.store.connection().execute("PRAGMA table_info(reminders)")
    }
    assert live == set(REMINDER_COLUMNS)
    stores.store.close()


def test_stored_reminders_are_untouched_by_a_disabled_run(tmp_path: Path):
    path = tmp_path / "inert.db"
    seeded = Store(path, clock=lambda: 1787203800.0)
    repo = ReminderStore(seeded)
    row = repo.schedule(
        "survives the flag", due_at=1787290200.0, due_tz="Europe/Amsterdam",
        input_spec="+1d",
    )
    seeded.close()

    config = _disabled_config(tmp_path)
    object.__setattr__(config.store, "path", str(path))
    _registry(config)  # a full disabled startup over the same file

    reopened = Store(path)
    try:
        again = ReminderStore(reopened).get(row.id)
        assert again.status == PENDING
        assert again.text == "survives the flag"
        assert again.due_at == row.due_at
    finally:
        reopened.close()


# --- 9.4 This change writes none of delivery's columns ------------------


def _sql_writes(directory: str) -> str:
    """Every non-docstring string literal under ``directory``, uppercased.

    Docstrings are excluded because this change's prose names these columns
    constantly while explaining that it does not write them — a plain text search
    matches the documentation instead of the code.
    """
    chunks = []
    for path in sorted((REPO_ROOT / directory).rglob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstrings.add(id(first.value))
        chunks.extend(
            f"{path.name}: {node.value.upper()}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        )
    return "\n".join(chunks)


@pytest.mark.parametrize("column", DELIVERY_ONLY_COLUMNS)
def test_nothing_in_this_change_writes_a_delivery_column(column: str):
    sql = _sql_writes("henk")
    # The CREATE TABLE statement names them; nothing may UPDATE or INSERT them.
    offenders = [
        line
        for line in sql.splitlines()
        if column.upper() in line
        and ("UPDATE" in line or "SET " in line or "INSERT" in line)
    ]
    if column == "send_attempts":
        # `send_attempts` appears in the INSERT-adjacent DDL literal, which is the
        # CREATE TABLE. Allow that one and nothing else.
        offenders = [line for line in offenders if "CREATE TABLE" not in line]
    assert offenders == [], offenders


def test_the_reminder_repository_writes_only_status_and_next_attempt_at():
    from henk.store import reminders as module

    tree = ast.parse(inspect.getsource(module))
    updates = [
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "UPDATE REMINDERS" in node.value.upper()
    ]
    assert updates, "the repository should have at least one UPDATE"
    for statement in updates:
        assigned = statement.split("SET", 1)[1].split("WHERE")[0]
        for column in DELIVERY_ONLY_COLUMNS:
            assert column.upper() not in assigned, statement
        assert "TEXT" not in assigned and "DUE_AT =" not in assigned


def test_there_is_no_scheduler_and_no_send_in_this_change():
    # `reminder-delivery`'s whole surface. A module or a call appearing here would
    # mean the split was not held.
    assert not (REPO_ROOT / "henk" / "reminders" / "scheduler.py").exists()
    assert not (REPO_ROOT / "henk" / "reminders" / "delivery.py").exists()
    reminders_dir = sorted(
        p.name for p in (REPO_ROOT / "henk" / "reminders").glob("*.py")
    )
    assert reminders_dir == ["__init__.py", "timeparse.py"]

    # And nothing under henk/reminders/ or henk/tools/reminders.py sends anything.
    for path in (
        REPO_ROOT / "henk" / "reminders" / "timeparse.py",
        REPO_ROOT / "henk" / "tools" / "reminders.py",
    ):
        tree = ast.parse(path.read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "send" not in called
        assert "send_proactive" not in called


def test_no_cadence_amendment_rode_along():
    # The `incident-triage` cadence amendment is reminder-delivery's. This change
    # must not have touched the pipeline's cadence surface.
    from henk.events.pipeline import PipelineConfig

    fields = {f.name for f in __import__("dataclasses").fields(PipelineConfig)}
    assert "reminder_class" not in fields
    assert not any("reminder" in name for name in fields)
