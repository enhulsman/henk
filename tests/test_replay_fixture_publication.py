"""*Fixtures pass the publication checks* (specs/triage-replay, *Replay test
fixtures carry no real homelab data*; triage-quality task 12.4).

Every replay fixture file is staged into a scratch git repository and the
repository's own `.githooks/pre-commit` runs over it: the pattern layer (tailnet
addresses, non-allowlisted domains, token shapes, real-looking phone numbers),
gitleaks when it is installed, and the untracked project-local checks when this
checkout has them. A control run with one planted tailnet address proves the gate
is live, so a hook that silently checks nothing cannot pass this test.

The planted values are assembled at run time, never written out here: this file
is itself staged by every commit, and the hook scans added lines.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".githooks" / "pre-commit"
LOCAL_CHECKS = REPO_ROOT / ".githooks" / "local-checks.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "replay"

#: The documentation ranges (RFC 5737). No other IPv4 address may appear.
DOCUMENTATION_NETS = tuple(
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def _stub_gitleaks(bin_dir: Path) -> None:
    """A gitleaks that passes, for machines without one: the pattern layer is
    what this test is about, and the hook hard-fails on a missing scanner."""
    stub = bin_dir / "gitleaks"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)


def _run_hook(tmp_path: Path, files: dict[str, bytes]) -> subprocess.CompletedProcess:
    repo = tmp_path / "scan"
    (repo / ".githooks").mkdir(parents=True)
    shutil.copy(HOOK, repo / ".githooks" / "pre-commit")
    if LOCAL_CHECKS.is_file():
        shutil.copy(LOCAL_CHECKS, repo / ".githooks" / "local-checks.sh")
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if shutil.which("gitleaks") is None and not (Path.home() / ".local/bin/gitleaks").exists():
        _stub_gitleaks(bin_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    git = ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
           "user.email=fixture@host-a.example"]
    subprocess.run(git + ["init", "-q"], check=True, env=env)
    # Only the fixtures are staged: the hook and the local checks carry the very
    # patterns they look for.
    subprocess.run(git + ["add", "--", *files], check=True, env=env)
    return subprocess.run(["bash", str(repo / ".githooks" / "pre-commit")], cwd=repo,
                          env=env, capture_output=True, text=True)


pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="the pre-commit hook needs git and bash")


def test_fixtures_pass_the_publication_checks(tmp_path):
    files = {str(p.relative_to(REPO_ROOT)): p.read_bytes() for p in _fixture_files()}
    assert any("case-2026-09-23" in name for name in files)
    result = _run_hook(tmp_path, files)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_publication_check_is_live(tmp_path):
    """The same run, with one tailnet-range address planted, is blocked."""
    planted = ".".join(["100", str(64 + 37), "0", "7"])
    files = {str(p.relative_to(REPO_ROOT)): p.read_bytes() for p in _fixture_files()}
    files["tests/fixtures/replay/planted.json"] = (
        '{"instance": "' + planted + ':9100"}\n').encode()
    result = _run_hook(tmp_path, files)
    assert result.returncode != 0
    assert "tailnet IP" in result.stdout + result.stderr


def test_every_fixture_address_is_a_documentation_address():
    """Stricter than the hook's tailnet range: any IPv4 address in a replay
    fixture must be RFC 5737, and every hostname a placeholder."""
    for path in _fixture_files():
        text = path.read_text(encoding="utf-8")
        for match in _IPV4.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(1))
            except ValueError:
                continue  # a version string such as 1.2.3.4 cannot parse past 255
            assert any(address in net for net in DOCUMENTATION_NETS), (path, match.group(1))
        for host in re.findall(r"https?://([a-z0-9.-]+)", text):
            assert host.endswith(".example") or host == "hulsman.dev" or \
                host.endswith(".hulsman.dev"), (path, host)
