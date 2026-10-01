"""Deterministic neutral → preset → tenant → campaign policy resolution."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .policy_schema import SCHEMA_VERSION, validate_policy_layer, validate_preset
from .safety_floor import SAFETY_FLOOR_VERSION, SafetyFloorValidator

RESOLVER_VERSION = "1.0.0"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _sha256(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_tenant_identity(layer: dict[str, Any] | None, tenant_id: str, name: str) -> None:
    if layer is not None and "tenant_id" in layer and layer["tenant_id"] != tenant_id:
        raise ValueError(f"{name} tenant_id does not match resolver tenant_id")


def resolve_policy(*, neutral: dict, preset: dict | None, tenant: dict | None,
                   campaign: dict | None, tenant_id: str,
                   resolved_at: str | None = None) -> dict:
    """Resolve B policy layers and return the serializable effective-policy record."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    validate_policy_layer(neutral, layer="neutral")
    if preset is not None:
        validate_preset(preset)
    if tenant is not None:
        validate_policy_layer(tenant, layer="tenant")
        _validate_tenant_identity(tenant, tenant_id, "tenant")
    if campaign is not None:
        validate_policy_layer(campaign, layer="campaign")
        # Campaign is B-only; its tenant isolation key is not overrideable.
        if "tenant_id" in campaign:
            raise ValueError("campaign must not override tenant_id")

    effective = _deep_merge({}, neutral)
    # schema_version/preset in source layers are record metadata, not B settings.
    effective.pop("schema_version", None)
    effective.pop("tenant_id", None)
    effective.pop("preset", None)
    preset_metadata = None
    if preset is not None:
        preset_metadata = {"id": preset["id"], "version": preset["version"]}
        effective = _deep_merge(effective, {k: v for k, v in preset.items() if k not in {"id", "version"}})
    if tenant is not None:
        effective = _deep_merge(effective, {k: v for k, v in tenant.items() if k not in {"schema_version", "tenant_id", "preset"}})
        if tenant.get("preset") is not None:
            preset_metadata = copy.deepcopy(tenant["preset"])
    if campaign is not None:
        effective = _deep_merge(effective, {k: v for k, v in campaign.items() if k != "schema_version"})
    effective["tenant_id"] = tenant_id

    if (effective.get("approval", {}).get("final_unit") == "wave"
            and effective["approval"].get("review_wave_size") is None):
        raise ValueError("approval.review_wave_size is required when final_unit is wave")
    reporting = effective.get("reporting", {})
    if (reporting.get("quality_positioning") == "custom_reviewed"
            and not reporting.get("custom_copy_ref")):
        raise ValueError("reporting.custom_copy_ref is required for custom_reviewed")

    SafetyFloorValidator().enforce(effective)
    return {
        "schema_version": SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "preset": preset_metadata,
        "tenant_policy_sha256": _sha256(tenant),
        "campaign_override_sha256": _sha256(campaign),
        "resolver_version": RESOLVER_VERSION,
        "resolved_at": resolved_at,
        "effective_policy": effective,
        "safety_floor_version": SAFETY_FLOOR_VERSION,
    }
