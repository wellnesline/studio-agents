"""Deterministic file orchestration for the blogfactory review workflow.

This module deliberately has no generator implementation and no network client.
Generation is an injected function; publication is a separate, approval-gated call.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .approval import verify_approval
from .campaign_audit import audit_campaign
from .plan_schema import validate_plan, PLAN_SCHEMA_VERSION
from .provenance_gate import HARD_BLOCK, check
from .publisher import ExportAdapter
from .review_package import render_review_package
from .source_registry import SourceRegistry

CREATED = "CREATED"
PLANNED = "PLANNED"
GENERATED = "GENERATED"
BIAS_BLOCKED = "BIAS_BLOCKED"
REVIEW_READY = "REVIEW_READY"
APPROVED = "APPROVED"
PUBLISHED = "PUBLISHED"

_TRANSITIONS = {
    None: {CREATED}, CREATED: {PLANNED}, PLANNED: {GENERATED},
    GENERATED: {BIAS_BLOCKED, REVIEW_READY}, REVIEW_READY: {APPROVED},
    APPROVED: {PUBLISHED}, BIAS_BLOCKED: set(), PUBLISHED: set(),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    # plan.json は人が用意する。 Windows のエディタが BOM 付きで保存すると
    # utf-8 では先頭が ﻿ になり json.load が落ちる。 utf-8-sig は BOM が
    # 無ければ utf-8 と同じ挙動なので、 入力側は常にこちらで読む。
    with path.open(encoding="utf-8-sig") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _state_path(campaign_dir: Path) -> Path:
    return campaign_dir / "campaign.json"


def _transition(campaign_dir: Path, state: str, *, now: datetime | None = None) -> dict[str, Any]:
    path = _state_path(campaign_dir)
    record = _read_json(path) if path.exists() else {}
    previous = record.get("state")
    if state not in _TRANSITIONS.get(previous, set()):
        raise ValueError(f"forbidden campaign state transition: {previous!r} -> {state}")
    record.update({"state": state, "updated_at": (now.isoformat() if now else None)})
    _write_json(path, record)
    return record


def _policy(resolved_policy: dict[str, Any]) -> dict[str, Any]:
    return resolved_policy.get("effective_policy", resolved_policy)


def _article_paths(campaign_dir: Path) -> list[Path]:
    return sorted((campaign_dir / "articles").glob("*/article.md"))


def _articles(campaign_dir: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {topic["topic_id"]: topic for topic in plan["topics"]}
    result = []
    for path in _article_paths(campaign_dir):
        article_id = path.parent.name
        if article_id not in by_id:
            raise ValueError(f"article {article_id!r} is not present in plan.json")
        result.append({"id": article_id, "article_md": path.read_text(encoding="utf-8"),
                       "plan": {"schema_version": PLAN_SCHEMA_VERSION, "topics": [by_id[article_id]]}})
    if {item["id"] for item in result} != set(by_id):
        raise ValueError("article set does not match plan.json")
    return result


def run_campaign(*, campaign_dir: str | Path, generator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
                 resolved_policy: dict[str, Any], registry: SourceRegistry, now: datetime | None = None) -> dict:
    """Generate, hard-gate, audit, and render a review package; never publish."""
    root = Path(campaign_dir)
    root.mkdir(parents=True, exist_ok=True)
    plan = _read_json(root / "plan.json")
    validate_plan(plan)
    # 大小文字を区別しない Windows では Topic-A と topic-a が同じディレクトリになる。
    # 区別する比較だけだと両方が検査を通り、後の記事が先の記事を黙って上書きする。
    if len({topic["topic_id"].casefold() for topic in plan["topics"]}) != len(plan["topics"]):
        raise ValueError("plan.json contains duplicate topic_id values (大小文字の違いのみも重複扱い)")
    policy = _policy(resolved_policy)
    tenant_id = policy.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("resolved_policy must contain effective_policy.tenant_id")
    _write_json(root / "effective-policy.json", resolved_policy)
    _transition(root, CREATED, now=now)
    _transition(root, PLANNED, now=now)
    generated: list[dict[str, Any]] = []
    for topic in plan["topics"]:
        article = generator(topic, policy)
        if not isinstance(article, dict) or set(article) - {"id", "article_md"}:
            raise ValueError("generator must return exactly id and article_md")
        if article.get("id") != topic["topic_id"] or not isinstance(article.get("article_md"), str):
            raise ValueError("generator result id/article_md does not match its topic")
        articles_root = (root / "articles").resolve()
        article_path = articles_root / article["id"] / "article.md"
        # topic_id はスキーマで単一パス要素に限定済み。ここは多層防御で、解決後のパスが
        # 保存先の外へ出ていないことを書き込み直前に確かめる。
        if not article_path.resolve().is_relative_to(articles_root):
            raise ValueError(f"article path escapes the campaign directory: {article['id']!r}")
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article_path.write_text(article["article_md"], encoding="utf-8")
        generated.append({"id": article["id"], "article_md": article["article_md"],
                          "plan": {"schema_version": PLAN_SCHEMA_VERSION, "topics": [topic]}})
    _transition(root, GENERATED, now=now)
    findings = []
    for article in generated:
        for finding in check(article_md=article["article_md"], plan=article["plan"], registry=registry,
                             effective_policy=policy, campaign_tenant_id=tenant_id):
            findings.append({"article_id": article["id"], **asdict(finding)})
    _write_json(root / "findings.json", {"findings": findings})
    if any(item["severity"] == HARD_BLOCK for item in findings):
        _transition(root, BIAS_BLOCKED, now=now)
        return {"state": BIAS_BLOCKED, "findings": findings}
    report = audit_campaign(articles=generated, effective_policy=resolved_policy, registry=registry)
    _write_json(root / "cluster-report.json", report)
    (root / "review-package.html").write_text(render_review_package(report, resolved_policy), encoding="utf-8")
    _transition(root, REVIEW_READY, now=now)
    return {"state": REVIEW_READY, "article_count": len(generated), "cluster_report": report}


def _export_hash(article: dict[str, Any], target: str) -> str:
    """Match ExportAdapter.prepare's deterministic content hash without side effects."""
    markdown, article_id = article["article_md"], article["id"]
    title = next((line[2:].strip() for line in markdown.splitlines() if line.startswith("# ")), article_id)
    payload = {"article_id": article_id, "title": title, "html": markdown, "excerpt": None,
               "slug": None, "categories": [], "assets": [], "target": target}
    return _sha256(payload)


def publish_campaign(*, campaign_dir: str | Path, adapter: ExportAdapter, approval_artifact: dict,
                     credentials: Any = None) -> dict:
    """Explicitly publish only a current, human-approved REVIEW_READY campaign."""
    root = Path(campaign_dir)
    if not isinstance(approval_artifact, dict):
        raise ValueError("approval artifact must be an object")
    state = _read_json(_state_path(root)).get("state")
    if state not in {REVIEW_READY, APPROVED}:
        raise ValueError(f"publish requires REVIEW_READY or APPROVED state, found {state!r}")
    required = (root / "review-package.html", root / "cluster-report.json", root / "effective-policy.json", root / "plan.json")
    if not all(path.is_file() for path in required):
        raise ValueError("publish requires review package, report, effective policy, and plan")
    plan, resolved = _read_json(root / "plan.json"), _read_json(root / "effective-policy.json")
    validate_plan(plan)
    articles = _articles(root, plan)
    if approval_artifact.get("tenant_id") != _policy(resolved).get("tenant_id"):
        raise ValueError("approval artifact tenant_id does not match campaign policy")
    registry = SourceRegistry.load(root / "source-registry.json")
    for article in articles:
        findings = check(article_md=article["article_md"], plan=article["plan"], registry=registry,
                         effective_policy=_policy(resolved), campaign_tenant_id=_policy(resolved)["tenant_id"])
        if any(item.severity == HARD_BLOCK for item in findings):
            raise ValueError("publish requires current provenance gate to have no hard blocks")
    targets = list(_policy(resolved).get("publication", {}).get("targets", []))
    if not targets or any(target != "export" for target in targets) or not isinstance(adapter, ExportAdapter):
        raise ValueError("this deterministic runner supports the explicit ExportAdapter/export target only")
    article_hashes = [_export_hash(article, "export") for article in articles]
    token = verify_approval(artifact=approval_artifact, article_hashes=article_hashes,
                            effective_policy_sha256=_sha256(resolved),
                            review_report_sha256=_sha256(_read_json(root / "cluster-report.json")),
                            publication_targets=targets)
    if state == REVIEW_READY:
        _transition(root, APPROVED)
    receipts = []
    for article in articles:
        post = adapter.prepare(article, "export")
        draft = adapter.create_draft(post, credentials)
        receipt = adapter.publish(draft, token, credentials)
        receipts.append({"article_id": article["id"], "publication_id": receipt.publication_id,
                         "content_sha256": receipt.content_sha256,
                         "publication_bundle": json.loads(receipt.publication_bundle_json)})
    _write_json(root / "publication-receipts.json", {"receipts": receipts})
    _transition(root, PUBLISHED)
    return {"state": PUBLISHED, "receipts": receipts}
