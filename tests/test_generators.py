"""Tests for the injectable Blog Agent adapter; no LLM or network is used."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio-agents" / "agents"))

from blogfactory.generators import make_blog_agent_generator
from blogfactory.runner import REVIEW_READY, run_campaign
from blogfactory.source_registry import SourceRegistry


def _topic() -> dict:
    return {"topic_id": "topic-1", "title": "記録にもとづく題", "audience_intent": "知る",
            "search_intent": "informational", "core_claim": "記録を確認して判断する", "lesson_id": "lesson-1",
            "conclusion": "根拠を確認する", "content_type": "article", "voice_mode": "first_person",
            "experience_claims": [{"text": "私は導入した", "source_ref": "good"}], "source_refs": ["good"],
            "risk_tags": [], "limitation_or_counterexample": None}


def _record(source_id: str, *, tenant: str = "tenant-a", approved: bool = True) -> dict:
    return {"source_id": source_id, "tenant_id": tenant, "kind": "internal_record",
            "owner_person_id": "person-1", "owner_org_id": None, "uri": f"file:///records/{source_id}.md",
            "locator": "section 1", "sha256": "a" * 64, "approved_for_publication": approved,
            "allowed_voice": ["first_person"], "approved_by": "editor", "approval_id": f"SRC-{source_id}",
            "expires_at": None}


def _registry(*records: dict) -> SourceRegistry:
    return SourceRegistry({"schema_version": "blogfactory-source-registry/v1", "sources": list(records)})


def _policy() -> dict:
    return {"tenant_id": "tenant-a", "persona": {"voice_default": "unspecified"},
            "publication": {"targets": ["export"]},
            "bias": {name: {"requirement": "report"} for name in
                     ("lesson_diversity", "framing", "advertorial", "similarity", "grounding", "search_intent")}}


def test_generator_returns_article_without_llm_or_network(monkeypatch):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network called"))
    calls = []
    generator = make_blog_agent_generator(
        registry=_registry(_record("good")), source_loader=lambda source: "承認済み本文",
        build_fn=lambda text, directives: calls.append((text, directives)) or "# 記録にもとづく題\n\n私は導入した。",
    )
    assert generator(_topic(), _policy()) == {"id": "topic-1", "article_md": "# 記録にもとづく題\n\n私は導入した。"}
    assert calls[0][0] == "承認済み本文"
    assert calls[0][1]["voice_mode"] == "first_person"
    assert calls[0][1]["core_claim"] == "記録を確認して判断する"
    assert calls[0][1]["llm_backend"] == "codex"
    assert calls[0][1]["mode"] == "faithful"
    assert calls[0][1]["target"] is None


def test_generator_passes_explicit_seo_generation_policy_to_build_fn():
    calls = []
    policy = _policy()
    policy["generation"] = {"mode": "seo_long", "target_chars": 3000}
    generator = make_blog_agent_generator(
        registry=_registry(_record("good")), source_loader=lambda source: "承認済み本文",
        build_fn=lambda text, directives: calls.append((text, directives)) or "# 記録にもとづく題",
    )

    generator(_topic(), policy)

    assert calls[0][1]["mode"] == "seo_long"
    assert calls[0][1]["target"] == 3000


def test_generator_excludes_wrong_tenant_and_unapproved_sources():
    topic = _topic(); topic["source_refs"] = ["good", "other-tenant", "unapproved"]
    passed = []
    generator = make_blog_agent_generator(
        registry=_registry(_record("good"), _record("other-tenant", tenant="tenant-b"),
                           _record("unapproved", approved=False)),
        source_loader=lambda source: passed.append(source["source_id"]) or source["source_id"],
        build_fn=lambda text, directives: "# 記録にもとづく題\n\n私は導入した。",
    )
    generator(topic, _policy())
    assert passed == ["good"]


def test_mocked_blog_agent_generator_reaches_review_ready(monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network called"))
    topic, registry, policy = _topic(), _registry(_record("good")), _policy()
    (tmp_path / "plan.json").write_text(json.dumps({"schema_version": "blogfactory-plan/v1", "topics": [topic]}), encoding="utf-8")
    generator = make_blog_agent_generator(
        registry=registry, source_loader=lambda source: "私は導入した",
        build_fn=lambda text, directives: "# 記録にもとづく題\n\n私は導入した。",
    )
    result = run_campaign(campaign_dir=tmp_path, generator=generator,
                          resolved_policy={"schema_version": "blogfactory-policy/v1", "effective_policy": policy},
                          registry=registry)
    assert result["state"] == REVIEW_READY
