#!/usr/bin/env python3
"""Small local CLI for the approval-gated blogfactory workflow."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from blogfactory.campaign_audit import audit_campaign
from blogfactory.policy_resolver import resolve_policy
from blogfactory.publisher import ExportAdapter
from blogfactory.review_package import render_review_package
from blogfactory.runner import publish_campaign, run_campaign
from blogfactory.source_registry import SourceRegistry


def _json(path: str) -> dict:
    # 人が用意するポリシー/計画ファイル。BOM 付きでも読む
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        from ruamel.yaml import YAML
        value = YAML(typ="safe").load(text)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _campaign(args) -> Path:
    return Path(args.campaign_dir)


def _stub(topic: dict, policy: dict) -> dict:
    return {"id": topic["topic_id"], "article_md": f"# {topic['title']}\n\n{topic['core_claim']}。"}


def cmd_resolve(args) -> None:
    result = resolve_policy(neutral=_json(args.neutral), preset=_json(args.preset) if args.preset else None,
                            tenant=_json(args.tenant) if args.tenant else None,
                            campaign=_json(args.campaign) if args.campaign else None, tenant_id=args.tenant_id)
    path = _campaign(args) / "effective-policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit(result)


def cmd_run(args) -> None:
    if not args.stub:
        raise SystemExit("実 generator は外部注入です。CLI では --stub のみ利用できます")
    root = _campaign(args)
    _emit(run_campaign(campaign_dir=root, generator=_stub, resolved_policy=_json(str(root / "effective-policy.json")),
                       registry=SourceRegistry.load(root / "source-registry.json")))


def _articles(root: Path, plan: dict) -> list[dict]:
    topics = {item["topic_id"]: item for item in plan["topics"]}
    return [{"id": path.parent.name, "article_md": path.read_text(encoding="utf-8"),
             "plan": {"topics": [topics[path.parent.name]]}}
            for path in sorted((root / "articles").glob("*/article.md"))]


def cmd_audit(args) -> None:
    root, plan = _campaign(args), _json(str(_campaign(args) / "plan.json"))
    report = audit_campaign(articles=_articles(root, plan), effective_policy=_json(str(root / "effective-policy.json")),
                            registry=SourceRegistry.load(root / "source-registry.json"))
    (root / "cluster-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit(report)


def cmd_review(args) -> None:
    root = _campaign(args)
    html = render_review_package(_json(str(root / "cluster-report.json")), _json(str(root / "effective-policy.json")))
    (root / "review-package.html").write_text(html, encoding="utf-8")
    _emit({"review_package": str(root / "review-package.html")})


def cmd_status(args) -> None:
    _emit(_json(str(_campaign(args) / "campaign.json")))


def cmd_publish(args) -> None:
    _emit(publish_campaign(campaign_dir=_campaign(args), adapter=ExportAdapter(), approval_artifact=_json(args.approval)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    resolve = sub.add_parser("resolve"); resolve.add_argument("campaign_dir"); resolve.add_argument("--neutral", required=True); resolve.add_argument("--tenant-id", required=True); resolve.add_argument("--preset"); resolve.add_argument("--tenant"); resolve.add_argument("--campaign"); resolve.set_defaults(func=cmd_resolve)
    run = sub.add_parser("run"); run.add_argument("campaign_dir"); run.add_argument("--stub", action="store_true"); run.set_defaults(func=cmd_run)
    for name, func in (("audit", cmd_audit), ("review", cmd_review), ("status", cmd_status)):
        item = sub.add_parser(name); item.add_argument("campaign_dir"); item.set_defaults(func=func)
    publish = sub.add_parser("publish"); publish.add_argument("campaign_dir"); publish.add_argument("--approval", required=True); publish.set_defaults(func=cmd_publish)
    args = parser.parse_args(argv); args.func(args)


if __name__ == "__main__":
    main()
