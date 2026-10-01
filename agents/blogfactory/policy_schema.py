"""Strict schemas for the blogfactory-policy/v1 B-policy surface."""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "blogfactory-policy/v1"


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


_nullable_string = {"type": ["string", "null"]}
_rate = {"type": ["number", "null"], "minimum": 0, "maximum": 1}
_requirement = {"type": "string", "enum": ["off", "report", "review_required"]}

_BIAS_ITEM = _object({
    "requirement": _requirement,
    "max_single_share": _rate,
    "max_single_stance_share": _rate,
    "limitation_min_rate": _rate,
    "cta_max_same_objective_share": _rate,
    "review_at": _rate,
    "external_article_min_rate": _rate,
    "overlap_review_at": _rate,
})

# The individual bias objects are deliberately narrower than the reusable helper.
_BIAS = _object({
    "lesson_diversity": _object({"requirement": _requirement, "max_single_share": _rate}),
    "framing": _object({"requirement": _requirement, "max_single_stance_share": _rate}),
    "advertorial": _object({"requirement": _requirement, "limitation_min_rate": _rate,
                              "cta_max_same_objective_share": _rate}),
    "similarity": _object({"requirement": _requirement, "review_at": _rate}),
    "grounding": _object({"requirement": _requirement, "external_article_min_rate": _rate}),
    "search_intent": _object({"requirement": _requirement, "overlap_review_at": _rate}),
})

# This validates a layer, so all B fields are optional.  The resolver builds the
# complete effective policy from neutral defaults before enforcing the safety floor.
POLICY_LAYER_SCHEMA: dict[str, Any] = _object({
    "schema_version": {"const": SCHEMA_VERSION},
    "tenant_id": {"type": "string", "minLength": 1},
    "preset": {"oneOf": [
        {"type": "null"},
        _object({"id": {"type": "string", "minLength": 1},
                 "version": {"type": "integer", "minimum": 1}}),
    ]},
    "persona": _object({
        "voice_default": {"type": "string", "enum": ["unspecified", "first_person", "third_person", "attributed"]},
        "persona_ref": _nullable_string,
    }),
    "brand": _object({"profile_ref": _nullable_string}),
    "generation": _object({
        "mode": {"type": "string", "enum": ["unspecified", "faithful", "seo_long"]},
        "target_chars": {"type": ["integer", "null"]},
    }),
    "research": _object({"external": {"type": "string", "enum": ["disabled", "allowed", "required"]}}),
    "publication": _object({"targets": {"type": "array", "items": {"type": "string", "minLength": 1}}}),
    "approval": _object({
        "plan_review": {"type": "string", "enum": ["optional", "required"]},
        "final_unit": {"type": "string", "enum": ["article", "wave", "campaign"]},
        "review_wave_size": {"type": ["integer", "null"], "minimum": 1},
        "approver_roles": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "minimum_approvers": {"type": "integer", "minimum": 1},
    }),
    "bias": _BIAS,
    "reporting": _object({
        "quality_positioning": {"type": "string", "enum": ["configured_visibility", "custom_reviewed"]},
        "custom_copy_ref": _nullable_string,
    }),
})

# Presets are named, versioned B-only diffs.  No identifier is inferred from a
# filename, and strict validation rejects any attempted A setting.
PRESET_SCHEMA: dict[str, Any] = _object({
    "id": {"type": "string", "minLength": 1},
    "version": {"type": "integer", "minimum": 1},
    "persona": POLICY_LAYER_SCHEMA["properties"]["persona"],
    "brand": POLICY_LAYER_SCHEMA["properties"]["brand"],
    "generation": POLICY_LAYER_SCHEMA["properties"]["generation"],
    "research": POLICY_LAYER_SCHEMA["properties"]["research"],
    "publication": POLICY_LAYER_SCHEMA["properties"]["publication"],
    "approval": POLICY_LAYER_SCHEMA["properties"]["approval"],
    "bias": _BIAS,
    "reporting": POLICY_LAYER_SCHEMA["properties"]["reporting"],
})


def validate_policy_layer(policy: dict[str, Any], *, layer: str) -> None:
    """Raise ValidationError unless a policy layer uses only defined B fields."""
    if not isinstance(policy, dict):
        raise ValueError(f"{layer} policy must be an object")
    Draft202012Validator(POLICY_LAYER_SCHEMA).validate(policy)


def validate_preset(preset: dict[str, Any]) -> None:
    if not isinstance(preset, dict):
        raise ValueError("preset must be an object")
    Draft202012Validator(PRESET_SCHEMA).validate(preset)
