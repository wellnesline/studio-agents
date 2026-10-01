"""Tests for deterministic blogfactory B-policy resolution and A floor."""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml
from jsonschema import ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio-agents" / "agents"))

from blogfactory.policy_resolver import resolve_policy
from blogfactory.safety_floor import SafetyFloorValidator

POLICY_DIR = ROOT / "studio-agents" / "agents" / "blogfactory" / "policy"


def load_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def neutral() -> dict:
    return load_yaml(POLICY_DIR / "neutral-defaults.yaml")


def resolve(neutral: dict, **layers) -> dict:
    return resolve_policy(neutral=neutral, preset=layers.get("preset"),
                          tenant=layers.get("tenant"), campaign=layers.get("campaign"),
                          tenant_id=layers.get("tenant_id", "tenant-123"),
                          resolved_at=layers.get("resolved_at"))


def test_neutral_only_is_the_neutral_effective_policy(neutral):
    effective = resolve(neutral)["effective_policy"]
    assert effective["persona"]["voice_default"] == "unspecified"
    assert effective["generation"] == {"mode": "unspecified", "target_chars": None}
    assert effective["research"]["external"] == "disabled"
    assert effective["publication"]["targets"] == ["export"]
    assert all(item["requirement"] == "report" for item in effective["bias"].values())
    assert effective["approval"]["minimum_approvers"] == 1


def test_personal_brand_preset_changes_only_its_named_diff(neutral):
    preset = load_yaml(POLICY_DIR / "presets" / "personal-brand.yaml")
    result = resolve(neutral, preset=preset)
    assert result["preset"] == {"id": "personal-brand", "version": 1}
    assert result["effective_policy"]["persona"]["voice_default"] == "first_person"
    assert result["effective_policy"]["research"]["external"] == "disabled"


def test_tenant_then_campaign_overrides_b_fields(neutral):
    result = resolve(neutral,
        tenant={"tenant_id": "tenant-123", "persona": {"voice_default": "third_person"},
                "research": {"external": "allowed"}},
        campaign={"persona": {"voice_default": "attributed"}, "research": {"external": "required"}})
    assert result["effective_policy"]["persona"]["voice_default"] == "attributed"
    assert result["effective_policy"]["research"]["external"] == "required"


@pytest.mark.parametrize("bad_layer", [
    {"approval": {"required": False}},
    {"safety": {"allow_unverified_claims": True}},
    {"publication": {"auto_publish": True}},
    {"generation": {"unexpected": True}},
])
def test_unknown_a_fields_are_rejected(neutral, bad_layer):
    with pytest.raises(ValidationError):
        resolve(neutral, tenant=bad_layer)


def test_campaign_cannot_supply_tenant_id(neutral):
    with pytest.raises(ValueError, match="tenant_id"):
        resolve(neutral, campaign={"tenant_id": "tenant-123"})


def test_minimum_approvers_zero_is_rejected_by_safety_floor():
    with pytest.raises(ValueError, match="minimum_approvers"):
        SafetyFloorValidator().enforce({"tenant_id": "tenant-123", "approval": {"minimum_approvers": 0},
                                        "publication": {"targets": ["export"]}})


def test_preset_attempting_a_change_is_rejected(neutral):
    with pytest.raises(ValidationError):
        resolve(neutral, preset={"id": "bad", "version": 1, "publication": {"auto_publish": True}})


def test_tenant_id_mismatch_is_rejected(neutral):
    with pytest.raises(ValueError, match="tenant_id"):
        resolve(neutral, tenant={"tenant_id": "other"})


def test_resolved_at_is_none_when_unspecified(neutral):
    assert resolve(neutral)["resolved_at"] is None


def test_hashes_use_normalized_json_and_are_deterministic(neutral):
    tenant = {"tenant_id": "tenant-123", "research": {"external": "allowed"}}
    first = resolve(neutral, tenant=tenant)
    second = resolve(neutral, tenant={"research": {"external": "allowed"}, "tenant_id": "tenant-123"})
    assert first["tenant_policy_sha256"] == second["tenant_policy_sha256"]
    assert first["campaign_override_sha256"] is None
