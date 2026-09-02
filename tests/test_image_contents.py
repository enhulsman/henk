"""Contract test for what the container image contains (``session-awareness`` 5.11).

The stamp writer set the precedent: host-side deploy artefacts live in this repo, are
tested by this suite, and are **absent from the image**. The session publisher is the
second of them, and it is the one where absence matters most — it runs on the
workstation with the system interpreter, it reads the live estate through two CLIs
that do not exist in the container, and its configuration names the owner's real
project roots. Shipping it inside the agent image would put a workstation-scoped tool
into the one process that must never hold workstation reach.

The Dockerfile's ``COPY`` set is the whole mechanism, so it is asserted directly
rather than inferred from a build.
"""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

#: Everything the image is built from. The runtime package, its metadata, and the
#: README `pyproject.toml` names as the long description — nothing else.
EXPECTED_COPY_SOURCES = {"pyproject.toml", "README.md", "henk"}


def _copy_instructions(text: str) -> list[list[str]]:
    """Every ``COPY`` instruction's arguments, with line continuations joined and
    flags (``--from=``, ``--chown=``) dropped."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending.strip())

    instructions: list[list[str]] = []
    for line in logical:
        head, _, rest = line.partition(" ")
        if head.upper() != "COPY":
            continue
        arguments = [token for token in rest.split() if not token.startswith("--")]
        instructions.append(arguments)
    return instructions


def test_the_dockerfile_copies_exactly_the_runtime_package() -> None:
    instructions = _copy_instructions(DOCKERFILE.read_text(encoding="utf-8"))
    assert instructions, "the parser must actually find the COPY instructions"
    sources: set[str] = set()
    for arguments in instructions:
        assert len(arguments) >= 2, arguments
        # The last argument is the destination inside the image.
        sources.update(arguments[:-1])
    assert sources == EXPECTED_COPY_SOURCES


def test_nothing_under_deploy_is_copied_into_the_image() -> None:
    """The session publisher and the stamp writer are host-side tools: the container
    must not carry either, and no future deploy artefact may arrive by accident."""
    instructions = _copy_instructions(DOCKERFILE.read_text(encoding="utf-8"))
    for arguments in instructions:
        for source in arguments[:-1]:
            normalised = source.lstrip("./")
            assert normalised != "deploy", source
            assert not normalised.startswith("deploy/"), source
    assert "deploy" not in DOCKERFILE.read_text(encoding="utf-8")


def test_the_publisher_is_not_reachable_from_the_installed_package() -> None:
    """A second route into the image would be an import from ``henk/`` — the package
    that *is* copied. There is none: the publisher is a standalone script."""
    package = DOCKERFILE.resolve().parent / "henk"
    offenders = [
        path
        for path in package.rglob("*.py")
        if "session_publisher" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
