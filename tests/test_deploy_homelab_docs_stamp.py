"""Contract tests for ``deploy/homelab-docs-stamp.sh`` (read-depth task 8.3).

The script is the host-side half of the corpus freshness mechanism: it pulls the
docs clone and, only after a *successful* pull, writes the stamp that
``henk.tools.homelab_docs`` reads. Every clause of the stamp contract in
``openspec/changes/read-depth/notes/apply-decisions-docs.md`` is pinned here, and
the two ways the mechanism can silently invert — stamping an *attempt*, and
stamping *before* the content moved — each have a test that would catch them.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "homelab-docs-stamp.sh"
STAMP_NAME = "homelab-docs-stamp.json"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
# Isolate from the developer's global git config (signing, hooksPath, pull.rebase):
# the script must behave identically on rp5's bare-bones root account.
ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # An identity for the *script's* git, so a mutation that drops --ff-only would
    # succeed in merging (and be caught) instead of failing on "who are you".
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True, env=ENV
    ).stdout.strip()


def run_script(clone: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(clone)], capture_output=True, text=True, env=ENV
    )


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A bare 'GitHub' remote, an 'upstream' working clone that pushes to it, and the
    'rp5' clone the script operates on."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", remote], check=True, env=ENV)
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "clone", "-q", remote, upstream], check=True, env=ENV)
    docs = upstream / "src" / "content" / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text("# Homelab\n\nplaceholder 10.0.0.1\n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "first", cwd=upstream)
    git("push", "-q", "-u", "origin", "main", cwd=upstream)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", remote, clone], check=True, env=ENV)
    return {"remote": remote, "upstream": upstream, "clone": clone}


def push_new_commit(upstream: Path, text: str) -> str:
    (upstream / "src" / "content" / "docs" / "index.md").write_text(text)
    git("commit", "-q", "-am", "update", cwd=upstream)
    git("push", "-q", cwd=upstream)
    return git("rev-parse", "HEAD", cwd=upstream)


def read_stamp(clone: Path) -> dict:
    return json.loads((clone / STAMP_NAME).read_text())


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_success_writes_stamp_naming_head_and_a_utc_pull_time(repo) -> None:
    clone = repo["clone"]
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    result = run_script(clone)
    assert result.returncode == 0, result.stderr
    stamp = read_stamp(clone)
    assert isinstance(stamp, dict)
    assert stamp["commit"] == git("rev-parse", "HEAD", cwd=clone)
    assert stamp["committed_at"] == git("log", "-1", "--format=%cI", cwd=clone)
    pulled = datetime.fromisoformat(stamp["pulled_at"].replace("Z", "+00:00"))
    assert pulled.tzinfo is not None, "pulled_at must carry an explicit offset"
    assert before <= pulled <= datetime.now(timezone.utc) + timedelta(seconds=2)


def test_pull_moves_content_and_stamp_names_the_new_commit(repo) -> None:
    clone, upstream = repo["clone"], repo["upstream"]
    assert run_script(clone).returncode == 0
    new_sha = push_new_commit(upstream, "# Homelab\n\nsecond revision\n")
    result = run_script(clone)
    assert result.returncode == 0, result.stderr
    assert "second revision" in (clone / "src/content/docs/index.md").read_text()
    assert read_stamp(clone)["commit"] == new_sha


def test_failed_pull_leaves_the_previous_stamp_byte_identical(repo) -> None:
    """The inversion the contract warns about: stamping every *attempt* makes a
    dead updater read as permanently fresh."""
    clone = repo["clone"]
    assert run_script(clone).returncode == 0
    stamp_path = clone / STAMP_NAME
    frozen = stamp_path.read_bytes()
    git("remote", "set-url", "origin", str(clone.parent / "does-not-exist.git"), cwd=clone)
    result = run_script(clone)
    assert result.returncode != 0
    assert stamp_path.read_bytes() == frozen


def test_first_run_failure_writes_no_stamp_at_all(repo) -> None:
    clone = repo["clone"]
    git("remote", "set-url", "origin", str(clone.parent / "does-not-exist.git"), cwd=clone)
    result = run_script(clone)
    assert result.returncode != 0
    assert not (clone / STAMP_NAME).exists()


def test_divergent_history_is_refused_not_merged(repo) -> None:
    """Fast-forward only: the clone is a mirror, never an author. A local commit
    that diverges must fail the pull, leave no merge commit, and leave the stamp."""
    clone, upstream = repo["clone"], repo["upstream"]
    assert run_script(clone).returncode == 0
    frozen = (clone / STAMP_NAME).read_bytes()
    (clone / "local-edit.md").write_text("local\n")
    git("add", "local-edit.md", cwd=clone)
    git("commit", "-q", "-m", "local divergence", cwd=clone)
    push_new_commit(upstream, "# Homelab\n\nupstream moved too\n")
    result = run_script(clone)
    assert result.returncode != 0
    assert (clone / STAMP_NAME).read_bytes() == frozen
    assert "Merge" not in git("log", "-1", "--format=%s", cwd=clone)


def test_stamp_is_excluded_from_git_status_idempotently(repo) -> None:
    clone = repo["clone"]
    assert run_script(clone).returncode == 0
    assert run_script(clone).returncode == 0
    assert git("status", "--porcelain", cwd=clone) == ""
    exclude = (clone / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(STAMP_NAME) == 1


def test_no_temporary_files_are_left_in_the_clone_root(repo) -> None:
    clone = repo["clone"]
    assert run_script(clone).returncode == 0
    names = {p.name for p in clone.iterdir()}
    assert names == {".git", "src", STAMP_NAME}, names


def test_missing_directory_fails_without_creating_anything(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    result = run_script(target)
    assert result.returncode != 0
    assert not target.exists()
    assert "absent" in result.stderr


def test_directory_that_is_not_a_git_clone_fails(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_script(plain)
    assert result.returncode != 0
    assert not (plain / STAMP_NAME).exists()


def test_usage_error_without_an_argument() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=ENV)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
