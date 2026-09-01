"""Materialise the synthetic homelab-docs corpus into a temp directory.

The static tree lives in ``tests/fixtures/homelab_docs_corpus/clone`` (see the
README beside it). This helper copies it — symlink preserved, not resolved — so a
test can mutate the corpus freely, and adds the two things git cannot store inside
another repository: a `.git/` directory, and a markdown file inside it.

Nothing here reaches production code; it exists so every corpus test starts from
one described shape rather than a bespoke tree per test.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Committed static tree (the clone root, i.e. what gets bind-mounted).
FIXTURE_CLONE = Path(__file__).parent / "fixtures" / "homelab_docs_corpus" / "clone"

#: Every documentation file, as a path relative to the documentation root.
ALL_DOCS = (
    "index.mdx",
    "devices/rp5.md",
    "devices/rp2.md",
    "services/monitoring.md",
    "services/large-runbook.md",
    "security/access.md",
)

#: The allowlist tests use when they want everything except `security/`.
DEFAULT_ALLOWLIST = ("index.mdx", "devices/", "services/")

#: The full-membership allowlist (the owner's initial membership, design D13).
FULL_ALLOWLIST = ("index.mdx", "devices/", "services/", "security/")

#: Sentinels that must not cross a boundary.
EXCLUDED_SENTINEL = "SYNTHETIC-EXCLUDED-SENTINEL"
OUTSIDE_SENTINEL = "SYNTHETIC-OUTSIDE-SENTINEL"
REPO_README_SENTINEL = "SYNTHETIC-REPO-README-SENTINEL"
BUILD_CONFIG_SENTINEL = "SYNTHETIC-BUILD-CONFIG-SENTINEL"
DEPENDENCY_SENTINEL = "SYNTHETIC-DEPENDENCY-SENTINEL"
FRONTMATTER_SENTINEL = "SYNTHETIC-FRONTMATTER-SENTINEL"
GIT_METADATA_SENTINEL = "SYNTHETIC-GIT-METADATA-SENTINEL"

#: The instant the committed stamp records as its last successful pull.
STAMP_PULLED_AT = "2026-09-01T04:00:11Z"
#: A clock reading two hours after that — comfortably inside the 26h bound.
FRESH_NOW = datetime(2026, 9, 1, 6, 0, 11, tzinfo=timezone.utc).timestamp()


def build_corpus(tmp_path: Path, *, name: str = "clone") -> Path:
    """Copy the static corpus into ``tmp_path`` and add the uncommittable parts.

    Returns the clone root — the directory a deployment would bind-mount, i.e. the
    value of ``homelab_docs.path``.
    """
    root = tmp_path / name
    shutil.copytree(FIXTURE_CLONE, root, symlinks=True)

    # A real `.git` cannot be committed inside this repository, so build one here.
    # It holds a markdown file on purpose: the extension filter alone must not be
    # what keeps version-control metadata out of the index.
    git_dir = root / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )
    (git_dir / "COMMIT_EDITMSG.md").write_text(
        f"# Commit message\n\n{GIT_METADATA_SENTINEL} — never indexed.\n",
        encoding="utf-8",
    )
    return root


def docs_root(clone: Path) -> Path:
    return clone / "src" / "content" / "docs"


def stamp_path(clone: Path) -> Path:
    return clone / "homelab-docs-stamp.json"


def write_stamp(
    clone: Path,
    *,
    commit: str = "5ynth3t1c0000000000000000000000000000abc",
    committed_at: str | None = "2026-08-30T09:14:07Z",
    pulled_at: str | None = STAMP_PULLED_AT,
) -> None:
    """Rewrite the stamp. ``None`` for a field omits it entirely."""
    payload: dict[str, str] = {}
    if commit is not None:
        payload["commit"] = commit
    if committed_at is not None:
        payload["committed_at"] = committed_at
    if pulled_at is not None:
        payload["pulled_at"] = pulled_at
    stamp_path(clone).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def at(offset_seconds: float) -> float:
    """A clock reading ``offset_seconds`` after the committed stamp's pull time."""
    return FRESH_NOW - 7200.0 + offset_seconds


def hours(count: float) -> float:
    return timedelta(hours=count).total_seconds()
