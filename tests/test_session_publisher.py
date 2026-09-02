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


# =========================================================================== #
# Task group 4 — fields, aggregate, snapshot, budget
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Fixture: the claude-estate side (probe 1.2)
# --------------------------------------------------------------------------- #

GENERATED_AT = "2026-09-02T10:40:00Z"

#: A placeholder hostname, present in no source the publisher reads, asserted
#: absent from the serialised bytes beside the real ``socket.gethostname()``.
FIXTURE_HOSTNAME = "placeholder-workstation"

#: A placeholder branch name. v1 never runs ``branch --show-current`` at all; the
#: forbidden-fields test asserts the shape of value is absent anyway, because the
#: deferred titles follow-up is the only thing that may ever add it.
FIXTURE_BRANCH = "feature/placeholder-branch"


def estate_row(pane: str, age_s: int = 0, **extra):
    """One ``claude-estate status --json`` row, carrying all eleven probed keys.

    Nine of them must never be read (probe 1.2), so every one of them carries a
    real-looking placeholder value here: a mutation that starts consuming ``cwd``,
    ``resume``, ``session``, ``title``, or ``class`` finds something to leak, and
    ``test_serialised_snapshot_carries_no_forbidden_value`` is what catches it.
    """
    row = {
        "age_s": age_s,
        "class": "stale",
        "cwd": f"/home/owner/Coding/estate-cwd-{pane.replace(':', '-')}",
        "kind": "claude",
        "pane_id": pane,
        "resume": f"claude --resume 11111111-2222-3333-4444-{pane.replace(':', '')}",
        "session": f"99999999-8888-7777-6666-{pane.replace(':', '')}",
        "status": "working",
        "tab_id": "wA:t1",
        "title": f"placeholder-estate-title-{pane.replace(':', '-')}",
        "workspace_id": "wA",
    }
    row.update(extra)
    return row


#: Ages for the fixture estate. ``wF:p6`` is deliberately absent: an admitted pane
#: that claude-estate does not know about must publish ``age_s: null`` while its
#: neighbours carry integers (task 4.5).
ESTATE_AGES = {
    "wA:p1": 42,
    "wB:p2": 91000,
    "wC:p3": 7,
    "wD:p4": 800,
    "wE:p5": 12,
    "wG:p7": 3600,
    "wH:p8": 5,
    "wI:p9": 91000,
    "wJ:p10": 0,
}


def estate_envelope(ages=None, rows=None) -> str:
    """The exact shape probe 1.2 recorded: ``{"agents": [rows], "summary": {...}}``."""
    import json as _json

    if rows is None:
        source = ESTATE_AGES if ages is None else ages
        rows = [estate_row(pane, age) for pane, age in source.items()]
    return _json.dumps(
        {"agents": list(rows), "summary": {"total": len(list(rows)), "fresh": 1}}
    )


class RecordingRow(dict):
    """A row that records every key lookup, so a test can prove the nine keys the
    publisher must never read were never touched."""

    def __init__(self, data) -> None:
        super().__init__(data)
        self.accessed: list[str] = []

    def __getitem__(self, key):
        self.accessed.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.accessed.append(key)
        return super().get(key, default)


#: Panes the fixture estate admits under ``BASE_CONFIG``, and the ones it denies.
ADMITTED_PANES = ("wA:p1", "wF:p6", "wI:p9")
DENIED_PANES = ("wB:p2", "wC:p3", "wD:p4", "wE:p5", "wG:p7", "wH:p8", "wJ:p10")


#: A configuration whose labels equal no component of any fixture path, so the
#: forbidden-fields test can assert every path component absent with no exception
#: carved out for a label that happens to look like a directory name (task 4.3).
DISTINCT_LABEL_CONFIG = """
allow_owners = ["owner-a"]
deny_roots = ["/home/owner/Coding/work"]

[[allow_roots]]
path = "/home/owner/Coding"
label = "alpha-one"

[[allow_roots]]
path = "/home/owner/Coding/henk"
label = "beta-two"

[[allow_roots]]
path = "/home/owner/Documents/homelab-docs-site"
label = "gamma-three"
"""


def snapshot_of(config, *, ages=None, estate_ok=True, agents=None, git=None):
    """Compose the whole publisher pipeline over the fixture estate."""
    return sp.snapshot_from_sources(
        herdr_envelope(agents),
        None if ages is None else estate_envelope(ages),
        estate_ok,
        config,
        FakeGit() if git is None else git,
        fake_realpath,
        generated_at=GENERATED_AT,
    )


@pytest.fixture
def distinct_config(tmp_path):
    return load(tmp_path, DISTINCT_LABEL_CONFIG)


# --------------------------------------------------------------------------- #
# Task 4.1 — exactly four keys, under every configuration the schema accepts
# --------------------------------------------------------------------------- #

FOUR_KEYS = ["pane", "project", "status", "age_s"]


#: Configurations the closed schema accepts, spanning the aggregate switch, an
#: extra deny root, a different cadence, and a different label set. The published
#: key list may not vary across any of them.
FIELD_SET_CONFIGS = [
    BASE_CONFIG,
    "publish_unlisted = true\n" + BASE_CONFIG,
    BASE_CONFIG.replace(
        'deny_roots = ["/home/owner/Coding/work"]',
        'deny_roots = ["/home/owner/Coding/work", "/home/owner/Downloads"]',
    ),
    "publish_unlisted = false\nheartbeat_seconds = 60\ntick_seconds = 30\n" + BASE_CONFIG,
    DISTINCT_LABEL_CONFIG,
]


@pytest.mark.parametrize("text", FIELD_SET_CONFIGS)
def test_every_admitted_session_carries_exactly_the_four_keys(tmp_path, text) -> None:
    """Task 4.1: the key list is a property of the code, not of the configuration —
    ``fields`` is refused at load (see
    ``test_unknown_entry_key_fields_is_refused_naming_key_and_entry``), so there is
    no configuration path to a fifth key."""
    config = load(tmp_path, text)
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["sessions"], "the fixture estate admits at least one session"
    for session in snapshot["sessions"]:
        assert list(session) == FOUR_KEYS, session


def test_the_four_keys_are_in_the_documented_order(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    for session in snapshot["sessions"]:
        assert list(session) == FOUR_KEYS


def test_a_fifth_key_has_no_configuration_route(tmp_path) -> None:
    """The refusal that makes task 4.1's claim total, restated at the boundary the
    field projection depends on."""
    text = BASE_CONFIG + """
[[allow_roots]]
path = "/home/owner/Coding/dotfiles"
label = "dotfiles"
fields = ["title"]
"""
    with pytest.raises(sp.ConfigError) as excinfo:
        load(tmp_path, text)
    assert "fields" in str(excinfo.value)


def test_admitted_sessions_are_exactly_the_admitted_classifications(config) -> None:
    snapshot, counts, classifications = snapshot_of(config, ages=ESTATE_AGES)
    assert {session["pane"] for session in snapshot["sessions"]} == set(ADMITTED_PANES)
    assert counts.admitted == len(ADMITTED_PANES)
    assert counts.denied == len(DENIED_PANES)
    assert len(classifications) == len(ESTATE)


def test_status_is_published_verbatim(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    published = {session["pane"]: session["status"] for session in snapshot["sessions"]}
    assert published == {"wA:p1": "working", "wF:p6": "done", "wI:p9": "idle"}


def test_an_unrecognised_status_is_still_published_verbatim(config) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk", "something-new")
    snapshot, _, _ = snapshot_of(config, ages={"wZ:p0": 1}, agents=[record])
    assert snapshot["sessions"] == [
        {"pane": "wZ:p0", "project": "henk", "status": "something-new", "age_s": 1}
    ]


# --------------------------------------------------------------------------- #
# Task 4.2 — `project` is the configured label
# --------------------------------------------------------------------------- #


def test_project_is_the_configured_label_not_the_cwd_basename(distinct_config) -> None:
    snapshot, _, _ = snapshot_of(distinct_config, ages=ESTATE_AGES)
    labels = {session["pane"]: session["project"] for session in snapshot["sessions"]}
    assert labels == {"wA:p1": "beta-two", "wF:p6": "gamma-three", "wI:p9": "beta-two"}
    for session in snapshot["sessions"]:
        assert session["project"] not in {"henk", "homelab-docs-site", "Coding"}


def test_the_label_of_a_deeply_nested_cwd_is_still_the_root_entry_label(
    distinct_config,
) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk/deep/nested/dir")
    snapshot, _, _ = snapshot_of(distinct_config, ages={"wZ:p0": 3}, agents=[record])
    assert snapshot["sessions"][0]["project"] == "beta-two"


def test_the_label_comes_from_the_cwd_root_when_foreground_sits_elsewhere(
    distinct_config,
) -> None:
    snapshot, _, _ = snapshot_of(distinct_config, ages=ESTATE_AGES)
    session = next(s for s in snapshot["sessions"] if s["pane"] == "wI:p9")
    assert session["project"] == "beta-two"  # cwd's root, not foreground_cwd's


# --------------------------------------------------------------------------- #
# Task 4.3 — the forbidden fields, asserted on the serialised bytes
# --------------------------------------------------------------------------- #


def _path_components(path: str) -> set[str]:
    return {part for part in path.split("/") if part}


def _fixture_path_components() -> set[str]:
    components: set[str] = set()
    for record in ESTATE:
        for key in ("cwd", "foreground_cwd"):
            value = record.get(key)
            if isinstance(value, str):
                components |= _path_components(value)
                components |= _path_components(fake_realpath(value))
    return components


#: herdr's `agent_status` is published verbatim, so a path component that is a
#: substring of a status value cannot be asserted absent from the body. The
#: exemption is derived, asserted to be exactly one component, and the *whole*
#: paths that component comes from are still asserted absent below.
PUBLISHED_STATUS_VALUES = ("idle", "done", "working", "blocked", "unknown")


def _status_shadowed_components() -> set[str]:
    return {
        component
        for component in _fixture_path_components()
        if any(component in status for status in PUBLISHED_STATUS_VALUES)
    }


def test_only_one_path_component_is_shadowed_by_a_published_status() -> None:
    """The exemption the forbidden-fields check carves out, pinned so it cannot
    silently widen: `work` is a substring of the status `working`."""
    assert _status_shadowed_components() == {"work"}


def test_fixture_labels_differ_from_every_path_component(distinct_config) -> None:
    """The strictness precondition of the forbidden-fields test: no label may equal
    a path component, or the check below would need an exception carved out for it
    and would stop proving anything about path components."""
    labels = {entry.label for entry in distinct_config.allow_roots}
    assert labels.isdisjoint(_fixture_path_components()), labels
    assert len(labels) == 3


def test_serialised_snapshot_carries_no_forbidden_value(distinct_config) -> None:
    """Task 4.3, asserted on the bytes rather than on the object: a leak that the
    object shape happens to hide (a value nested under a key, a stringified record)
    is still a leak."""
    import socket

    snapshot, _, _ = snapshot_of(distinct_config, ages=ESTATE_AGES)
    body = sp.serialise(snapshot).decode("utf-8")

    forbidden: set[str] = set()
    # Every reported and canonical path, and every one of their components.
    for record in ESTATE:
        for key in ("cwd", "foreground_cwd"):
            value = record.get(key)
            if isinstance(value, str):
                forbidden.add(value)
                forbidden.add(fake_realpath(value))
    forbidden |= _fixture_path_components() - _status_shadowed_components()
    # herdr's never-consumed fields.
    for record in ESTATE:
        forbidden.add(record["terminal_title_stripped"])
        forbidden.add(record["terminal_title"])
        forbidden.add(record["agent_session"]["value"])
        forbidden.add(record["tab_id"])
        forbidden.add(record["terminal_id"])
    # claude-estate's never-consumed fields.
    for pane, age in ESTATE_AGES.items():
        row = estate_row(pane, age)
        for key in ("resume", "session", "title", "class", "cwd"):
            forbidden.add(row[key])
    # Neither machine's identity, and no branch name.
    forbidden.add(socket.gethostname())
    forbidden.add(socket.gethostname().lower())
    forbidden.add(FIXTURE_HOSTNAME)
    forbidden.add(FIXTURE_BRANCH)

    for value in sorted(forbidden):
        assert value not in body, value

    # Two herdr keys are forbidden as keys, not only as values.
    for key in ("revision", "state_change_seq", "foreground_cwd", "agent_session"):
        assert key not in body, key


def test_the_workspace_id_appears_only_inside_a_published_pane_id(distinct_config) -> None:
    """The one value that cannot be asserted flatly absent, and why.

    Probe 1.1 recorded that a pane id is `<workspace segment>:<pane segment>` and
    that `workspace_id` is the bare `w` segment — so herdr's workspace id is
    structurally inside the addressing token D4 publishes on purpose. What must
    never appear is that id anywhere *else*, the tab id, or any human-authored
    workspace or tab label; this test pins the narrow shape of the exception
    instead of deleting the check.
    """
    import re as _re

    snapshot, _, _ = snapshot_of(distinct_config, ages=ESTATE_AGES)
    body = sp.serialise(snapshot).decode("utf-8")
    panes = {session["pane"] for session in snapshot["sessions"]}
    workspace_ids = {record["workspace_id"] for record in ESTATE}
    assert workspace_ids == {"wA"}
    for workspace_id in workspace_ids:
        found = [match.start() for match in _re.finditer(_re.escape(workspace_id), body)]
        assert found, "the fixture must actually contain the segment"
        for start in found:
            token = body[start : body.index('"', start)]
            assert token in panes, token
    for record in ESTATE:
        assert record["tab_id"] not in body


def test_forbidden_values_are_absent_with_the_aggregate_on_and_a_degraded_key(
    tmp_path,
) -> None:
    """The same assertion over the two optional keys, so neither can be the leak."""
    config = load(tmp_path, "publish_unlisted = true\n" + DISTINCT_LABEL_CONFIG)
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    snapshot["degraded"] = {"dropped": 2}
    body = sp.serialise(snapshot).decode("utf-8")
    for value in sorted(_fixture_path_components() - _status_shadowed_components()):
        assert value not in body, value
    assert '"unlisted"' in body


def test_the_forbidden_fields_check_would_notice_a_leak(distinct_config) -> None:
    """The check is only evidence if it bites: a session object carrying a cwd is
    caught by exactly the assertion above."""
    snapshot, _, _ = snapshot_of(distinct_config, ages=ESTATE_AGES)
    snapshot["sessions"][0]["cwd"] = "/home/owner/Coding/henk"
    body = sp.serialise(snapshot).decode("utf-8")
    leaked = [
        value
        for value in _fixture_path_components() - _status_shadowed_components()
        if value in body
    ]
    assert leaked, "the path-component check must catch a cwd in a session object"


# --------------------------------------------------------------------------- #
# Task 4.4 — the unlisted aggregate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("extra", ["", "publish_unlisted = false\n"])
def test_no_unlisted_key_when_the_aggregate_is_off(tmp_path, extra) -> None:
    config = load(tmp_path, extra + BASE_CONFIG)
    snapshot, counts, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert "unlisted" not in snapshot
    assert counts.denied == len(DENIED_PANES), "the count exists, it is just not published"


def test_the_aggregate_is_exactly_two_integers_when_opted_in(tmp_path) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["unlisted"] == {"count": 7, "blocked": 1}
    assert list(snapshot["unlisted"]) == ["count", "blocked"]
    assert len(snapshot["unlisted"]) == 2


def test_the_aggregate_says_nothing_else_about_denied_sessions(tmp_path) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    body = sp.serialise(snapshot).decode("utf-8")
    for pane in DENIED_PANES:
        assert pane not in body, pane


def test_the_aggregate_counts_only_blocked_among_the_denied(tmp_path) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    agents = [
        agent("wZ:p1", "/home/owner/Downloads/scratch", "blocked"),
        agent("wZ:p2", "/home/owner/Downloads/scratch", "blocked"),
        agent("wZ:p3", "/home/owner/Downloads/scratch", "idle"),
        agent("wZ:p4", "/home/owner/Coding/henk", "blocked"),  # admitted, not counted
    ]
    snapshot, counts, _ = snapshot_of(config, ages={}, agents=agents)
    assert snapshot["unlisted"] == {"count": 3, "blocked": 2}
    assert counts.admitted == 1


def test_the_aggregate_is_present_and_zeroed_when_nothing_was_denied(tmp_path) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    agents = [agent("wZ:p1", "/home/owner/Coding/henk", "working")]
    snapshot, _, _ = snapshot_of(config, ages={}, agents=agents)
    assert snapshot["unlisted"] == {"count": 0, "blocked": 0}


# --------------------------------------------------------------------------- #
# Task 4.5 — the age join
# --------------------------------------------------------------------------- #


def test_both_sources_yield_integer_ages_and_the_claude_estate_source(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["age_source"] == "claude-estate"
    ages = {session["pane"]: session["age_s"] for session in snapshot["sessions"]}
    assert ages["wA:p1"] == 42
    assert ages["wI:p9"] == 91000
    assert all(
        isinstance(value, int) for value in ages.values() if value is not None
    )


def test_a_pane_herdr_reports_and_claude_estate_does_not_gets_null(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    ages = {session["pane"]: session["age_s"] for session in snapshot["sessions"]}
    assert ages["wF:p6"] is None, "absent from the estate rows on purpose"
    assert ages["wA:p1"] == 42 and ages["wI:p9"] == 91000
    assert snapshot["age_source"] == "claude-estate", "one absent pane is not a failure"


def test_claude_estate_failure_nulls_every_age_and_says_so(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES, estate_ok=False)
    assert snapshot["age_source"] == "none"
    assert [session["age_s"] for session in snapshot["sessions"]] == [None, None, None]
    assert len(snapshot["sessions"]) == len(ADMITTED_PANES), "the publish still proceeds"


@pytest.mark.parametrize(
    "text",
    [None, "", "   \n", "not json", "[]", '{"agents": {}}', '{"summary": {}}', "null"],
)
def test_an_unusable_estate_payload_is_the_none_source(config, text) -> None:
    snapshot, _, _ = sp.snapshot_from_sources(
        herdr_envelope(),
        text,
        True,
        config,
        FakeGit(),
        fake_realpath,
        generated_at=GENERATED_AT,
    )
    assert snapshot["age_source"] == "none"
    assert all(session["age_s"] is None for session in snapshot["sessions"])


def test_an_estate_with_zero_rows_is_still_the_claude_estate_source(config) -> None:
    """An empty row list is claude-estate answering, not claude-estate failing —
    ``age_source`` must not be decided on the truthiness of the join table."""
    snapshot, _, _ = snapshot_of(config, ages={})
    assert snapshot["age_source"] == "claude-estate"
    assert all(session["age_s"] is None for session in snapshot["sessions"])


@pytest.mark.parametrize("text", [None, "", "not json", "[]", '{"agents": 3}'])
def test_parse_estate_returns_none_for_an_unusable_payload(text) -> None:
    assert sp.parse_estate(text) is None


def test_parse_estate_returns_the_pane_to_age_join_table() -> None:
    assert sp.parse_estate(estate_envelope({"wA:p1": 42, "wB:p2": 0})) == {
        "wA:p1": 42,
        "wB:p2": 0,
    }


@pytest.mark.parametrize("age", [None, "42", -1, 1.5, True, [42], {}])
def test_a_row_whose_age_is_not_a_non_negative_int_is_treated_as_absent(age) -> None:
    rows = [estate_row("wA:p1", 0), estate_row("wB:p2", 0)]
    rows[0]["age_s"] = age
    table = sp.parse_estate(estate_envelope(rows=rows))
    assert table == {"wB:p2": 0}, table


def test_a_row_without_a_usable_pane_id_is_skipped() -> None:
    rows = [estate_row("wA:p1", 5), estate_row("wB:p2", 6)]
    del rows[0]["pane_id"]
    rows[1] = dict(rows[1], pane_id=12)
    assert sp.parse_estate(estate_envelope(rows=rows)) == {}


def test_a_row_carrying_only_the_two_consumed_keys_is_enough() -> None:
    rows = [{"pane_id": "wA:p1", "age_s": 42}]
    assert sp.parse_estate(estate_envelope(rows=rows)) == {"wA:p1": 42}


def test_the_nine_other_estate_keys_are_never_accessed() -> None:
    """A JSON round trip cannot carry a recording Mapping, so the join reads the
    rows through one seam and this test drives that seam directly (probe 1.2: nine
    of the eleven keys, ``cwd``/``resume``/``session``/``title`` among them, must
    never be read)."""
    row = RecordingRow(estate_row("wA:p1", 42))
    assert sp._estate_ages([row]) == {"wA:p1": 42}
    assert set(row.accessed) == {"pane_id", "age_s"}, row.accessed


# --------------------------------------------------------------------------- #
# Task 4.6 — fail-closed sources
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,needle",
    [
        ("", "no output"),
        ("not json", "JSON"),
        ('{"id": "x"}', "result"),
        ('{"id": "x", "result": {"type": "something_else", "agents": []}}', "agent_list"),
        ('{"id": "x", "result": {"type": "agent_list", "agents": {}}}', "agents"),
    ],
)
def test_a_broken_herdr_payload_fails_the_whole_run(config, text, needle) -> None:
    """The exit code, the absence of an HTTP request, and the untouched state file
    are §5's ``main``; what group 4 owns is that the composition raises rather than
    publishing a partial estate as a whole one."""
    with pytest.raises(sp.EstateError) as excinfo:
        sp.snapshot_from_sources(
            text, estate_envelope(), True, config, FakeGit(), fake_realpath,
            generated_at=GENERATED_AT,
        )
    assert needle in str(excinfo.value)


@pytest.mark.parametrize("field", ["pane_id", "agent_status", "cwd"])
def test_a_record_missing_a_required_field_fails_the_whole_run(config, field) -> None:
    record = agent("wZ:p0", "/home/owner/Coding/henk")
    del record[field]
    with pytest.raises(sp.EstateError) as excinfo:
        snapshot_of(config, ages=ESTATE_AGES, agents=[record, ESTATE[0]])
    assert field in str(excinfo.value)


def test_a_broken_estate_source_does_not_fail_the_run(config) -> None:
    """The asymmetry the design insists on: herdr is fail-closed, claude-estate is
    fail-soft (D2)."""
    snapshot, _, _ = sp.snapshot_from_sources(
        herdr_envelope(), "not json", True, config, FakeGit(), fake_realpath,
        generated_at=GENERATED_AT,
    )
    assert snapshot["age_source"] == "none"


# --------------------------------------------------------------------------- #
# Task 4.7 — the snapshot shape
# --------------------------------------------------------------------------- #

PLAIN_TOP_LEVEL = [
    "schema",
    "generated_at",
    "publisher",
    "age_source",
    "heartbeat_s",
    "tick_s",
    "sessions",
]


def test_the_plain_snapshot_top_level_is_exactly_the_seven_keys(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert list(snapshot) == PLAIN_TOP_LEVEL
    assert snapshot["schema"] == 1
    assert snapshot["heartbeat_s"] == config.heartbeat_seconds == 900
    assert snapshot["tick_s"] == config.tick_seconds == 300


def test_the_opted_in_and_degraded_snapshot_adds_exactly_two_keys(tmp_path) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    snapshot, counts, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert list(snapshot) == PLAIN_TOP_LEVEL + ["unlisted"]
    over = _budget_snapshot(count=80, label_len=24, config=config, unlisted=True)
    final, dropped = sp.degrade(over)
    assert dropped > 0
    assert list(final) == PLAIN_TOP_LEVEL + ["unlisted", "degraded"]
    assert counts.dropped == 0, "the fixture estate fits"


def test_heartbeat_and_tick_follow_the_configuration(tmp_path) -> None:
    config = load(tmp_path, "heartbeat_seconds = 1200\ntick_seconds = 60\n" + BASE_CONFIG)
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert (snapshot["heartbeat_s"], snapshot["tick_s"]) == (1200, 60)


def test_the_publisher_identity_is_the_prefix_henk_matches_on(config) -> None:
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["publisher"] == sp.PUBLISHER
    assert snapshot["publisher"].startswith("session-publisher/")


def test_generated_at_is_utc_iso_8601(config) -> None:
    import re as _re

    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert _re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", snapshot["generated_at"])
    assert snapshot["generated_at"] == GENERATED_AT


def test_a_zero_session_snapshot_is_valid(tmp_path) -> None:
    """Every live session denied, the aggregate off: an empty ``sessions`` array is
    a snapshot, not an error — D10's ``No live sessions`` rendering depends on it."""
    config = load(tmp_path, 'allow_owners = ["owner-a"]\n')
    snapshot, counts, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["sessions"] == []
    assert "unlisted" not in snapshot
    assert (counts.admitted, counts.denied, counts.labels) == (0, len(ESTATE), ())
    body = sp.serialise(snapshot)
    assert len(body) <= sp.BODY_BUDGET_BYTES
    import json as _json

    assert _json.loads(body.decode("utf-8"))["sessions"] == []


def test_an_empty_estate_is_a_zero_session_snapshot(config) -> None:
    snapshot, counts, _ = snapshot_of(config, ages={}, agents=[])
    assert snapshot["sessions"] == []
    assert (counts.admitted, counts.denied, counts.dropped) == (0, 0, 0)


def test_the_counts_carry_the_admitted_label_set_for_the_journal_line(config) -> None:
    """Task 5.7 logs these; group 4 computes them (D7's journal line is what catches
    a re-pointed or relabelled root)."""
    _, counts, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert counts.labels == ("henk", "homelab-docs")
    assert (counts.admitted, counts.denied, counts.dropped) == (3, 7, 0)


def test_serialise_is_compact_ascii_and_preserves_key_order(config) -> None:
    """One serialiser, so the bytes the budget measured are the bytes §5 publishes."""
    snapshot, _, _ = snapshot_of(config, ages=ESTATE_AGES)
    body = sp.serialise(snapshot)
    assert isinstance(body, bytes)
    assert b", " not in body and b": " not in body
    assert body.startswith(b'{"schema":1,"generated_at":')
    body.decode("ascii")  # ensure_ascii, so a non-ASCII label cannot widen the body
    assert sp.serialise({"b": 1, "a": 2}) == b'{"b":1,"a":2}'
    assert sp.serialise({"x": "é"}) == b'{"x":"\\u00e9"}'


def test_the_budget_loop_measures_through_serialise(monkeypatch, config) -> None:
    """The budget and the publish body must not be able to disagree: the loop calls
    the same function §5 posts."""
    calls: list[int] = []
    real = sp.serialise

    def counting(snapshot):
        body = real(snapshot)
        calls.append(len(body))
        return body

    monkeypatch.setattr(sp, "serialise", counting)
    sp.degrade(_budget_snapshot(count=80, label_len=24, config=config))
    assert calls, "degrade must measure through serialise"


# --------------------------------------------------------------------------- #
# Task 4.8 — the byte budget and the degrade order
# --------------------------------------------------------------------------- #

#: The status cycle the budget fixture uses: two of the three priority bands plus
#: three statuses that fall into the third.
BUDGET_STATUSES = ("working", "idle", "blocked", "done", "unknown")


def _budget_classifications(count: int, label_len: int, *, ages: bool = True):
    """``count`` admitted classifications with labels of a chosen width, so a test
    can put the serialised body on either side of the budget deliberately."""
    items = []
    for index in range(1, count + 1):
        label = f"p{index:02d}".ljust(label_len, "x")[:label_len]
        items.append(
            sp.Classification(
                pane=f"w1:p{index:02d}",
                status=BUDGET_STATUSES[index % len(BUDGET_STATUSES)],
                admitted=True,
                label=label,
                denials=(),
            )
        )
    return items


def _budget_ages(items, *, null: bool = False):
    if null:
        return {}
    return {item.pane: (index * 7) % 900 for index, item in enumerate(items)}


def _budget_snapshot(count: int, label_len: int, *, config=None, unlisted=False, null_ages=False):
    if config is None:
        config = sp.PublisherConfig(publish_unlisted=unlisted)
    items = _budget_classifications(count, label_len)
    snapshot, _ = sp.build_snapshot(
        items, _budget_ages(items, null=null_ages), config, generated_at=GENERATED_AT
    )
    return snapshot


def _bands(snapshot):
    ranks = {"blocked": 0, "working": 1}
    return [ranks.get(session["status"], 2) for session in snapshot["sessions"]]


def test_a_forty_session_snapshot_is_under_budget_and_untouched() -> None:
    snapshot = _budget_snapshot(count=40, label_len=3)
    assert len(sp.serialise(snapshot)) <= sp.BODY_BUDGET_BYTES
    before = list(snapshot["sessions"])
    final, dropped = sp.degrade(snapshot)
    assert dropped == 0
    assert "degraded" not in final
    assert len(final["sessions"]) == 40
    assert sorted(s["pane"] for s in final["sessions"]) == sorted(
        s["pane"] for s in before
    )


def test_an_under_budget_snapshot_is_still_returned_in_priority_order() -> None:
    """The order is applied once, whether or not anything is dropped, so the
    comparison key §5 computes cannot depend on herdr's enumeration order."""
    final, dropped = sp.degrade(_budget_snapshot(count=40, label_len=3))
    assert dropped == 0
    assert _bands(final) == sorted(_bands(final))


@pytest.mark.parametrize("count,label_len", [(40, 32), (80, 12), (200, 3)])
def test_an_over_budget_snapshot_is_degraded_to_fit(count, label_len) -> None:
    snapshot = _budget_snapshot(count=count, label_len=label_len)
    assert len(sp.serialise(snapshot)) > sp.BODY_BUDGET_BYTES, "fixture must overflow"
    kept_before = [session["pane"] for session in snapshot["sessions"]]
    final, dropped = sp.degrade(snapshot)
    body = sp.serialise(final)
    assert len(body) <= sp.BODY_BUDGET_BYTES
    assert dropped > 0
    assert final["degraded"] == {"dropped": dropped}
    assert len(final["sessions"]) == count - dropped
    assert set(kept_before) >= {session["pane"] for session in final["sessions"]}


@pytest.mark.parametrize("null_ages", [False, True])
def test_the_degrade_drops_the_tail_of_the_priority_order(null_ages) -> None:
    snapshot = _budget_snapshot(count=80, label_len=16, null_ages=null_ages)
    ordered = sorted(snapshot["sessions"], key=sp.session_order_key)
    final, dropped = sp.degrade(snapshot)
    assert final["sessions"] == ordered[: len(ordered) - dropped]
    dropped_panes = {session["pane"] for session in ordered[len(ordered) - dropped :]}
    kept_panes = {session["pane"] for session in final["sessions"]}
    assert dropped_panes and kept_panes.isdisjoint(dropped_panes)


@pytest.mark.parametrize("null_ages", [False, True])
def test_every_blocked_session_survives_before_any_working_one(null_ages) -> None:
    snapshot = _budget_snapshot(count=200, label_len=8, null_ages=null_ages)
    total_blocked = sum(1 for s in snapshot["sessions"] if s["status"] == "blocked")
    total_working = sum(1 for s in snapshot["sessions"] if s["status"] == "working")
    final, dropped = sp.degrade(snapshot)
    assert dropped > 0
    statuses = [session["status"] for session in final["sessions"]]
    assert statuses.count("blocked") == total_blocked, "no blocked session is dropped"
    assert _bands(final) == sorted(_bands(final))
    # The blocked band alone does not fill the budget here, so working survives too
    # and the third band is what gets cut.
    assert 0 < statuses.count("working") <= total_working
    assert len(sp.serialise(final)) <= sp.BODY_BUDGET_BYTES


def test_when_the_budget_fits_only_part_of_the_first_band() -> None:
    """The extreme the order exists for: when even the blocked band overflows, what
    survives is blocked sessions and nothing else."""
    items = [
        sp.Classification(
            pane=f"w1:p{index:03d}",
            status="blocked" if index < 20 else "working",
            admitted=True,
            label=f"label-{index:03d}".ljust(150, "y"),
            denials=(),
        )
        for index in range(60)
    ]
    snapshot, _ = sp.build_snapshot(
        items, {item.pane: 5 for item in items}, sp.PublisherConfig(),
        generated_at=GENERATED_AT,
    )
    final, dropped = sp.degrade(snapshot)
    assert dropped > 0
    statuses = {session["status"] for session in final["sessions"]}
    assert statuses == {"blocked"}, "no working session survives a first-band overflow"
    assert 0 < len(final["sessions"]) < 20
    assert len(sp.serialise(final)) <= sp.BODY_BUDGET_BYTES


def test_the_third_band_is_ordered_by_ascending_age_with_nulls_last() -> None:
    items = [
        sp.Classification(pane="w1:pA", status="idle", admitted=True, label="a", denials=()),
        sp.Classification(pane="w1:pB", status="done", admitted=True, label="b", denials=()),
        sp.Classification(pane="w1:pC", status="unknown", admitted=True, label="c", denials=()),
        sp.Classification(pane="w1:pD", status="idle", admitted=True, label="d", denials=()),
        sp.Classification(pane="w1:pE", status="working", admitted=True, label="e", denials=()),
        sp.Classification(pane="w1:pF", status="blocked", admitted=True, label="f", denials=()),
    ]
    ages = {"w1:pA": 900, "w1:pC": 10, "w1:pD": 100, "w1:pE": 50000, "w1:pF": 90000}
    snapshot, _ = sp.build_snapshot(items, ages, sp.PublisherConfig(), generated_at=GENERATED_AT)
    final, dropped = sp.degrade(snapshot)
    assert dropped == 0
    assert [session["pane"] for session in final["sessions"]] == [
        "w1:pF",  # blocked, whatever its age
        "w1:pE",  # working, whatever its age
        "w1:pC",  # 10
        "w1:pD",  # 100
        "w1:pA",  # 900
        "w1:pB",  # null age, last
    ]


def test_session_order_key_bands_and_nulls() -> None:
    def key(status, age):
        return sp.session_order_key(
            {"pane": "w1:p1", "project": "p", "status": status, "age_s": age}
        )

    assert key("blocked", None) < key("working", 0)
    assert key("working", 99999) < key("idle", 0)
    assert key("idle", 5) < key("idle", 6)
    assert key("idle", 10**9) < key("idle", None)
    assert key("done", None) == key("unknown", None), "the third band is one band"
    assert key("blocked", 5) == key("blocked", 900), "age never reorders the first band"


def test_the_order_is_stable_within_a_band() -> None:
    items = [
        sp.Classification(
            pane=f"w1:p{i}", status="blocked", admitted=True, label=f"l{i}", denials=()
        )
        for i in range(6)
    ]
    snapshot, _ = sp.build_snapshot(
        items, {item.pane: 3 for item in items}, sp.PublisherConfig(),
        generated_at=GENERATED_AT,
    )
    final, _ = sp.degrade(snapshot)
    assert [session["pane"] for session in final["sessions"]] == [
        f"w1:p{i}" for i in range(6)
    ]


def test_the_degrade_is_tight_one_fewer_drop_would_not_have_fitted() -> None:
    """The loop re-measures after every drop, so it stops at the first fit: putting
    the last-dropped session back must overflow the budget again."""
    snapshot = _budget_snapshot(count=80, label_len=16)
    ordered = sorted(snapshot["sessions"], key=sp.session_order_key)
    final, dropped = sp.degrade(snapshot)
    assert dropped > 0
    one_fewer = dict(final)
    one_fewer["sessions"] = ordered[: len(ordered) - dropped + 1]
    if dropped - 1:
        one_fewer["degraded"] = {"dropped": dropped - 1}
    else:  # pragma: no cover - the fixture always drops more than one
        one_fewer.pop("degraded", None)
    assert len(sp.serialise(one_fewer)) > sp.BODY_BUDGET_BYTES


def _degrade_measuring_before_the_key(snapshot):
    """The mutation task 4.10 must not ship: drop until the body fits *without* the
    ``degraded`` key, then add the key — which puts the published body back over the
    budget whenever the remaining slack is smaller than the key."""
    snapshot = dict(snapshot)
    sessions = sorted(snapshot["sessions"], key=sp.session_order_key)
    snapshot["sessions"] = sessions
    dropped = 0
    while sessions and len(sp.serialise(snapshot)) > sp.BODY_BUDGET_BYTES:
        sessions.pop()
        dropped += 1
    if dropped:
        snapshot["degraded"] = {"dropped": dropped}
    return snapshot, dropped


def test_the_degraded_key_is_measured_inside_the_budget() -> None:
    """A fixture tuned so the slack after the last drop is smaller than the
    ``degraded`` key itself: measuring before adding the key overshoots, and the
    published body must not."""
    for label_len in range(4, 40):
        candidate = _budget_snapshot(count=120, label_len=label_len)
        naive, _ = _degrade_measuring_before_the_key(candidate)
        if len(sp.serialise(naive)) > sp.BODY_BUDGET_BYTES:
            break
    else:  # pragma: no cover - the search always finds one
        pytest.fail("no fixture width makes the naive measurement overshoot")
    final, dropped = sp.degrade(candidate)
    assert dropped > 0
    assert final["degraded"] == {"dropped": dropped}
    assert len(sp.serialise(final)) <= sp.BODY_BUDGET_BYTES
    assert len(final["sessions"]) < len(naive["sessions"])


def test_the_degraded_count_grows_with_its_own_digits() -> None:
    """Ten or more drops widens the ``degraded`` key by a byte; the re-measurement
    after every drop is what absorbs it."""
    snapshot = _budget_snapshot(count=400, label_len=6)
    final, dropped = sp.degrade(snapshot)
    assert dropped >= 100
    assert final["degraded"] == {"dropped": dropped}
    assert len(sp.serialise(final)) <= sp.BODY_BUDGET_BYTES


def test_degrade_leaves_the_input_snapshot_alone() -> None:
    snapshot = _budget_snapshot(count=120, label_len=16)
    before = len(snapshot["sessions"])
    sp.degrade(snapshot)
    assert len(snapshot["sessions"]) == before
    assert "degraded" not in snapshot


def test_degrade_of_a_zero_session_snapshot_is_a_no_op() -> None:
    final, dropped = sp.degrade(_budget_snapshot(count=0, label_len=4))
    assert (final["sessions"], dropped) == ([], 0)
    assert "degraded" not in final


def test_the_budget_is_the_documented_number() -> None:
    assert sp.BODY_BUDGET_BYTES == 3800


def test_snapshot_from_sources_reports_the_drop_count_in_the_counts(tmp_path) -> None:
    """The journal line's ``dropped`` comes from the composition, not from a second
    measurement (task 5.7 logs it)."""
    config = load(tmp_path, BASE_CONFIG)
    agents = [
        agent(f"w1:p{index:03d}", "/home/owner/Coding/henk", "working")
        for index in range(300)
    ]
    snapshot, counts, classifications = sp.snapshot_from_sources(
        herdr_envelope(agents), estate_envelope({}), True, config, FakeGit(),
        fake_realpath, generated_at=GENERATED_AT,
    )
    assert counts.admitted == 300
    assert counts.dropped > 0
    assert len(snapshot["sessions"]) == counts.admitted - counts.dropped
    assert snapshot["degraded"] == {"dropped": counts.dropped}
    assert len(classifications) == 300
    assert len(sp.serialise(snapshot)) <= sp.BODY_BUDGET_BYTES


# --------------------------------------------------------------------------- #
# Task 4.9 — the whole composition reads no transcript
# --------------------------------------------------------------------------- #


def test_a_full_snapshot_run_opens_no_transcript(tmp_path, transcripts) -> None:
    config = load(tmp_path, "publish_unlisted = true\n" + BASE_CONFIG)
    snapshot, counts, _ = snapshot_of(config, ages=ESTATE_AGES)
    assert snapshot["sessions"] and counts.admitted == 3
    assert transcripts.opened == []


def test_the_estate_sources_record_is_the_seam_group_five_fills() -> None:
    sources = sp.EstateSources(
        herdr_text=herdr_envelope(), estate_text=estate_envelope(), estate_ok=True
    )
    assert sources.estate_ok is True
    failed = sp.EstateSources(herdr_text=herdr_envelope(), estate_text=None, estate_ok=False)
    assert (failed.estate_text, failed.estate_ok) == (None, False)
    snapshot, _, _ = sp.snapshot_from_sources(
        failed.herdr_text,
        failed.estate_text,
        failed.estate_ok,
        sp.PublisherConfig(),
        FakeGit(),
        fake_realpath,
        generated_at=GENERATED_AT,
    )
    assert snapshot["age_source"] == "none"
