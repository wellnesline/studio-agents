"""Strict, local source-registry support for blogfactory provenance checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SOURCE_REGISTRY_SCHEMA_VERSION = "blogfactory-source-registry/v1"
VOICE_MODES = ("first_person", "third_person", "attributed", "unspecified")

_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "source_id", "tenant_id", "kind", "uri", "locator", "sha256",
        "approved_for_publication", "allowed_voice", "approved_by", "approval_id", "expires_at",
    ],
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "tenant_id": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "minLength": 1},
        "owner_person_id": {"type": ["string", "null"], "minLength": 1},
        "owner_org_id": {"type": ["string", "null"], "minLength": 1},
        "uri": {"type": "string", "minLength": 1},
        "locator": {"type": ["string", "null"]},
        "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
        "approved_for_publication": {"type": "boolean"},
        "allowed_voice": {"type": "array", "minItems": 1,
                          "uniqueItems": True, "items": {"enum": list(VOICE_MODES)}},
        "approved_by": {"type": "string", "minLength": 1},
        "approval_id": {"type": "string", "minLength": 1},
        "expires_at": {"type": ["string", "null"]},
    },
    "anyOf": [
        {"required": ["owner_person_id"], "properties": {"owner_person_id": {"type": "string", "minLength": 1}}},
        {"required": ["owner_org_id"], "properties": {"owner_org_id": {"type": "string", "minLength": 1}}},
    ],
}

SOURCE_REGISTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "sources"],
    "properties": {
        "schema_version": {"const": SOURCE_REGISTRY_SCHEMA_VERSION},
        "sources": {"type": "array", "items": _RECORD_SCHEMA},
    },
}


def _parse_expiry(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


class SourceRegistry:
    """A validated in-memory source registry; it never performs network access."""

    def __init__(self, document: dict[str, Any]):
        Draft202012Validator(SOURCE_REGISTRY_SCHEMA).validate(document)
        self.document = document
        self._sources = {record["source_id"]: record for record in document["sources"]}
        if len(self._sources) != len(document["sources"]):
            raise ValueError("source-registry contains duplicate source_id values")
        for record in document["sources"]:
            _parse_expiry(record["expires_at"])

    @classmethod
    def load(cls, path: str | Path) -> "SourceRegistry":
        with Path(path).open(encoding="utf-8") as fh:
            return cls(json.load(fh))

    def find(self, source_id: str) -> dict[str, Any] | None:
        return self._sources.get(source_id)

    @staticmethod
    def is_tenant_match(source: dict[str, Any], tenant_id: str) -> bool:
        return source.get("tenant_id") == tenant_id

    @staticmethod
    def is_approved(source: dict[str, Any]) -> bool:
        return source.get("approved_for_publication") is True

    @staticmethod
    def is_unexpired(source: dict[str, Any], *, now: datetime | None = None) -> bool:
        expiry = _parse_expiry(source.get("expires_at"))
        return expiry is None or expiry > (now or datetime.now(timezone.utc))

    @staticmethod
    def allows_voice(source: dict[str, Any], voice_mode: str) -> bool:
        return voice_mode in source.get("allowed_voice", [])

    @staticmethod
    def has_locator(source: dict[str, Any]) -> bool:
        return bool(source.get("locator") and source["locator"].strip())

    @staticmethod
    def owner_matches_voice(source: dict[str, Any], voice_mode: str) -> bool:
        """Check the owner type possible with the plan's selected voice.

        plan v2.1 has no narrator/attribution-subject ID, so exact identity
        comparison is intentionally impossible here (# 未確認 in the design).
        """
        if voice_mode == "first_person":
            return bool(source.get("owner_person_id"))
        if voice_mode == "third_person":
            return bool(source.get("owner_org_id"))
        if voice_mode == "attributed":
            return bool(source.get("owner_person_id") or source.get("owner_org_id"))
        return False
