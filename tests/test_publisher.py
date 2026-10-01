"""Approval binding and side-effect-free export coverage."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "studio-agents" / "agents"))

from blogfactory.approval import ApprovalExpiredError, VerifiedApprovalToken, verify_approval
from blogfactory.publisher import ExportAdapter, PublisherRegistry, TargetValidationError


def artifact(**overrides):
    value = {"tenant_id": "tenant-a", "approval_id": "APR-1", "unit": "campaign",
             "article_hashes": ["article-a", "article-b"], "publication_targets": ["export"],
             "effective_policy_sha256": "policy-1", "review_report_sha256": "review-1",
             "approved_by": ["editor-1"], "approved_at": "2000-01-01T00:00:00Z", "decision": "approved"}
    value.update(overrides)
    return value


def verified():
    return verify_approval(artifact=artifact(), article_hashes=["article-a", "article-b"],
                           effective_policy_sha256="policy-1", review_report_sha256="review-1",
                           publication_targets=["export"])


def test_correct_approval_issues_opaque_verified_token():
    assert isinstance(verified(), VerifiedApprovalToken)


@pytest.mark.parametrize(("field", "current"), [
    ("article_hashes", ["article-a", "changed"]),
    ("effective_policy_sha256", "changed-policy"),
    ("review_report_sha256", "changed-review"),
    ("publication_targets", ["other"]),
])
def test_changed_approval_binding_expires(field, current):
    kwargs = {"article_hashes": ["article-a", "article-b"], "effective_policy_sha256": "policy-1",
              "review_report_sha256": "review-1", "publication_targets": ["export"]}
    kwargs[field] = current
    with pytest.raises(ApprovalExpiredError):
        verify_approval(artifact=artifact(), **kwargs)


@pytest.mark.parametrize("bad", [{"decision": "rejected"}, {"approved_by": []}])
def test_unapproved_or_unattributed_artifact_expires(bad):
    with pytest.raises(ApprovalExpiredError):
        verify_approval(artifact=artifact(**bad), article_hashes=["article-a", "article-b"],
                        effective_policy_sha256="policy-1", review_report_sha256="review-1",
                        publication_targets=["export"])


def test_export_publish_rejects_unverified_values_and_never_networks(monkeypatch):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *args, **kwargs: pytest.fail("network called"))
    adapter = ExportAdapter()
    post = adapter.prepare({"id": "article-a", "article_md": "# Title\nBody"}, "export")
    draft = adapter.create_draft(post, credentials=None)
    for bad in ({}, None):
        with pytest.raises(TypeError):
            adapter.publish(draft, bad, credentials=None)
    token = verify_approval(
        artifact=artifact(article_hashes=[post.content_sha256]),
        article_hashes=[post.content_sha256], effective_policy_sha256="policy-1",
        review_report_sha256="review-1", publication_targets=["export"],
    )
    receipt = adapter.publish(draft, token, credentials=None)
    bundle = json.loads(receipt.publication_bundle_json)
    assert bundle["adapter_id"] == "export" and bundle["approval_id"] == "APR-1"
    assert receipt.content_sha256 == post.content_sha256


def test_verified_token_cannot_be_constructed_or_mutated_externally():
    with pytest.raises(TypeError):
        VerifiedApprovalToken()  # type: ignore[call-arg]
    token = verified()
    with pytest.raises(Exception):
        token._artifact = None  # type: ignore[misc]


def test_registry_rejects_uninstalled_target():
    with pytest.raises(TargetValidationError):
        PublisherRegistry().get("wordpress:site-a")
