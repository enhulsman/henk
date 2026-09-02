"""Contract tests for the workstation session publisher's classification half.

Covers ``session-awareness`` tasks 3.1–3.11: the closed-schema config loader, the
unsafe-root refusal set, canonicalisation, the root gate, the owner gate, both-path
classification, and the ``--dry-run`` classification table. Fields, aggregate,
snapshot, budget (§4) and publish policy, transport, units (§5) are owned by the
later groups; the stubs they extend are asserted to exist and to be unimplemented.

Standing rule 1 of the change applies to this file: every path, label, and owner
below is a placeholder. Nothing here is read from the live estate, and no test in
this module invokes ``herdr``, ``claude-estate``, or reads anything under
``~/.claude``.

Standing rule 3 applies too: ``test_source_never_mentions_transcript_tools`` greps
the publisher source for ``cclog`` and ``herdr agent explain``, and the
``transcripts`` fixture records every ``open`` under a fake transcript directory so
a classification run can be asserted to have opened none of it.

Exit codes, "no HTTP request", and "no state write" are deliberately NOT asserted
here: the loader raises ``ConfigError`` and the estate parser raises
``EstateError``, and it is §5's ``main`` that maps those onto a non-zero exit with
no request and no state write. These tests pin the raise and the message; §5 pins
the exit.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "session-publisher"
    / "session_publisher.py"
)


def _load_publisher():
    """Import the publisher by path — its directory name has a hyphen, so it is
    not an importable package name."""
    spec = importlib.util.spec_from_file_location("session_publisher", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["session_publisher"] = module
    spec.loader.exec_module(module)
    return module


sp = _load_publisher()


# --------------------------------------------------------------------------- #
# Fixture estate (task 3.1)
# --------------------------------------------------------------------------- #

# Placeholder canonicalisation table: the two indirections the design names — a
# symlink under an allowed root that lands in the denied subtree, and a scratch
# worktree under /tmp whose canonical location is a denied checkout.
CANONICAL = {
    "/home/owner/Coding/link-to-work": "/home/owner/Coding/work/client-x/site",
    "/tmp/scratch-wt/feature": "/home/owner/Coding/work/client-x/other",
    "/home/owner/link-to-home": "/home/owner",
    "/home/owner/link-to-tempdir": "/scratch/tmp",
}

ENV = {"HOME": "/home/owner"}
TEMPDIR = "/scratch/tmp"

# Placeholder origin table, keyed by CANONICAL path.
GIT_TABLE = {
    "/home/owner/Coding/henk": (0, "git@github.com:owner-a/henk.git\n"),
    "/home/owner/Coding/thirdparty-tool": (
        0,
        "https://github.com/third-party/thirdparty-tool.git\n",
    ),
    "/home/owner/Coding/no-origin": (2, ""),
    "/home/owner/Coding/work/client-x/site": (0, "git@github.com:client-org/site.git\n"),
    "/home/owner/Coding/work/client-x/other": (0, "git@github.com:client-org/o.git\n"),
    # Not a git work tree at all: rc 128.
    "/home/owner/Documents/homelab-docs-site": (128, ""),
    "/home/owner/Downloads/scratch": (128, ""),
}

BASE_CONFIG = """
allow_owners = ["owner-a"]
deny_roots = ["/home/owner/Coding/work"]

[[allow_roots]]
path = "/home/owner/Coding"
label = "coding"

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "henk"

[[allow_roots]]
path = "/home/owner/Documents/homelab-docs-site"
label = "homelab-docs"
"""


def fake_realpath(path: str) -> str:
    return CANONICAL.get(path, os.path.normpath(path))


class FakeGit:
    """A ``GitRunner`` double. Records every path it was asked about so a test can
    assert the owner gate was not consulted for a root-denied path (one git
    subprocess per denied pane per tick is exactly the cost the gate order avoids)."""

    def __init__(self, table=None, timeout_paths=(), raises=None) -> None:
        self.table = dict(GIT_TABLE if table is None else table)
        self.timeout_paths = set(timeout_paths)
        self.raises = raises
        self.calls: list[str] = []

    def origin_url(self, path: str) -> tuple[int, str]:
        self.calls.append(path)
        if path in self.timeout_paths:
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=5.0)
        if self.raises is not None:
            raise self.raises
        return self.table.get(path, (128, "fatal: not a git repository"))


def agent(
    pane: str,
    cwd: str,
    status: str = "working",
    foreground_cwd: str | None = None,
    **extra,
):
    """One herdr agent record. The keys that must never be consumed are present on
    every fixture record on purpose — a mutation that starts reading one of them
    would find real-looking values here, and the forbidden-fields test in §4 is
    what catches it."""
    record = {
        "agent": "claude",
        "agent_session": {
            "agent": "claude",
            "kind": "id",
            "source": "osc",
            "value": "11111111-2222-3333-4444-555555555555",
        },
        "agent_status": status,
        "cwd": cwd,
        "focused": False,
        "foreground_cwd": cwd if foreground_cwd is None else foreground_cwd,
        "pane_id": pane,
        "revision": 42,
        "state_change_seq": 7,
        "tab_id": "wA:t1",
        "terminal_id": "term-1",
        "terminal_title": "placeholder title",
        "terminal_title_stripped": "placeholder title",
        "workspace_id": "wA",
    }
    record.update(extra)
    return record


#: The fixture estate, one pane per case the design names.
ESTATE = [
    # Both gates admit; nested allowed root supplies the label.
    agent("wA:p1", "/home/owner/Coding/henk", "working"),
    # Root admits (container root "coding"), owner gate refuses a third-party origin.
    agent("wB:p2", "/home/owner/Coding/thirdparty-tool", "idle"),
    # Denied subtree below an allowed root.
    agent("wC:p3", "/home/owner/Coding/work/client-x/site", "blocked"),
    # Symlink under an allowed root resolving into the denied subtree.
    agent("wD:p4", "/home/owner/Coding/link-to-work", "working"),
    # Scratch worktree under /tmp whose canonical path is a denied checkout.
    agent("wE:p5", "/tmp/scratch-wt/feature", "idle"),
    # Non-git directory under an allowed root.
    agent("wF:p6", "/home/owner/Documents/homelab-docs-site", "done"),
    # Checkout with no origin.
    agent("wG:p7", "/home/owner/Coding/no-origin", "working"),
    # cwd admitted, foreground_cwd different and denied.
    agent(
        "wH:p8",
        "/home/owner/Coding/henk",
        "working",
        foreground_cwd="/home/owner/Coding/work/client-x/site",
    ),
    # Two reported paths under two different allowed roots, both admitted.
    agent(
        "wI:p9",
        "/home/owner/Coding/henk",
        "idle",
        foreground_cwd="/home/owner/Documents/homelab-docs-site",
    ),
    # Outside every allowed root.
    agent("wJ:p10", "/home/owner/Downloads/scratch", "unknown"),
]


def herdr_envelope(agents=None) -> str:
    """The exact envelope shape probe 1.1 recorded: agents live at
    ``result.agents``, not at the top level."""
    import json

    return json.dumps(
        {
            "id": "01JPLACEHOLDER",
            "result": {"type": "agent_list", "agents": list(ESTATE if agents is None else agents)},
        }
    )


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    """A fake transcript directory whose every ``open`` is recorded (standing rule 3)."""
    directory = tmp_path / "transcripts"
    directory.mkdir()
    (directory / "session.jsonl").write_text('{"type": "user"}\n', encoding="utf-8")
    opened: list[str] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        try:
            name = os.fspath(file)
        except TypeError:
            name = ""
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if isinstance(name, str) and name.startswith(str(directory)):
            opened.append(name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    return SimpleNamespace(directory=directory, opened=opened)


def write_config(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str, *, env=None, tempdir: str = TEMPDIR):
    return sp.load_config(
        write_config(tmp_path, text),
        env=ENV if env is None else env,
        realpath=fake_realpath,
        tempdir=tempdir,
    )


def classify_all(config, git=None, agents=None):
    git = FakeGit() if git is None else git
    return [
        sp.classify(record, config, git, fake_realpath)
        for record in (ESTATE if agents is None else agents)
    ], git


def by_pane(classifications):
    return {item.pane: item for item in classifications}


# --------------------------------------------------------------------------- #
# Module shape and standing rules
# --------------------------------------------------------------------------- #


def test_publisher_script_exists_and_is_executable() -> None:
    assert MODULE_PATH.is_file()
    assert os.access(MODULE_PATH, os.X_OK), "the unit ExecStarts the script directly"
    assert MODULE_PATH.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


def test_source_never_mentions_transcript_tools() -> None:
    """Standing rule 3: neither transcript reader may appear in the publisher at all."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "cclog" not in source
    assert "herdr agent explain" not in source
    assert "agent explain" not in source


def test_publisher_imports_are_stdlib_only() -> None:
    """The workstation runs this with the system interpreter and no venv (D14)."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    non_stdlib = {name for name in roots if name not in sys.stdlib_module_names}
    assert non_stdlib == set()


def test_python_version_guard_names_the_requirement() -> None:
    """The guard §5 tests through the entry point; pinned here because it is the
    one thing in the module that must run before ``tomllib`` is imported."""
    assert sp.MINIMUM_PYTHON == (3, 11)
    stream = SimpleNamespace(written=[], write=lambda text: stream.written.append(text))
    with pytest.raises(SystemExit) as excinfo:
        sp.require_python((3, 10, 12), stream=stream)
    assert excinfo.value.code == 2
    assert "3.11" in "".join(stream.written)
    # The live interpreter passes, so importing the module cannot have exited.
    sp.require_python(stream=stream)


def test_later_group_entry_points_are_declared_but_unimplemented() -> None:
    """Groups 4 and 5 extend this module; the seams they attach to exist now so
    neither has to restructure it."""
    for name in ("build_snapshot", "degrade", "comparison_key", "publish", "main"):
        assert hasattr(sp, name), name
        assert callable(getattr(sp, name))
        assert (getattr(sp, name).__doc__ or "").strip(), f"{name} needs its contract"


# --------------------------------------------------------------------------- #
# Task 3.2 — unsafe allow roots are refused at load
# --------------------------------------------------------------------------- #

UNSAFE_ROOTS = [
    "/",
    "/home/owner",  # $HOME, from the injected env
    "/home",
    "/root",
    "/mnt",
    "/mnt/c",  # an immediate child of /mnt: the WSL drive mount
    "/mnt/wsl",
    "/tmp",
    TEMPDIR,  # the injected system temporary root
    "/var",
    "/etc",
    "/usr",
    "/opt",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/media",
    "/srv",
]


@pytest.mark.parametrize("root", UNSAFE_ROOTS)
def test_unsafe_allow_root_is_refused_naming_the_entry(tmp_path, root) -> None:
    text = f"""
allow_owners = ["owner-a"]

[[allow_roots]]
path = "{root}"
label = "too-broad"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    message = str(excinfo.value)
    assert root in message, message
    assert "too-broad" in message, message


def test_home_reached_through_a_symlink_is_refused(tmp_path) -> None:
    """The set is applied to the canonical path, so a symlink to $HOME is the same
    entry as $HOME."""
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/link-to-home"
label = "sneaky"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "sneaky" in str(excinfo.value)


def test_tempdir_reached_through_a_symlink_is_refused(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/link-to-tempdir"
label = "sneaky-tmp"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "sneaky-tmp" in str(excinfo.value)


def test_a_deep_child_of_an_unsafe_root_is_fine(tmp_path) -> None:
    """`/mnt` and its immediate children are refused; `/mnt/c/Users/owner/x` is a
    real directory an owner could legitimately allow, and must load."""
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/mnt/c/Users/owner/projects"
label = "windows-side"
"""
    config = load(tmp_path, text)
    assert [entry.label for entry in config.allow_roots] == ["windows-side"]


def test_home_itself_is_not_confused_with_a_project_under_it(tmp_path) -> None:
    config = load(tmp_path, BASE_CONFIG)
    assert {entry.label for entry in config.allow_roots} == {
        "coding",
        "henk",
        "homelab-docs",
    }


# --------------------------------------------------------------------------- #
# Task 3.3 — the closed schema
# --------------------------------------------------------------------------- #


def test_unknown_top_level_key_is_refused_by_name(tmp_path) -> None:
    """A misspelled `deny_root` would otherwise load with the only deny rule
    silently absent."""
    text = """
allow_owners = ["owner-a"]
deny_root = ["/home/owner/Coding/work"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "henk"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "deny_root" in str(excinfo.value)


def test_unknown_entry_key_fields_is_refused_naming_key_and_entry(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "henk"
fields = ["pane", "title"]
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    message = str(excinfo.value)
    assert "fields" in message, message
    assert "henk" in message, message


def test_missing_label_is_refused_naming_the_entry(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    message = str(excinfo.value)
    assert "label" in message
    assert "/home/owner/Coding/henk" in message


def test_duplicate_label_is_refused_naming_the_entry(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "henk"

[[allow_roots]]
path = "/home/owner/Documents/homelab-docs-site"
label = "henk"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    message = str(excinfo.value)
    assert "henk" in message
    assert "/home/owner/Documents/homelab-docs-site" in message


@pytest.mark.parametrize(
    "label",
    [
        "homelab docs",  # a space: the shape Henk refuses at render
        "",
        "a" * 33,
        "label/with/slash",
        "label,with,comma",
        "ünïcödé-ish!",
    ],
)
def test_malformed_label_is_refused(tmp_path, label) -> None:
    text = f"""
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "{label}"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "/home/owner/Coding/henk" in str(excinfo.value)


@pytest.mark.parametrize("label", ["henk", "homelab-docs", "a" * 32, "ns:sub.part-1"])
def test_well_shaped_label_loads(tmp_path, label) -> None:
    text = f"""
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "{label}"
"""
    assert load(tmp_path, text).allow_roots[0].label == label


def test_label_shape_matches_the_shape_henk_enforces_at_render() -> None:
    """The publisher's label rule exists so no legal config can produce a session
    Henk would classify as unusable."""
    assert sp.LABEL_PATTERN.pattern == r"^[\w:.-]{1,32}$"


@pytest.mark.parametrize(
    "text,needle",
    [
        ('allow_roots = "/home/owner/Coding"\n', "allow_roots"),
        ('allow_owners = "owner-a"\n', "allow_owners"),
        ('deny_roots = "/home/owner/Coding/work"\n', "deny_roots"),
        ("publish_unlisted = \"yes\"\n", "publish_unlisted"),
        ("heartbeat_seconds = \"900\"\n", "heartbeat_seconds"),
        ("tick_seconds = 1.5\n", "tick_seconds"),
        ("topic = 12\n", "topic"),
    ],
)
def test_wrong_type_at_top_level_is_refused_by_name(tmp_path, text, needle) -> None:
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert needle in str(excinfo.value)


def test_allow_root_entry_must_be_a_table(tmp_path) -> None:
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, 'allow_roots = ["/home/owner/Coding"]\n')
    assert "allow_roots" in str(excinfo.value)


def test_relative_allow_root_is_refused(tmp_path) -> None:
    text = """
[[allow_roots]]
path = "Coding/henk"
label = "henk"
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "Coding/henk" in str(excinfo.value)


def test_malformed_toml_is_refused_as_a_config_error(tmp_path) -> None:
    with pytest.raises(sp.ConfigError):
        load(tmp_path, "allow_owners = [\n")


def test_missing_config_file_is_refused_as_a_config_error(tmp_path) -> None:
    with pytest.raises(sp.ConfigError) as excinfo:
        sp.load_config(
            tmp_path / "absent.toml", env=ENV, realpath=fake_realpath, tempdir=TEMPDIR
        )
    assert "absent.toml" in str(excinfo.value)


def test_defaults_are_the_documented_ones(tmp_path) -> None:
    config = load(tmp_path, 'allow_owners = ["owner-a"]\n')
    assert config.publish_unlisted is False
    assert config.heartbeat_seconds == 900
    assert config.tick_seconds == 300
    assert config.topic == "henk-sessions"
    assert config.token_env == "HENK_SESSION_PUBLISHER_TOKEN"
    assert config.token_file.endswith("henk-session-publisher/ntfy-token")
    assert config.allow_roots == ()
    assert config.deny_roots == ()


def test_owners_are_a_frozenset_and_roots_are_canonical(tmp_path) -> None:
    text = """
allow_owners = ["owner-a", "owner-a", "owner-b"]
deny_roots = ["/home/owner/Coding/work/", "/home/owner/Coding/./work"]

[[allow_roots]]
path = "/home/owner/Coding/henk/"
label = "henk"
"""
    config = load(tmp_path, text)
    assert config.allow_owners == frozenset({"owner-a", "owner-b"})
    assert config.allow_roots[0].canonical_path == "/home/owner/Coding/henk"
    assert set(config.deny_roots) == {"/home/owner/Coding/work"}


# --------------------------------------------------------------------------- #
# Task 3.4 — a container allow root with a deny root beneath it
# --------------------------------------------------------------------------- #


def test_container_allow_root_with_deny_root_below_loads_and_classifies(tmp_path) -> None:
    """There is deliberately NO load-time refusal for this shape: it is exactly the
    configuration `deny_roots` exists to express."""
    config = load(tmp_path, BASE_CONFIG)
    assert "/home/owner/Coding/work" in config.deny_roots
    assert "/home/owner/Coding" in {entry.canonical_path for entry in config.allow_roots}

    results = by_pane(classify_all(config)[0])
    # Under the deny entry: refused.
    assert results["wC:p3"].admitted is False
    # Elsewhere under the container entry: admitted (its own nested root labels it).
    assert results["wA:p1"].admitted is True
    # Elsewhere under the container entry with no nested root of its own.
    extra = agent("wK:p11", "/home/owner/Coding/dotfiles", "idle")
    only = sp.classify(extra, config, FakeGit(), fake_realpath)
    assert (only.admitted, only.label) == (True, "coding")


# --------------------------------------------------------------------------- #
# Task 3.5 — empty allowlists
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    ['allow_owners = ["owner-a"]\n', 'allow_roots = []\nallow_owners = ["owner-a"]\n'],
)
def test_unset_or_empty_allow_roots_admits_nothing(tmp_path, text) -> None:
    config = load(tmp_path, text)
    results, git = classify_all(config)
    assert [item.pane for item in results if item.admitted] == []
    assert git.calls == [], "no git subprocess is worth spawning when no root can admit"
    for item in results:
        assert any(denial.gate == "root" for denial in item.denials)


def test_empty_allow_owners_admits_only_non_git_directories(tmp_path) -> None:
    text = """
allow_owners = []
deny_roots = ["/home/owner/Coding/work"]

[[allow_roots]]
path = "/home/owner/Coding"
label = "coding"

[[allow_roots]]
path = "/home/owner/Documents/homelab-docs-site"
label = "homelab-docs"
"""
    config = load(tmp_path, text)
    results, _ = classify_all(config)
    admitted = {item.pane for item in results if item.admitted}
    assert admitted == {"wF:p6"}  # the only non-git directory under an allowed root


def test_dry_run_says_only_non_git_can_pass_when_allow_owners_is_empty(tmp_path) -> None:
    text = """
allow_owners = []

[[allow_roots]]
path = "/home/owner/Coding"
label = "coding"
"""
    config = load(tmp_path, text)
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    assert "non-git directories can pass the owner gate" in rendered


def test_dry_run_omits_the_empty_owners_note_when_owners_are_configured(tmp_path) -> None:
    config = load(tmp_path, BASE_CONFIG)
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    assert "non-git directories can pass the owner gate" not in rendered


# --------------------------------------------------------------------------- #
# Task 3.6 — the two gates, scenario by scenario
# --------------------------------------------------------------------------- #


@pytest.fixture
def config(tmp_path):
    return load(tmp_path, BASE_CONFIG)


def test_both_gates_admit(config) -> None:
    result = by_pane(classify_all(config)[0])["wA:p1"]
    assert result.admitted is True
    assert result.denials == ()
    assert result.status == "working"


def test_root_admits_and_owner_refuses(config) -> None:
    result = by_pane(classify_all(config)[0])["wB:p2"]
    assert result.admitted is False
    assert [(d.reported_path_role, d.gate) for d in result.denials] == [("cwd", "owner")]


def test_dry_run_names_the_owner_gate_for_a_third_party_origin(config) -> None:
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    line = next(line for line in rendered.splitlines() if line.startswith("wB:p2"))
    assert "denied" in line
    assert "owner gate on cwd" in line


def test_symlink_into_a_denied_subtree_is_refused(config) -> None:
    result = by_pane(classify_all(config)[0])["wD:p4"]
    assert result.admitted is False
    assert [(d.gate, d.reason) for d in result.denials] == [("root", "deny_roots")]


def test_scratch_worktree_under_tmp_embedding_a_denied_checkout_is_refused(config) -> None:
    result = by_pane(classify_all(config)[0])["wE:p5"]
    assert result.admitted is False
    assert [(d.gate, d.reason) for d in result.denials] == [("root", "deny_roots")]


def test_deny_below_allow_wins_at_any_depth(config) -> None:
    for depth in range(1, 6):
        path = "/home/owner/Coding/work" + "/x" * depth
        record = agent("wZ:p0", path, "working")
        assert sp.classify(record, config, FakeGit(), fake_realpath).admitted is False


def test_deny_root_containment_is_segment_aware(config) -> None:
    """`/home/owner/Coding/workshop` is not under `/home/owner/Coding/work`."""
    record = agent("wZ:p0", "/home/owner/Coding/workshop", "working")
    result = sp.classify(record, config, FakeGit(), fake_realpath)
    assert result.admitted is True
    assert result.label == "coding"


def test_allow_root_containment_is_segment_aware(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/hen"
label = "hen"
"""
    config = load(tmp_path, text)
    record = agent("wZ:p0", "/home/owner/henk", "working")
    result = sp.classify(record, config, FakeGit(), fake_realpath)
    assert result.admitted is False
    assert [(d.gate, d.reason) for d in result.denials] == [("root", "allow_roots")]


def test_non_git_directory_under_an_allowed_root_is_admitted(config) -> None:
    result = by_pane(classify_all(config)[0])["wF:p6"]
    assert (result.admitted, result.label) == (True, "homelab-docs")


def test_checkout_with_no_origin_is_refused(config) -> None:
    result = by_pane(classify_all(config)[0])["wG:p7"]
    assert result.admitted is False
    assert result.denials[0].gate == "owner"
    assert "origin" in result.denials[0].reason


def test_path_outside_every_allowed_root_is_refused(config) -> None:
    result = by_pane(classify_all(config)[0])["wJ:p10"]
    assert result.admitted is False
    assert [(d.gate, d.reason) for d in result.denials] == [("root", "allow_roots")]


def test_the_owner_gate_is_not_consulted_for_a_root_denied_path(config) -> None:
    _, git = classify_all(config)
    for denied in ("/home/owner/Coding/work/client-x/site", "/home/owner/Downloads/scratch"):
        assert denied not in git.calls


def test_root_gate_decisions_directly(config) -> None:
    assert sp.root_gate("/home/owner/Coding/henk/sub", config) == sp.RootDecision(
        True, "henk", "root:allowed"
    )
    assert sp.root_gate("/home/owner/Coding/work/x", config).admitted is False
    assert sp.root_gate("/home/owner/Coding/work/x", config).reason == "deny_roots"
    assert sp.root_gate("/etc", config).reason == "allow_roots"


def test_owner_gate_decisions_directly(config) -> None:
    git = FakeGit()
    assert sp.owner_gate("/home/owner/Coding/henk", config, git).admitted is True
    not_git = sp.owner_gate("/home/owner/Documents/homelab-docs-site", config, git)
    assert (not_git.admitted, not_git.reason) == (True, "not-git")
    refused = sp.owner_gate("/home/owner/Coding/thirdparty-tool", config, git)
    assert (refused.admitted, refused.reason) == (False, "owner:not-allowed")
    no_origin = sp.owner_gate("/home/owner/Coding/no-origin", config, git)
    assert (no_origin.admitted, no_origin.reason) == (False, "owner:no-origin")


def test_owner_gate_treats_an_unparseable_origin_as_a_refusal(config) -> None:
    git = FakeGit(table={"/p": (0, "garbage\n")})
    decision = sp.owner_gate("/p", config, git)
    assert (decision.admitted, decision.reason) == (False, "owner:unparseable")


def test_owner_gate_treats_an_empty_origin_as_a_refusal(config) -> None:
    git = FakeGit(table={"/p": (0, "   \n")})
    decision = sp.owner_gate("/p", config, git)
    assert decision.admitted is False


def test_owner_gate_treats_an_unexpected_return_code_as_a_refusal(config) -> None:
    git = FakeGit(table={"/p": (129, "boom")})
    decision = sp.owner_gate("/p", config, git)
    assert decision.admitted is False
    assert "owner:" in decision.reason


def test_owner_gate_treats_a_timeout_as_a_refusal_not_a_crash(config) -> None:
    git = FakeGit(timeout_paths={"/home/owner/Coding/henk"})
    decision = sp.owner_gate("/home/owner/Coding/henk", config, git)
    assert (decision.admitted, decision.reason) == (False, "owner:timeout")


def test_a_git_timeout_denies_the_session_rather_than_failing_the_run(config) -> None:
    git = FakeGit(timeout_paths={"/home/owner/Coding/henk"})
    result = sp.classify(ESTATE[0], config, git, fake_realpath)
    assert result.admitted is False
    assert result.denials[0].reason == "owner:timeout"


def test_a_git_oserror_denies_the_session_rather_than_failing_the_run(config) -> None:
    git = FakeGit(raises=FileNotFoundError("git"))
    result = sp.classify(ESTATE[0], config, git, fake_realpath)
    assert result.admitted is False
    assert result.denials[0].gate == "owner"


# --------------------------------------------------------------------------- #
# Task 3.7 — both reported paths
# --------------------------------------------------------------------------- #


def test_denied_foreground_path_blocks_the_session(config) -> None:
    result = by_pane(classify_all(config)[0])["wH:p8"]
    assert result.admitted is False
    assert [(d.reported_path_role, d.gate) for d in result.denials] == [
        ("foreground_cwd", "root")
    ]


def test_dry_run_names_the_denying_gate_and_reported_path(config) -> None:
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    line = next(line for line in rendered.splitlines() if line.startswith("wH:p8"))
    assert "denied" in line
    assert "root gate on foreground_cwd" in line
    assert "cwd" in line  # foreground_cwd names it; the cwd gate is not blamed
    assert "owner gate" not in line


def test_two_paths_under_two_roots_takes_the_cwd_label(config) -> None:
    result = by_pane(classify_all(config)[0])["wI:p9"]
    assert (result.admitted, result.label) == (True, "henk")


def test_absent_foreground_cwd_is_classified_on_cwd_alone(config) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk")
    del record["foreground_cwd"]
    git = FakeGit()
    result = sp.classify(record, config, git, fake_realpath)
    assert result.admitted is True
    assert git.calls == ["/home/owner/Coding/henk"]


def test_null_foreground_cwd_is_classified_on_cwd_alone(config) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk", foreground_cwd=None)
    git = FakeGit()
    assert sp.classify(record, config, git, fake_realpath).admitted is True
    assert git.calls == ["/home/owner/Coding/henk"]


def test_equal_foreground_cwd_is_evaluated_once(config) -> None:
    git = FakeGit()
    result = sp.classify(ESTATE[0], config, git, fake_realpath)
    assert result.admitted is True
    assert git.calls == ["/home/owner/Coding/henk"], "one path, one git call"


def test_a_denied_cwd_and_a_denied_foreground_report_both(config) -> None:
    record = agent(
        "wZ:p0",
        "/home/owner/Downloads/scratch",
        foreground_cwd="/home/owner/Coding/work/client-x/site",
    )
    result = sp.classify(record, config, FakeGit(), fake_realpath)
    assert [(d.reported_path_role, d.reason) for d in result.denials] == [
        ("cwd", "allow_roots"),
        ("foreground_cwd", "deny_roots"),
    ]


def test_label_is_taken_from_cwd_even_when_foreground_is_a_different_root(config) -> None:
    record = agent(
        "wZ:p0",
        "/home/owner/Documents/homelab-docs-site",
        foreground_cwd="/home/owner/Coding/henk",
    )
    result = sp.classify(record, config, FakeGit(), fake_realpath)
    assert (result.admitted, result.label) == (True, "homelab-docs")


# --------------------------------------------------------------------------- #
# Task 3.8 — owner matching across remote URL forms
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner-a/repo",
        "https://github.com/owner-a/repo.git",
        "https://github.com/owner-a/repo/",
        "https://github.com/owner-a/repo.git/",
        "http://git.hulsman.dev/owner-a/repo.git",
        "git@github.com:owner-a/repo.git",
        "git@github.com:owner-a/repo",
        "ssh://git@github.com/owner-a/repo",
        "ssh://git@github.com/owner-a/repo.git",
        "ssh://git@github.com:2222/owner-a/repo.git",
        "git@github.com-work:owner-a/repo.git",  # host alias
        "git://github.com/owner-a/repo.git",
        "  https://github.com/owner-a/repo.git\n",
    ],
)
def test_owner_of_extracts_the_owner_segment(url) -> None:
    assert sp.owner_of(url) == "owner-a"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "garbage",
        "https://github.com",
        "https://github.com/",
        "https://github.com/onlyrepo",
        "git@github.com:repo.git",
        "not a url at all",
    ],
)
def test_owner_of_returns_none_for_an_unparseable_origin(url) -> None:
    assert sp.owner_of(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/third-party/henk.git",
        "git@github.com:third-party/henk.git",
        "ssh://git@github.com/third-party/henk",
        "git@github.com-work:third-party/henk.git",
    ],
)
def test_a_different_owner_with_the_same_repo_name_is_refused(config, url) -> None:
    git = FakeGit(table={"/p": (0, url + "\n")})
    assert sp.owner_gate("/p", config, git).admitted is False


def test_owner_matching_is_case_sensitive(config) -> None:
    git = FakeGit(table={"/p": (0, "git@github.com:Owner-A/repo.git\n")})
    assert sp.owner_gate("/p", config, git).admitted is False


# --------------------------------------------------------------------------- #
# Task 3.9 — canonicalisation
# --------------------------------------------------------------------------- #


def test_classification_uses_the_canonical_path_not_the_reported_one(config) -> None:
    """The reported path is under an allowed root; its canonical path is not."""
    record = agent("wZ:p0", "/home/owner/Coding/henk/../../Downloads/scratch")
    result = sp.classify(record, config, FakeGit(), fake_realpath)
    assert result.admitted is False
    assert [(d.gate, d.reason) for d in result.denials] == [("root", "allow_roots")]


def test_canonicalise_resolves_dot_segments_and_symlinks() -> None:
    assert sp.canonicalise("/home/owner/Coding/./henk/", fake_realpath) == (
        "/home/owner/Coding/henk"
    )
    assert sp.canonicalise("/home/owner/Coding/link-to-work", fake_realpath) == (
        "/home/owner/Coding/work/client-x/site"
    )


def test_canonicalisation_against_a_real_symlink(tmp_path, monkeypatch) -> None:
    """One test with the real ``os.path.realpath``: the fake table above is only
    trustworthy if the production canonicaliser behaves the same way on disk."""
    real_root = tmp_path / "roots" / "personal"
    denied = tmp_path / "roots" / "personal" / "work"
    (denied / "client").mkdir(parents=True)
    link = tmp_path / "roots" / "personal" / "shortcut"
    link.symlink_to(denied / "client", target_is_directory=True)

    config = sp.load_config(
        write_config(
            tmp_path,
            f"""
allow_owners = ["owner-a"]
deny_roots = ["{denied}"]

[[allow_roots]]
path = "{real_root}"
label = "personal"
""",
        ),
        env={"HOME": str(tmp_path / "home")},
        tempdir=str(tmp_path / "systmp"),
    )
    record = agent("wZ:p0", str(link))
    result = sp.classify(record, config, FakeGit(table={}), os.path.realpath)
    assert result.admitted is False
    assert result.denials[0].reason == "deny_roots"

    sibling = real_root / "notes"
    sibling.mkdir()
    ok = sp.classify(agent("wZ:p1", str(sibling)), config, FakeGit(table={}), os.path.realpath)
    assert (ok.admitted, ok.label) == (True, "personal")


# --------------------------------------------------------------------------- #
# Task 3.10 — longest prefix wins
# --------------------------------------------------------------------------- #


def test_longest_matching_root_admits_and_supplies_the_label(config) -> None:
    """`/home/owner/Coding` and `/home/owner/Coding/henk` both match; the nested
    entry labels the session."""
    assert sp.root_gate("/home/owner/Coding/henk", config).label == "henk"
    assert sp.root_gate("/home/owner/Coding/henk/deep/er", config).label == "henk"
    assert sp.root_gate("/home/owner/Coding/other", config).label == "coding"


def test_longest_prefix_is_independent_of_configuration_order(tmp_path) -> None:
    text = """
allow_owners = ["owner-a"]

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "henk"

[[allow_roots]]
path = "/home/owner/Coding"
label = "coding"
"""
    config = load(tmp_path, text)
    assert sp.root_gate("/home/owner/Coding/henk/x", config).label == "henk"


# --------------------------------------------------------------------------- #
# Task 3.11 — the clean git environment
# --------------------------------------------------------------------------- #


def test_git_command_argv_is_the_probed_invocation() -> None:
    argv, env = sp.git_command("/home/owner/Coding/henk")
    assert argv == [
        "git",
        "-C",
        "/home/owner/Coding/henk",
        "-c",
        "core.pager=cat",
        "remote",
        "get-url",
        "origin",
    ]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_git_command_environment_is_minimal() -> None:
    _, env = sp.git_command("/p")
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
    }
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env


def test_subprocess_git_passes_a_timeout_and_captures_output(monkeypatch) -> None:
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="git@github.com:owner-a/r.git\n", stderr="")

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    runner = sp.SubprocessGit(timeout_seconds=5.0)
    assert runner.origin_url("/p") == (0, "git@github.com:owner-a/r.git\n")
    assert recorded["timeout"] == 5.0
    assert recorded["capture_output"] is True
    assert recorded["text"] is True
    assert recorded["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert recorded["argv"][:3] == ["git", "-C", "/p"]


def test_subprocess_git_lets_a_timeout_reach_the_gate(monkeypatch) -> None:
    """``owner_gate`` owns the refusal; the runner does not swallow it into a
    success that would look like "not a git tree"."""

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        sp.SubprocessGit(timeout_seconds=0.1).origin_url("/p")


def test_real_subprocess_git_against_a_scratch_checkout(tmp_path) -> None:
    """One real-``git`` test. No commits: ``remote get-url`` needs none, and the
    workstation's global config signs commits and routes identity via ``includeIf``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:owner-a/repo.git"],
        check=True,
        env=env,
    )
    runner = sp.SubprocessGit(timeout_seconds=10.0)
    rc, out = runner.origin_url(str(repo))
    assert (rc, out.strip()) == (0, "git@github.com:owner-a/repo.git")
    assert sp.owner_of(out) == "owner-a"

    # A checkout with no origin: rc 2, per probe 1.3.
    bare = tmp_path / "no-origin"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(bare)], check=True, env=env)
    assert runner.origin_url(str(bare))[0] == 2

    # A plain directory: rc 128, which is the "not a git work tree" admit path.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert runner.origin_url(str(plain))[0] == 128


def test_real_git_on_a_checkout_configured_to_prompt_neither_prompts_nor_hangs(
    tmp_path,
) -> None:
    """An https origin that would demand credentials on a fetch: ``remote get-url``
    is local, and the clean environment forbids the prompt regardless."""
    repo = tmp_path / "prompting"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://git.hulsman.dev/owner-a/private.git",
        ],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "credential.helper", ""], check=True, env=env
    )
    rc, out = sp.SubprocessGit(timeout_seconds=10.0).origin_url(str(repo))
    assert rc == 0
    assert sp.owner_of(out) == "owner-a"


# --------------------------------------------------------------------------- #
# The estate parser and record validation
# --------------------------------------------------------------------------- #


def test_parse_herdr_reads_the_probed_envelope() -> None:
    agents = sp.parse_herdr(herdr_envelope())
    assert [record["pane_id"] for record in agents] == [r["pane_id"] for r in ESTATE]


def test_parse_herdr_tolerates_a_trailing_newline() -> None:
    assert len(sp.parse_herdr(herdr_envelope() + "\n")) == len(ESTATE)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "not json",
        "[]",
        '{"id": "x"}',
        '{"id": "x", "result": {}}',
        '{"id": "x", "result": {"type": "something_else", "agents": []}}',
        '{"id": "x", "result": {"type": "agent_list", "agents": {}}}',
        '{"id": "x", "result": {"type": "agent_list", "agents": ["not-a-record"]}}',
        '{"id": "x", "result": "agent_list"}',
    ],
)
def test_parse_herdr_refuses_anything_but_an_agent_list(text) -> None:
    with pytest.raises(sp.EstateError):
        sp.parse_herdr(text)


def test_parse_herdr_accepts_an_empty_estate() -> None:
    assert sp.parse_herdr('{"id": "x", "result": {"type": "agent_list", "agents": []}}') == []


@pytest.mark.parametrize("field", ["pane_id", "agent_status", "cwd"])
def test_a_record_missing_a_required_field_raises_naming_the_field(config, field) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk")
    del record[field]
    with pytest.raises(sp.EstateError) as excinfo:
        sp.classify(record, config, FakeGit(), fake_realpath)
    assert field in str(excinfo.value)


@pytest.mark.parametrize("field", ["pane_id", "agent_status", "cwd"])
@pytest.mark.parametrize("value", [None, 12, ["/home/owner/Coding/henk"], ""])
def test_a_record_with_a_non_string_required_field_raises(config, field, value) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk")
    record[field] = value
    with pytest.raises(sp.EstateError) as excinfo:
        sp.classify(record, config, FakeGit(), fake_realpath)
    assert field in str(excinfo.value)


def test_a_non_string_foreground_cwd_raises_naming_the_field(config) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk", foreground_cwd=17)
    with pytest.raises(sp.EstateError) as excinfo:
        sp.classify(record, config, FakeGit(), fake_realpath)
    assert "foreground_cwd" in str(excinfo.value)


def test_status_is_herdrs_value_verbatim(config) -> None:
    for status in ("idle", "done", "working", "blocked", "unknown", "something-new"):
        record = agent("wZ:p0", "/home/owner/Coding/henk", status)
        assert sp.classify(record, config, FakeGit(), fake_realpath).status == status


# --------------------------------------------------------------------------- #
# The dry-run table
# --------------------------------------------------------------------------- #


def test_dry_run_renders_one_line_per_pane(config) -> None:
    classifications = classify_all(config)[0]
    lines = [
        line
        for line in sp.render_dry_run(classifications, config).splitlines()
        if line and not line.startswith(" ") and ":" in line.split()[0]
    ]
    assert [line.split()[0] for line in lines] == [item.pane for item in classifications]


def test_dry_run_shows_the_project_label_for_admitted_panes(config) -> None:
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    line = next(line for line in rendered.splitlines() if line.startswith("wA:p1"))
    assert "admitted" in line
    assert "henk" in line


def test_dry_run_names_the_denying_gate_for_every_denied_pane(config) -> None:
    classifications = classify_all(config)[0]
    rendered = sp.render_dry_run(classifications, config)
    for item in classifications:
        line = next(line for line in rendered.splitlines() if line.startswith(item.pane + " "))
        if item.admitted:
            assert "denied" not in line
        else:
            assert "denied" in line
            assert any(f"{d.gate} gate on {d.reported_path_role}" in line for d in item.denials)


def test_dry_run_of_an_empty_estate_is_still_a_string(config) -> None:
    assert isinstance(sp.render_dry_run([], config), str)


def test_dry_run_carries_no_filesystem_path(config) -> None:
    """The dry-run table is what task 8.5 pastes into a review note under standing
    rule 1, so it may not carry a working directory."""
    rendered = sp.render_dry_run(classify_all(config)[0], config)
    for fragment in (
        "/home/owner",
        "/tmp/scratch-wt",
        "Documents",
        "thirdparty-tool",
        "client-x",
    ):
        assert fragment not in rendered, fragment


# --------------------------------------------------------------------------- #
# Standing rule 3 — nothing under the transcript directory is opened
# --------------------------------------------------------------------------- #


def test_classification_opens_no_transcript(tmp_path, transcripts) -> None:
    config = load(tmp_path, BASE_CONFIG)
    classify_all(config)
    sp.render_dry_run(classify_all(config)[0], config)
    assert transcripts.opened == []


def test_the_transcript_fixture_would_notice_an_open(transcripts) -> None:
    """The recorder is only evidence if it records; this is its self-check."""
    target = transcripts.directory / "session.jsonl"
    with open(target, encoding="utf-8") as handle:
        handle.read()
    assert transcripts.opened == [str(target)]
