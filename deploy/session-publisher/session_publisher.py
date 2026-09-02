#!/usr/bin/env python3
"""session-publisher — publish a metadata-only snapshot of the workstation's live
Claude Code sessions to the ntfy topic Henk reads.

Runs from a systemd **user** timer on the workstation, outside the Henk container,
with the system interpreter and no virtualenv — hence stdlib only. It reads the
live estate from `herdr agent list` (pane set, status, working directories) and
`claude-estate status --json` (last-activity age), classifies each pane against two
independent gates, and publishes at most four keys per admitted session.

Design: `openspec/changes/session-awareness/design.md` (D2-D7, D14).
Probed facts the code follows rather than the design prose:
`openspec/changes/session-awareness/notes/estate-probe.md`.

**It never reads a transcript.** No transcript file is opened, and neither of the
two transcript readers on this workstation is invoked from here. That is a boundary,
not an optimisation.

Two gates, both applied to every reported path (`cwd`, and `foreground_cwd` when it
is present and different):

  root gate   the canonical path is under an `allow_roots` entry and under no
              `deny_roots` entry; deny wins at any depth
  owner gate  the canonical path is not inside a git work tree, or its `origin`
              names an owner in `allow_owners`

A session is published only when every reported path passes both gates. The project
label is the configured label of the entry admitting `cwd` — never a path component.

Exit codes: 0 published or nothing to publish · 1 a source or the publish failed ·
2 configuration refused or an unsupported interpreter.
"""

from __future__ import annotations

import sys

#: The floor is `tomllib`, which arrived in 3.11. The check runs before that import
#: so an older interpreter gets the sentence rather than an ImportError traceback.
MINIMUM_PYTHON = (3, 11)


def require_python(version=None, *, stream=None) -> None:
    """Exit 2 with a message naming the requirement on an interpreter below 3.11."""
    version = sys.version_info if version is None else version
    if tuple(version[:2]) >= MINIMUM_PYTHON:
        return
    stream = sys.stderr if stream is None else stream
    have = ".".join(str(part) for part in version[:3])
    stream.write(
        "session-publisher requires Python 3.11 or newer (for tomllib); "
        f"this interpreter is {have}\n"
    )
    raise SystemExit(2)


require_python()

import json  # noqa: E402  (deliberately after the interpreter guard)
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import tomllib  # noqa: E402
from dataclasses import dataclass, replace  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, Iterable, Mapping, Protocol, Sequence  # noqa: E402

PUBLISHER = "session-publisher/0.1"

#: ntfy's default body limit is 4096 bytes and an over-limit body is silently
#: promoted to an attachment (probe 1.4), which would make Henk fetch a second
#: URL. The budget is the limit less headroom, and the degrade loop below is what
#: keeps a publish inside it (D6).
BODY_BUDGET_BYTES = 3800

#: The two statuses that outrank age in the degrade order. Everything else — and
#: an unrecognised status is deliberately everything else — sorts in the third
#: band by ascending age (D6).
_STATUS_BANDS = {"blocked": 0, "working": 1}
_OTHER_BAND = 2

#: The same shape Henk enforces at render, so a legal configuration can never
#: produce a session Henk would classify as unusable (D3).
LABEL_PATTERN = re.compile(r"^[\w:.-]{1,32}$")

#: Every key the configuration may carry, mapped to its expected type and default.
#: The schema is closed: anything else is refused by name at load, because a
#: misspelled `deny_root` would otherwise load with the only deny rule absent, and
#: `fields` must not be a configuration flag (D3, D4).
_TOP_LEVEL_DEFAULTS: dict[str, object] = {
    "allow_roots": (),
    "allow_owners": (),
    "deny_roots": (),
    "publish_unlisted": False,
    "heartbeat_seconds": 900,
    "tick_seconds": 300,
    "ntfy_url": "",
    "topic": "henk-sessions",
    "token_file": "~/.config/henk-session-publisher/ntfy-token",
    "token_env": "HENK_SESSION_PUBLISHER_TOKEN",
}

_ENTRY_KEYS = frozenset({"path", "label"})

#: Roots too broad to allow, whatever the owner meant. Applied to each entry's
#: canonical path; `/mnt` and every immediate child are refused so a WSL drive
#: mount such as `/mnt/c` cannot be an allow root.
_UNSAFE_FIXED = (
    "/",
    "/home",
    "/root",
    "/mnt",
    "/tmp",
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
)


class ConfigError(Exception):
    """The configuration violates the closed schema, or names an unsafe root.

    Raised at load. The entry point maps it onto exit 2 with nothing published,
    no HTTP request, and no state write.
    """


class EstateError(Exception):
    """The estate source is unusable: `herdr` failed, emitted non-JSON, or emitted
    an agent record without `pane_id`, `agent_status`, or `cwd`.

    A partial estate presented as a whole one is the failure mode to refuse (D2),
    so the entry point maps this onto a non-zero exit with nothing published.
    """


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RootEntry:
    """One `allow_roots` entry, canonicalised at load."""

    canonical_path: str
    label: str


@dataclass(frozen=True)
class PublisherConfig:
    """The whole configuration, validated and canonicalised.

    `allow_roots` and `deny_roots` hold canonical paths only: classification never
    sees a reported path again after `classify` canonicalises it.
    """

    allow_roots: tuple[RootEntry, ...] = ()
    allow_owners: frozenset[str] = frozenset()
    deny_roots: tuple[str, ...] = ()
    publish_unlisted: bool = False
    heartbeat_seconds: int = 900
    tick_seconds: int = 300
    ntfy_url: str = ""
    topic: str = "henk-sessions"
    token_file: str = _TOP_LEVEL_DEFAULTS["token_file"]  # type: ignore[assignment]
    token_env: str = "HENK_SESSION_PUBLISHER_TOKEN"


def _entry_name(entry: Mapping[str, object]) -> str:
    """How an `allow_roots` entry is named in a refusal message: both its label and
    its path when they are available, so the owner can find it in the file."""
    label = entry.get("label")
    path = entry.get("path")
    parts = []
    if isinstance(label, str) and label:
        parts.append(f"label {label!r}")
    if isinstance(path, str) and path:
        parts.append(f"path {path!r}")
    if not parts:
        parts.append(repr(dict(entry)))
    return " / ".join(parts)


def _unsafe_roots(env: Mapping[str, str], realpath: Callable[[str], str], tempdir: str) -> set[str]:
    unsafe = {canonicalise(path, realpath) for path in _UNSAFE_FIXED}
    home = env.get("HOME") or ""
    if home:
        unsafe.add(canonicalise(home, realpath))
    if tempdir:
        unsafe.add(canonicalise(tempdir, realpath))
    return unsafe


def _is_mnt_child(canonical: str) -> bool:
    return canonical.startswith("/mnt/") and canonical.count("/") == 2


def load_config(
    path: Path | str,
    *,
    env: Mapping[str, str],
    realpath: Callable[[str], str] = os.path.realpath,
    tempdir: str = "",
) -> PublisherConfig:
    """Read and validate the TOML configuration, or raise `ConfigError`.

    The schema is closed at both levels, `label` is required/unique/shaped, and
    every allow root is canonicalised and checked against the unsafe-root set.
    An allow root is NOT refused for containing a deny root beneath it: that is
    exactly the configuration `deny_roots` exists to express (D3).

    `env`, `realpath`, and `tempdir` are injected so the tests can exercise the
    `$HOME` and system-temp refusals without touching the real filesystem.
    """
    tempdir = tempdir or tempfile.gettempdir()
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"configuration file unreadable: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file is not valid TOML: {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"configuration file is not valid UTF-8: {path}: {exc}") from exc

    unknown = sorted(set(raw) - set(_TOP_LEVEL_DEFAULTS))
    if unknown:
        raise ConfigError(
            "unrecognised configuration key(s) "
            + ", ".join(repr(key) for key in unknown)
            + "; the publisher schema is closed, so a misspelling is refused rather "
            "than ignored"
        )

    def scalar(key: str, kind: type, kind_name: str):
        value = raw.get(key, _TOP_LEVEL_DEFAULTS[key])
        if kind is int and isinstance(value, bool):
            raise ConfigError(f"{key!r} must be {kind_name}, not a boolean")
        if not isinstance(value, kind):
            raise ConfigError(f"{key!r} must be {kind_name}, got {type(value).__name__}")
        return value

    publish_unlisted = scalar("publish_unlisted", bool, "a boolean")
    heartbeat_seconds = scalar("heartbeat_seconds", int, "an integer")
    tick_seconds = scalar("tick_seconds", int, "an integer")
    ntfy_url = scalar("ntfy_url", str, "a string")
    topic = scalar("topic", str, "a string")
    token_file = scalar("token_file", str, "a string")
    token_env = scalar("token_env", str, "a string")

    for key, value in (("heartbeat_seconds", heartbeat_seconds), ("tick_seconds", tick_seconds)):
        if value <= 0:
            raise ConfigError(f"{key!r} must be a positive number of seconds, got {value}")

    def string_list(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"{key!r} must be a list of strings, got {type(value).__name__}")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"{key!r} must be a list of non-empty strings, got {item!r}")
        return tuple(value)

    allow_owners = frozenset(string_list("allow_owners"))

    deny_roots_raw = raw.get("deny_roots", [])
    if not isinstance(deny_roots_raw, list):
        raise ConfigError(
            f"'deny_roots' must be a list of strings, got {type(deny_roots_raw).__name__}"
        )
    deny_roots: list[str] = []
    for item in deny_roots_raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"'deny_roots' must be a list of non-empty strings, got {item!r}")
        if not item.startswith("/"):
            raise ConfigError(f"'deny_roots' entry must be an absolute path, got {item!r}")
        canonical = canonicalise(item, realpath)
        if canonical not in deny_roots:
            deny_roots.append(canonical)

    entries_raw = raw.get("allow_roots", [])
    if not isinstance(entries_raw, list):
        raise ConfigError(
            f"'allow_roots' must be a list of tables, got {type(entries_raw).__name__}"
        )

    unsafe = _unsafe_roots(env, realpath, tempdir)
    entries: list[RootEntry] = []
    seen_labels: dict[str, str] = {}
    for entry in entries_raw:
        if not isinstance(entry, dict):
            raise ConfigError(
                "each 'allow_roots' entry must be a table of 'path' and 'label', got "
                f"{entry!r}"
            )
        extra = sorted(set(entry) - _ENTRY_KEYS)
        if extra:
            raise ConfigError(
                "unrecognised key(s) "
                + ", ".join(repr(key) for key in extra)
                + f" in 'allow_roots' entry ({_entry_name(entry)}); the publisher "
                "schema is closed and 'fields' in particular is deliberately not part "
                "of it"
            )
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) needs a non-empty 'path'"
            )
        if not raw_path.startswith("/"):
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) needs an absolute 'path'"
            )
        label = entry.get("label")
        if not isinstance(label, str) or not label:
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) needs a 'label'; the label "
                "is what is published as the project, so it is never inferred"
            )
        if not LABEL_PATTERN.match(label):
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) has a 'label' that does not "
                f"match {LABEL_PATTERN.pattern}, which is the shape Henk enforces at "
                "render"
            )
        if label in seen_labels:
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) repeats the label "
                f"{label!r}, already used by path {seen_labels[label]!r}; labels "
                "identify projects and must be unique"
            )
        canonical = canonicalise(raw_path, realpath)
        if canonical in unsafe or _is_mnt_child(canonical):
            raise ConfigError(
                f"'allow_roots' entry ({_entry_name(entry)}) canonicalises to "
                f"{canonical!r}, which is too broad to allow"
            )
        seen_labels[label] = raw_path
        entries.append(RootEntry(canonical_path=canonical, label=label))

    return PublisherConfig(
        allow_roots=tuple(entries),
        allow_owners=allow_owners,
        deny_roots=tuple(deny_roots),
        publish_unlisted=publish_unlisted,
        heartbeat_seconds=heartbeat_seconds,
        tick_seconds=tick_seconds,
        ntfy_url=ntfy_url,
        topic=topic,
        token_file=token_file,
        token_env=token_env,
    )


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


class GitRunner(Protocol):
    """The one git operation the owner gate needs, injectable for tests."""

    def origin_url(self, path: str) -> tuple[int, str]:
        """Return `(returncode, stdout)` for `git -C <path> remote get-url origin`.

        Probe 1.3: rc 0 with the URL · rc 2 for a checkout with no `origin` ·
        rc 128 for a directory that is not a git work tree. A `TimeoutExpired`
        may propagate; `owner_gate` turns it into a refusal.
        """


def git_command(path: str) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of every git invocation the publisher makes.

    One function so the clean-environment guarantee is asserted in one place:
    `GIT_TERMINAL_PROMPT=0` and `GIT_CONFIG_NOSYSTEM=1` so a misconfigured checkout
    can neither prompt nor pick up a system-wide rule, `-c core.pager=cat` so no
    pager can hold the tick open. The environment is deliberately minimal — the
    publisher's own environment carries a token, and none of it is git's business.
    """
    argv = ["git", "-C", path, "-c", "core.pager=cat", "remote", "get-url", "origin"]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return argv, env


class SubprocessGit:
    """The production `GitRunner`: one bounded subprocess per queried path."""

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def origin_url(self, path: str) -> tuple[int, str]:
        argv, env = git_command(path)
        completed = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        return completed.returncode, completed.stdout


def owner_of(url: str | None) -> str | None:
    """The owner path segment of a git remote URL, or `None` when there is none.

    Handles `https://host/owner/repo`, `git@host:owner/repo`,
    `ssh://git@host[:port]/owner/repo`, and host-alias forms such as
    `git@github.com-work:owner/repo.git` identically — a `.git` suffix and a
    trailing slash do not matter. A URL naming no owner segment yields `None`,
    which the owner gate treats as a refusal.
    """
    text = (url or "").strip()
    if not text:
        return None
    if "://" in text:
        remainder = text.split("://", 1)[1]
        if "/" not in remainder:
            return None
        rest = remainder.split("/", 1)[1]
    elif ":" in text and not text.startswith("/"):
        rest = text.split(":", 1)[1]
    else:
        return None
    segments = [segment for segment in rest.strip("/").split("/") if segment]
    if len(segments) < 2:
        return None
    owner = segments[0]
    return owner or None


# --------------------------------------------------------------------------- #
# Canonicalisation and the two gates
# --------------------------------------------------------------------------- #


def canonicalise(path: str, realpath: Callable[[str], str] = os.path.realpath) -> str:
    """The path every gate decides on: symlinks and `..` resolved, trailing
    separator dropped.

    Classification on the *reported* path is the defect this exists to prevent: a
    symlink under an allowed root, or a scratch worktree under `/tmp`, can name a
    denied checkout.
    """
    resolved = realpath(os.path.normpath(path))
    resolved = os.path.normpath(resolved)
    if len(resolved) > 1:
        resolved = resolved.rstrip("/")
    return resolved


def _is_under(candidate: str, root: str) -> bool:
    """Segment-aware containment: `/home/owner/henk` is not under `/home/owner/hen`."""
    if root == "/":
        return True
    return candidate == root or candidate.startswith(root + "/")


@dataclass(frozen=True)
class RootDecision:
    admitted: bool
    label: str | None
    reason: str


@dataclass(frozen=True)
class OwnerDecision:
    admitted: bool
    reason: str


def root_gate(canonical: str, config: PublisherConfig) -> RootDecision:
    """Deny wins at any depth; otherwise the longest matching allow root admits and
    supplies the label."""
    for deny in config.deny_roots:
        if _is_under(canonical, deny):
            return RootDecision(False, None, "deny_roots")
    best: RootEntry | None = None
    for entry in config.allow_roots:
        if _is_under(canonical, entry.canonical_path):
            if best is None or len(entry.canonical_path) > len(best.canonical_path):
                best = entry
    if best is None:
        return RootDecision(False, None, "allow_roots")
    return RootDecision(True, best.label, "root:allowed")


def owner_gate(canonical: str, config: PublisherConfig, git: GitRunner) -> OwnerDecision:
    """A path passes when it is not inside a git work tree, or when its `origin`
    names an allowed owner.

    Every failure of the git call — a timeout, a missing binary, an unexpected exit
    code — is a *refusal*, never a crash: a tick that cannot establish ownership
    must not publish the session, and must not take the whole run down either.
    """
    try:
        returncode, stdout = git.origin_url(canonical)
    except subprocess.TimeoutExpired:
        return OwnerDecision(False, "owner:timeout")
    except OSError:
        return OwnerDecision(False, "owner:git-unavailable")
    if returncode == 128:
        # Not a git work tree at all (probe 1.3): there is no owner to check.
        return OwnerDecision(True, "not-git")
    if returncode == 2:
        return OwnerDecision(False, "owner:no-origin")
    if returncode != 0:
        return OwnerDecision(False, "owner:git-failed")
    owner = owner_of(stdout)
    if owner is None:
        return OwnerDecision(False, "owner:unparseable")
    if owner in config.allow_owners:
        return OwnerDecision(True, "owner:allowed")
    return OwnerDecision(False, "owner:not-allowed")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Denial:
    #: Which reported path was refused: "cwd" or "foreground_cwd".
    reported_path_role: str
    #: Which gate refused it: "root" or "owner".
    gate: str
    #: The machine-readable reason, e.g. "deny_roots" or "owner:not-allowed".
    reason: str


@dataclass(frozen=True)
class Classification:
    pane: str
    status: str
    admitted: bool
    label: str | None
    denials: tuple[Denial, ...]


_REQUIRED_FIELDS = ("pane_id", "agent_status", "cwd")


def _required(agent: Mapping[str, object], field: str) -> str:
    value = agent.get(field)
    if not isinstance(value, str) or not value:
        raise EstateError(
            f"herdr agent record has no usable {field!r} (got {value!r}); a partial "
            "estate is refused rather than published as a whole one"
        )
    return value


def classify(
    agent: Mapping[str, object],
    config: PublisherConfig,
    git: GitRunner,
    realpath: Callable[[str], str] = os.path.realpath,
) -> Classification:
    """Decide one pane, on every path it reports.

    Both gates are applied to `cwd` and, when `foreground_cwd` is present and
    different, to `foreground_cwd` too: publishing on `cwd` alone would let a
    foreground excursion into a denied checkout ride out on a session admitted by
    its shell's working directory (D3). The label is the entry admitting `cwd`.

    The owner gate is consulted only for a path the root gate admitted — a denied
    path is not worth a subprocess, and the denial has one cause.
    """
    pane = _required(agent, "pane_id")
    status = _required(agent, "agent_status")
    cwd = _required(agent, "cwd")

    foreground = agent.get("foreground_cwd")
    if foreground is not None and not isinstance(foreground, str):
        raise EstateError(
            f"herdr agent record for pane {pane!r} has a non-string 'foreground_cwd' "
            f"({foreground!r})"
        )

    roles: list[tuple[str, str]] = [("cwd", cwd)]
    if isinstance(foreground, str) and foreground and foreground != cwd:
        roles.append(("foreground_cwd", foreground))

    denials: list[Denial] = []
    label: str | None = None
    for role, reported in roles:
        canonical = canonicalise(reported, realpath)
        root = root_gate(canonical, config)
        if role == "cwd":
            label = root.label
        if not root.admitted:
            denials.append(Denial(role, "root", root.reason))
            continue
        owner = owner_gate(canonical, config, git)
        if not owner.admitted:
            denials.append(Denial(role, "owner", owner.reason))

    # `label` is the `cwd` root decision's label whether or not the session was
    # admitted — a session the owner gate refused still has a root that would have
    # labelled it, and the dry-run table is easier to read for it. Only an admitted
    # session's label is ever published (task group 4).
    return Classification(
        pane=pane,
        status=status,
        admitted=not denials,
        label=label,
        denials=tuple(denials),
    )


def parse_herdr(text: str) -> list[Mapping[str, object]]:
    """The agent records from one `herdr agent list` invocation.

    Probe 1.1: the output is a single JSON line whose agents live at
    `result.agents`, inside `{"id": ..., "result": {"type": "agent_list", ...}}` —
    not at the top level, as the design's shorthand had it. Anything else is an
    `EstateError`, because a shape the publisher does not recognise is not an empty
    estate.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise EstateError("herdr agent list produced no output")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise EstateError(f"herdr agent list did not produce JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EstateError("herdr agent list did not produce a JSON object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise EstateError("herdr agent list output has no 'result' object")
    if result.get("type") != "agent_list":
        raise EstateError(
            f"herdr agent list output is not an agent_list (type={result.get('type')!r})"
        )
    agents = result.get("agents")
    if not isinstance(agents, list):
        raise EstateError("herdr agent list output has no 'result.agents' list")
    for record in agents:
        if not isinstance(record, dict):
            raise EstateError(f"herdr agent record is not an object: {record!r}")
    return list(agents)


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #

_EMPTY_OWNERS_NOTE = (
    "note: 'allow_owners' is empty, so only non-git directories can pass the owner gate"
)


def render_dry_run(
    classifications: Iterable[Classification], config: PublisherConfig
) -> str:
    """The per-pane classification table `--dry-run` prints (D7).

    One line per pane: the pane id, the verdict, the status, and either the project
    label or every (gate, reported path) pair that refused it. Deliberately carries
    no working directory: task 8.5 records this table's findings in a review note,
    under the standing rule that no live path or label enters the repository.
    """
    lines: list[str] = []
    for item in classifications:
        verdict = "admitted" if item.admitted else "denied"
        detail = (
            f"project={item.label}"
            if item.admitted
            else "; ".join(
                f"{denial.gate} gate on {denial.reported_path_role}"
                for denial in item.denials
            )
        )
        lines.append(f"{item.pane} {verdict:<8}  status={item.status}  {detail}")
    if not config.allow_owners:
        lines.append(_EMPTY_OWNERS_NOTE)
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- #
# The age source
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EstateSources:
    """The two CLI payloads one tick read, as text.

    Group 5's entry point fills this from two bounded subprocesses and passes its
    three fields to `snapshot_from_sources`; everything below the entry point takes
    text, so the whole pipeline is testable without a subprocess.

    `estate_ok` is False when `claude-estate` was absent or exited non-zero. It is
    kept separate from `estate_text` because an empty payload from a working CLI
    ("no rows") and a missing CLI are different facts: the first is an age source
    with nothing to say, the second is `age_source: "none"` (D2).
    """

    herdr_text: str
    estate_text: str | None = None
    estate_ok: bool = True


def _estate_ages(rows: Iterable[object]) -> dict[str, int]:
    """The pane-to-age join table, reading **only** `pane_id` and `age_s`.

    Probe 1.2 recorded eleven keys per row, four of which (`cwd`, `resume`,
    `session`, `title`) must never leave the machine. This function is written so
    that only the two consumed keys are ever indexed — no key iteration, no row
    copy, no `repr` — and a test drives it with a recording Mapping to prove the
    other nine were not touched. It exists as a separate function from
    `parse_estate` for exactly that reason: a JSON round trip cannot carry a
    recording Mapping.

    An `age_s` that is not a non-negative integer is treated as absent for that
    pane, which publishes `age_s: null` for it rather than a value the publisher
    cannot vouch for.
    """
    ages: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, (dict, Mapping)):
            continue
        pane = row.get("pane_id")
        if not isinstance(pane, str) or not pane:
            continue
        age = row.get("age_s")
        if isinstance(age, bool) or not isinstance(age, int) or age < 0:
            continue
        ages[pane] = age
    return ages


def parse_estate(text: str | None) -> dict[str, int] | None:
    """The `claude-estate status --json` join table, or `None` when there is none.

    Probe 1.2: the payload is `{"agents": [rows], "summary": {...}}`. `None` is the
    "no age source" signal — an absent, empty, non-JSON, or wrong-shaped payload —
    and the snapshot then declares `age_source: "none"` with every `age_s` null.
    An empty *row list* is not that: it is a working age source with nothing to
    report, and returns an empty table, so the caller must distinguish the two by
    `is None` rather than by truthiness.

    claude-estate declares itself "NOT A SECURITY BOUNDARY"; it is a data source
    here and never a gate, which is why nothing in this function can admit a
    session.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("agents")
    if not isinstance(rows, list):
        return None
    return _estate_ages(rows)


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SnapshotCounts:
    """What the per-run journal line states (D7, task 5.7).

    The admitted label set is the artefact that catches a re-pointed or relabelled
    allow root, which Henk's own label gate cannot see (D1).
    """

    admitted: int
    denied: int
    dropped: int
    labels: tuple[str, ...]


def build_snapshot(
    classifications: Sequence[Classification],
    ages: dict[str, int] | None,
    config: PublisherConfig,
    *,
    generated_at: str,
) -> tuple[dict, SnapshotCounts]:
    """Project the admitted classifications onto the four published keys and
    assemble the snapshot object (D4, D5, D6).

    Each session object is built key by key, in the documented order, from values
    that already exist as scalars: the pane id, the *configured* label of the root
    that admitted `cwd`, herdr's `agent_status` verbatim, and the joined age. There
    is no path, no title, and no configuration flag that can add a fifth key —
    widening the set is a schema change, and the closed config schema refuses
    `fields` by name so it cannot arrive as a setting (D3, D4).

    `ages` is `None` when there is no age source; every `age_s` is then `null` and
    the snapshot says `age_source: "none"`. A pane absent from a working join table
    is also `null`, and that is not a failure of the source.
    """
    admitted = [item for item in classifications if item.admitted]
    denied = [item for item in classifications if not item.admitted]

    sessions: list[dict] = []
    for item in admitted:
        session = {
            "pane": item.pane,
            "project": item.label,
            "status": item.status,
            "age_s": None if ages is None else ages.get(item.pane),
        }
        sessions.append(session)

    snapshot: dict = {
        "schema": 1,
        "generated_at": generated_at,
        "publisher": PUBLISHER,
        "age_source": "none" if ages is None else "claude-estate",
        "heartbeat_s": config.heartbeat_seconds,
        "tick_s": config.tick_seconds,
        "sessions": sessions,
    }

    if config.publish_unlisted:
        # Two integers and nothing else about the sessions the gates refused: no
        # label, no age, no status breakdown beyond blocked (D5). Absent entirely
        # when the owner has not opted in, so Henk cannot tell "none" from "not
        # reporting" — which is the point.
        snapshot["unlisted"] = {
            "count": len(denied),
            "blocked": sum(1 for item in denied if item.status == "blocked"),
        }

    counts = SnapshotCounts(
        admitted=len(admitted),
        denied=len(denied),
        dropped=0,
        labels=tuple(sorted({item.label for item in admitted if item.label})),
    )
    return snapshot, counts


def serialise(snapshot: Mapping[str, object]) -> bytes:
    """The published bytes, and the bytes the budget measures.

    One function for both, so the body that fitted cannot differ from the body that
    is posted. Compact separators keep sessions cheap; `ensure_ascii` means a label
    outside ASCII cannot widen the body after the measurement; key order is the
    insertion order the builder chose and is never sorted.
    """
    return json.dumps(
        snapshot, separators=(",", ":"), ensure_ascii=True, sort_keys=False
    ).encode("utf-8")


def session_order_key(session: Mapping[str, object]) -> tuple[int, int, int]:
    """The degrade order: `blocked` first, `working` second, everything else third
    by ascending `age_s` with `null` ages last (D6).

    An unrecognised status sorts into the third band deliberately: herdr's status
    detection is heuristic and remotely versioned, so a status this publisher has
    never heard of must not outrank a status it has. Age never reorders the first
    two bands — "is anything waiting on me" does not depend on how long it has
    been waiting.
    """
    band = _STATUS_BANDS.get(session.get("status"), _OTHER_BAND)  # type: ignore[arg-type]
    if band != _OTHER_BAND:
        return (band, 0, 0)
    age = session.get("age_s")
    if isinstance(age, bool) or not isinstance(age, int):
        return (_OTHER_BAND, 1, 0)
    return (_OTHER_BAND, 0, age)


def degrade(snapshot: Mapping[str, object]) -> tuple[dict, int]:
    """Order the sessions once, then drop from the tail until the serialised body
    fits `BODY_BUDGET_BYTES`, and record how many went (D6).

    Two details are load-bearing. The body is **re-measured after every drop**, and
    it is measured **with the `degraded` key already present** — the key costs
    bytes, and it widens again at ten and a hundred drops, so measuring without it
    would publish a body over the budget and let ntfy promote it to an attachment.
    And the order is applied whether or not anything is dropped, so the comparison
    key group 5 computes cannot depend on herdr's enumeration order.

    The input snapshot is left alone; the returned one is a shallow copy sharing
    the session objects. `degraded` is absent when nothing was dropped.
    """
    ordered = sorted(snapshot.get("sessions") or [], key=session_order_key)
    result = dict(snapshot)
    result["sessions"] = ordered
    result.pop("degraded", None)

    dropped = 0
    while ordered and len(serialise(result)) > BODY_BUDGET_BYTES:
        ordered.pop()
        dropped += 1
        result["degraded"] = {"dropped": dropped}
    return result, dropped


def snapshot_from_sources(
    herdr_text: str,
    estate_text: str | None,
    estate_ok: bool,
    config: PublisherConfig,
    git: GitRunner,
    realpath: Callable[[str], str] = os.path.realpath,
    *,
    generated_at: str,
) -> tuple[dict, SnapshotCounts, list[Classification]]:
    """The whole pipeline over one tick's two payloads.

    `parse_herdr` -> `classify` each pane -> `parse_estate` -> `build_snapshot` ->
    `degrade`. The classifications come back so `--dry-run` can render its table
    from the same pass that produced the snapshot.

    `EstateError` propagates: herdr is the authoritative pane set, and a partial
    estate presented as a whole one is the failure mode to refuse, so group 5's
    entry point maps this onto a non-zero exit with nothing published and no state
    write. A failing `claude-estate`, by contrast, is absorbed here — `estate_ok`
    False simply means no age source (D2).
    """
    agents = parse_herdr(herdr_text)
    classifications = [classify(agent, config, git, realpath) for agent in agents]
    ages = parse_estate(estate_text) if estate_ok else None
    snapshot, counts = build_snapshot(
        classifications, ages, config, generated_at=generated_at
    )
    snapshot, dropped = degrade(snapshot)
    return snapshot, replace(counts, dropped=dropped), classifications


# --------------------------------------------------------------------------- #
# Seams owned by later task groups
# --------------------------------------------------------------------------- #


def comparison_key(*args, **kwargs):
    """The change-detection key: computed from the final, post-degrade snapshot with
    `generated_at`, every `age_s`, `heartbeat_s`, and `tick_s` removed (task group 5:
    task 5.1). Not implemented here."""
    raise NotImplementedError("task group 5 owns the comparison key")


def publish(*args, **kwargs):
    """POST the serialised snapshot to `{ntfy_url}/{topic}` with the bearer token,
    over stdlib `urllib`, exactly once per run (task group 5: tasks 5.3-5.4, 5.9).
    Not implemented here."""
    raise NotImplementedError("task group 5 owns the transport")


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI entry point: argument parsing, the advisory lock, the state
    directory, `--dry-run`, the change-or-heartbeat decision, and the exit codes
    (task group 5: tasks 5.5-5.9). Not implemented here.

    Classification is complete and callable without it: `load_config`,
    `parse_herdr`, `classify`, and `render_dry_run` compose into the dry-run table
    today.
    """
    raise NotImplementedError("task group 5 owns the entry point")


if __name__ == "__main__":  # pragma: no cover - task group 5 wires this up
    raise SystemExit(main(sys.argv[1:]))
