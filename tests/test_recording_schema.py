"""The committed triage-recording schema and its fixtures (group 10, task 10.1).

From `specs/triage-replay` (*Every triage is recorded to the audit volume*:
"Recordings SHALL validate against a versioned recording schema committed to the
repository"; *Replay test fixtures carry no real homelab data*) and design D13. The
fixtures under `tests/fixtures/replay/recordings/` are placeholders only (standing
rule 1): `host-a.example`, `example-a.service`, RFC 5737 addresses.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

from henk.replay.recorder import RECORDING_SCHEMA, RECORDING_SCHEMA_PATH

SCHEMA = json.loads(RECORDING_SCHEMA_PATH.read_text())
FIXTURES = Path(__file__).parent / "fixtures" / "replay" / "recordings"
FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _invalid(record: dict) -> bool:
    try:
        jsonschema.validate(record, SCHEMA)
    except jsonschema.ValidationError:
        return True
    return False


def test_the_schema_is_committed_and_versioned():
    assert RECORDING_SCHEMA_PATH.name == "triage-recording.v1.schema.json"
    assert RECORDING_SCHEMA == "henk.triage-recording.v1"
    assert SCHEMA["properties"]["schema"] == {"const": RECORDING_SCHEMA}
    jsonschema.Draft7Validator.check_schema(SCHEMA)


def test_there_are_fixtures_for_each_shape():
    assert {p.name for p in FIXTURE_FILES} >= {
        "live-completed.json", "live-denied.json", "live-errored.json",
        "live-truncated-with-reference.json",
    }


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_the_recording_schema_validates_the_fixtures(path: Path):
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixtures_carry_no_real_homelab_data(path: Path):
    text = path.read_text()
    assert not re.search(r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d+\.\d+", text)
    for address in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        assert address.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), address
    assert not re.search(r"\+\d{9,}", text.replace("+31600000000", ""))
    for host in re.findall(r"\b[a-z0-9-]+\.(?:example|service)\b", text):
        assert host.startswith(("host-", "example-")), host


# --- What the schema refuses ------------------------------------------------------


def _mutated(name: str, change) -> dict:
    record = copy.deepcopy(_load(name))
    change(record)
    return record


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.pop("content"),
        lambda r: r.pop("transcript"),
        lambda r: r.pop("ending"),
        lambda r: r.pop("reconstructed"),
        lambda r: r.pop("hashes"),
        lambda r: r.pop("complete"),
        lambda r: r.update(schema="henk.triage-recording.v2"),
        lambda r: r.update(reconstructed="no"),
        lambda r: r.update(recording_id="../escape"),
        lambda r: r.update(unexpected_field=1),
        lambda r: r.update(incidents=[]),
        lambda r: r["ending"].update(outcome="fine"),
        lambda r: r["ending"].update(http_status=42),
        lambda r: r["profile"].update(name="judge"),
        lambda r: r["hashes"].update(system_prompt="md5:abc"),
        lambda r: r["transcript"][0].pop("result"),
        lambda r: r["transcript"][0].update(arguments="unknown"),
        lambda r: r["transcript"][0].update(extra=True),
        lambda r: r.update(complete=False),  # incomplete, with no reason given
        lambda r: r.update(incomplete_reasons=["truncated"]),  # complete, with a reason
        lambda r: r.update(prior_handoff_ids=["3"]),
        lambda r: r.update(reference={"branch": "b"}),
    ],
)
def test_the_schema_refuses_malformed_recordings(change):
    assert _invalid(_mutated("live-completed.json", change))


def test_a_truncated_call_removed_at_least_one_byte():
    record = _mutated("live-truncated-with-reference.json",
                      lambda r: r["transcript"][0].update(truncated_bytes=0))
    assert _invalid(record)
    assert not _invalid(_load("live-truncated-with-reference.json"))


def test_a_truncated_call_requires_the_truncated_reason():
    record = _mutated("live-truncated-with-reference.json",
                      lambda r: r.update(incomplete_reasons=["transcript-unavailable"]))
    assert _invalid(record)


def test_a_reconstructed_recording_may_carry_unknown_arguments():
    # The group 12b seam: a rebuilt case keeps tool names with arguments unknown.
    record = _mutated("live-completed.json", lambda r: r.update(reconstructed=True))
    record["transcript"][0]["arguments"] = "unknown"
    record["transcript"][0]["result"] = None
    record["hashes"] = {"system_prompt": None, "tool_definitions": None}
    jsonschema.validate(record, SCHEMA)


def test_the_reference_is_optional_and_complete_when_present():
    jsonschema.validate(_load("live-completed.json"), SCHEMA)
    assert "reference" not in _load("live-completed.json")
    reference = _load("live-truncated-with-reference.json")["reference"]
    assert set(reference) == {"branch", "culprit", "mechanism", "fix"}
