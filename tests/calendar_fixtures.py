"""Shared fixtures for the maintenance-calendar tests.

Every key here is generated per test run and never written to disk, and every
identity is a placeholder (tasks.md standing rule 1): the real calendar id, the
owner's Google account and the service-account emails live only in rp5's config
and `.env`.
"""

from __future__ import annotations

import base64
import functools
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SA_EMAIL = "henk@example-project.iam.gserviceaccount.com"
OWNER_EMAIL = "owner@hulsman.dev"
PEER_EMAIL = "pam@example-project.iam.gserviceaccount.com"
STRANGER_EMAIL = "stranger@example.invalid"
CALENDAR_ID = "placeholder-calendar@group.calendar.google.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ENV_VAR = "GOOGLE_CALENDAR_SA_KEY_B64"


@functools.lru_cache(maxsize=None)
def rsa_key(slot: int = 0) -> rsa.RSAPrivateKey:
    """A fresh RSA key per run (and per slot), cached for the run only."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_pem(slot: int = 0) -> str:
    return rsa_key(slot).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_pem(slot: int = 0) -> str:
    return rsa_key(slot).public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


class _Drop:
    def __repr__(self) -> str:
        return "DROP"


#: Pass as an override value to remove that key from the document.
DROP = _Drop()


def key_document(**overrides: Any) -> dict[str, Any]:
    """The shape of a downloaded service-account JSON key, with placeholders."""
    document: dict[str, Any] = {
        "type": "service_account",
        "project_id": "example-project",
        "private_key_id": "0000000000000000000000000000000000000000",
        "private_key": private_pem(),
        "client_email": SA_EMAIL,
        "client_id": "000000000000000000000",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URI,
    }
    for name, value in overrides.items():
        if value is DROP:
            document.pop(name, None)
        else:
            document[name] = value
    return document


def encode(document: Any) -> str:
    raw = document if isinstance(document, (bytes, str)) else json.dumps(document)
    if isinstance(raw, str):
        raw = raw.encode()
    return base64.b64encode(raw).decode()


def key_b64(**overrides: Any) -> str:
    return encode(key_document(**overrides))


def private_key_lines(slot: int = 0) -> list[str]:
    """Every line of the PEM, header included: none may appear in any error."""
    return [line for line in private_pem(slot).splitlines() if line.strip()]


def minimal_raw(**calendar: Any) -> dict[str, Any]:
    """A loadable config with the calendar section set to ``calendar``."""
    raw: dict[str, Any] = {
        "owner": {"id": "+31600000000", "timezone": "Europe/Amsterdam"},
        "signal": {"bridge_url": "http://b", "account": "+1"},
        "endpoints": {
            "gatus": {"base_url": "http://g"},
            "prometheus": {"base_url": "http://p"},
            "taiga": {"base_url": "http://t"},
            "todo": {"base_url": "http://d"},
            "ntfy": {"base_url": "http://n", "topic": "henk"},
        },
    }
    if calendar:
        raw["calendar"] = dict(calendar)
    return raw


def enabled_raw(**calendar: Any) -> dict[str, Any]:
    settings = {"enabled": True, "calendar_id": CALENDAR_ID}
    settings.update(calendar)
    return minimal_raw(**settings)
