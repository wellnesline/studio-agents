"""Coverage for the v2.1 deterministic provenance safety floor."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio-agents" / "agents"))

from blogfactory.provenance_gate import HARD_BLOCK, check
from blogfactory.source_registry import SourceRegistry


def source(*, tenant="tenant-a", approved=True, voices=None, expires_at=None, locator="section 2"):
    return {
        "source_id": "src-1", "tenant_id": tenant, "kind": "internal_record",
        "owner_person_id": "person-1", "owner_org_id": None, "uri": "file:///records/a.md",
        "locator": locator, "sha256": "a" * 64, "approved_for_publication": approved,
        "allowed_voice": voices or ["first_person"], "approved_by": "editor-1",
        "approval_id": "APR-1", "expires_at": expires_at,
    }


def registry(record=None):
    return SourceRegistry({"schema_version": "blogfactory-source-registry/v1", "sources": [record or source()]})


def plan(*, source_ref="src-1", source_refs=None, voice="first_person", text="私は導入した"):
    return {
        "schema_version": "blogfactory-plan/v1", "topics": [{
            "topic_id": "topic-1", "title": "題", "audience_intent": "知る", "search_intent": "informational",
            "core_claim": "主張", "lesson_id": "lesson-1", "conclusion": "結論", "content_type": "article",
            "voice_mode": voice, "experience_claims": [{"text": text, "source_ref": source_ref}],
            "source_refs": source_refs if source_refs is not None else ([source_ref] if source_ref else []),
            "risk_tags": [], "limitation_or_counterexample": None,
        }],
    }


def gate(article, *, p=None, r=None):
    return check(article_md=article, plan=p, registry=r or registry(),
                 effective_policy={"tenant_id": "tenant-a", "bias": {"grounding": {"requirement": "off"}}},
                 campaign_tenant_id="tenant-a")


def blocks(findings):
    return [item for item in findings if item.severity == HARD_BLOCK]


def test_approved_first_person_experience_with_all_six_conditions_passes():
    assert gate("私は導入した。", p=plan()) == []


def test_experience_claim_without_source_ref_blocks():
    findings = gate("私は導入した。", p=plan(source_ref=None))
    assert any(item.type == "experience_provenance" and "source_ref" in item.message for item in findings)


def test_source_tenant_mismatch_blocks():
    findings = gate("私は導入した。", p=plan(), r=registry(source(tenant="tenant-b")))
    assert any("tenant_id" in item.message for item in findings)


def test_unapproved_source_blocks():
    findings = gate("私は導入した。", p=plan(), r=registry(source(approved=False)))
    assert any("公開承認" in item.message for item in findings)


def test_disallowed_voice_blocks():
    findings = gate("私は導入した。", p=plan(), r=registry(source(voices=["third_person"])))
    assert any("allowed_voice" in item.message for item in findings)


def test_expired_source_blocks():
    findings = gate("私は導入した。", p=plan(), r=registry(source(expires_at="2000-01-01T00:00:00+00:00")))
    assert any("期限" in item.message for item in findings)


def test_source_without_locator_blocks():
    findings = gate("私は導入した。", p=plan(), r=registry(source(locator=None)))
    assert any("locator" in item.message for item in findings)


def test_owner_type_must_match_selected_voice():
    findings = gate("同社は導入した。", p=plan(voice="third_person", text="同社は導入した"),
                    r=registry(source(voices=["third_person"])))
    assert any("owner" in item.message for item in findings)


def test_unsupported_high_risk_number_blocks_even_when_grounding_is_off():
    findings = gate("通過率40%でした。", p=None)
    assert any(item.type == "unsupported_high_risk_rate" for item in findings)


def test_unplanned_third_person_result_blocks():
    findings = gate("同社は導入して通過率40%を達成した。", p=None)
    assert any(item.type == "third_person_experience" for item in findings)
