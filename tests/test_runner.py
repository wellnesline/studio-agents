"""End-to-end deterministic coverage for the thin blogfactory runner."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio-agents" / "agents"))

from blogfactory.publisher import ExportAdapter
from blogfactory.runner import BIAS_BLOCKED, PUBLISHED, REVIEW_READY, publish_campaign, run_campaign
from blogfactory.source_registry import SourceRegistry
from blogfactory import cli


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _topic() -> dict:
    return {"topic_id": "topic-1", "title": "検証済みの題", "audience_intent": "知る",
            "search_intent": "informational", "core_claim": "記録を確認して判断する",
            "lesson_id": "lesson-1", "conclusion": "根拠を確認する", "content_type": "article",
            "voice_mode": "first_person", "experience_claims": [{"text": "私は導入した", "source_ref": "src-1"}],
            "source_refs": ["src-1"], "risk_tags": [], "limitation_or_counterexample": None}


def _registry() -> SourceRegistry:
    return SourceRegistry({"schema_version": "blogfactory-source-registry/v1", "sources": [{
        "source_id": "src-1", "tenant_id": "tenant-a", "kind": "internal_record", "owner_person_id": "person-1",
        "owner_org_id": None, "uri": "file:///records/a.md", "locator": "section 1", "sha256": "a" * 64,
        "approved_for_publication": True, "allowed_voice": ["first_person"], "approved_by": "editor",
        "approval_id": "SRC-1", "expires_at": None,
    }]})


def _policy() -> dict:
    return {"schema_version": "blogfactory-policy/v1", "effective_policy": {"tenant_id": "tenant-a",
            "publication": {"targets": ["export"]}, "bias": {name: {"requirement": "report"} for name in
            ("lesson_diversity", "framing", "advertorial", "similarity", "grounding", "search_intent")}}}


def _campaign(tmp_path: Path) -> Path:
    _write(tmp_path / "plan.json", {"schema_version": "blogfactory-plan/v1", "topics": [_topic()]})
    _write(tmp_path / "source-registry.json", _registry().document)
    return tmp_path


def _good(topic: dict, policy: dict) -> dict:
    return {"id": topic["topic_id"], "article_md": "# 検証済みの題\n\n私は導入した。"}


def _bad(topic: dict, policy: dict) -> dict:
    return {"id": topic["topic_id"], "article_md": "# 検証済みの題\n\n私は運用して改善した。"}


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _approval(root: Path) -> dict:
    article = {"id": "topic-1", "article_md": (root / "articles/topic-1/article.md").read_text(encoding="utf-8")}
    adapter = ExportAdapter(); post = adapter.prepare(article, "export")
    resolved, report = json.loads((root / "effective-policy.json").read_text(encoding="utf-8")), json.loads((root / "cluster-report.json").read_text(encoding="utf-8"))
    return {"tenant_id": "tenant-a", "approval_id": "APR-1", "unit": "campaign",
            "article_hashes": [post.content_sha256], "publication_targets": ["export"],
            "effective_policy_sha256": _hash(resolved), "review_report_sha256": _hash(report),
            "approved_by": ["editor"], "approved_at": "2000-01-01T00:00:00Z", "decision": "approved"}


def test_run_ready_renders_audit_and_review_package_without_network(monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network called"))
    root = _campaign(tmp_path)
    result = run_campaign(campaign_dir=root, generator=_good, resolved_policy=_policy(), registry=_registry())
    assert result["state"] == REVIEW_READY
    assert (root / "cluster-report.json").is_file() and (root / "review-package.html").is_file()
    assert json.loads((root / "campaign.json").read_text(encoding="utf-8"))["state"] == REVIEW_READY


def test_missing_provenance_blocks_before_audit_or_publish(tmp_path):
    root = _campaign(tmp_path)
    assert run_campaign(campaign_dir=root, generator=_bad, resolved_policy=_policy(), registry=_registry())["state"] == BIAS_BLOCKED
    assert not (root / "cluster-report.json").exists()
    with pytest.raises(ValueError, match="REVIEW_READY"):
        publish_campaign(campaign_dir=root, adapter=ExportAdapter(), approval_artifact={})


def test_publish_requires_matching_human_approval_and_writes_bundle(tmp_path):
    root = _campaign(tmp_path)
    run_campaign(campaign_dir=root, generator=_good, resolved_policy=_policy(), registry=_registry())
    result = publish_campaign(campaign_dir=root, adapter=ExportAdapter(), approval_artifact=_approval(root))
    assert result["state"] == PUBLISHED
    assert json.loads((root / "publication-receipts.json").read_text(encoding="utf-8"))["receipts"][0]["publication_bundle"]["approval_id"] == "APR-1"


def test_review_ready_cannot_publish_without_current_approval(tmp_path):
    root = _campaign(tmp_path)
    run_campaign(campaign_dir=root, generator=_good, resolved_policy=_policy(), registry=_registry())
    bad = _approval(root); bad["review_report_sha256"] = "changed"
    with pytest.raises(ValueError):
        publish_campaign(campaign_dir=root, adapter=ExportAdapter(), approval_artifact=bad)
    assert json.loads((root / "campaign.json").read_text(encoding="utf-8"))["state"] == REVIEW_READY


def test_cli_stub_run_is_local_and_has_no_llm_or_network_path(monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network called"))
    root = _campaign(tmp_path)
    _write(root / "effective-policy.json", _policy())
    cli.main(["run", str(root), "--stub"])
    assert json.loads((root / "campaign.json").read_text(encoding="utf-8"))["state"] == REVIEW_READY
