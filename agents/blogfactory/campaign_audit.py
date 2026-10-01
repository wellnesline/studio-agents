"""Deterministic, policy-driven campaign visibility audit (Phase 3)."""
from __future__ import annotations

from collections import Counter
from itertools import combinations
import re
from typing import Any

from .provenance_gate import detect_first_person_findings
from .source_registry import SourceRegistry


_RISK = re.compile(r"(?:[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?\s*(?:円|ドル|万円|千円|%|％|倍)|(?:制度|法律|法令|補助金|助成金|保険適用|認定|通過率|合格率|成功率|利用者数|導入社数|売上))")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _policy(value: dict[str, Any]) -> dict[str, Any]:
    return value.get("effective_policy", value)


def _requirement(policy: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    config = policy.get("bias", {}).get(name, {})
    return config.get("requirement", "report"), config


def _topic(article: dict[str, Any]) -> dict[str, Any] | None:
    plan = article.get("plan")
    if not isinstance(plan, dict):
        return None
    topics = plan.get("topics")
    if isinstance(topics, list):
        if len(topics) == 1:
            return topics[0] if isinstance(topics[0], dict) else None
        article_id = article.get("id")
        for item in topics:
            if isinstance(item, dict) and item.get("topic_id") == article_id:
                return item
        return topics[0] if topics and isinstance(topics[0], dict) else None
    return plan


def _title(markdown: str, topic: dict[str, Any] | None, fallback: str) -> str:
    if topic and topic.get("title"):
        return str(topic["title"])
    match = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return match.group(1) if match else fallback


def _tokens(text: str, n_min: int = 3, n_max: int = 5) -> set[str]:
    normalized = re.sub(r"\s+", "", re.sub(r"[#>*_`\-]", "", text)).lower()
    return {normalized[i:i + n] for n in range(n_min, n_max + 1)
            for i in range(max(0, len(normalized) - n + 1))}


def _jaccard(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / len(a | b) if a or b else 0.0


def _headings(markdown: str) -> list[str]:
    return [match.group(1) for match in _HEADING.finditer(markdown)]


def _source_status(topic: dict[str, Any] | None, registry: SourceRegistry | None, tenant_id: str | None) -> tuple[bool, bool]:
    """Return (has valid source, has valid external source)."""
    if not topic or not registry:
        return False, False
    valid: list[dict[str, Any]] = []
    for source_id in topic.get("source_refs", []):
        source = registry.find(source_id)
        if (source and registry.is_approved(source) and registry.is_unexpired(source)
                and registry.has_locator(source) and (tenant_id is None or registry.is_tenant_match(source, tenant_id))):
            valid.append(source)
    return bool(valid), any(not str(item.get("kind", "")).startswith("internal") for item in valid)


def _add_review(report: dict[str, Any], metric: str, target: Any, value: float, threshold: float) -> None:
    report["review_required"].append({"metric": metric, "target": target,
                                      "value": round(value, 6), "threshold": threshold})


def audit_campaign(*, articles: list[dict], effective_policy: dict, registry=None) -> dict:
    """Produce a visibility-only cluster report; it never emits PASS/BLOCK or edits articles."""
    policy = _policy(effective_policy)
    source_registry = registry if isinstance(registry, SourceRegistry) else (SourceRegistry(registry) if registry else None)
    tenant_id = policy.get("tenant_id")
    rows: list[dict[str, Any]] = []
    for index, article in enumerate(articles):
        markdown = article.get("article_md", "")
        if not isinstance(markdown, str):
            raise ValueError("article_md must be a string")
        topic = _topic(article)
        article_id = str(article.get("id", index + 1))
        valid_source, external_source = _source_status(topic, source_registry, tenant_id)
        risk_tags = list(topic.get("risk_tags", [])) if topic else []
        risk_class = risk_tags or (["body_high_risk_expression"] if _RISK.search(markdown) else [])
        limitation = topic.get("limitation_or_counterexample") if topic else None
        first_person = detect_first_person_findings(markdown)
        rows.append({
            "id": article_id, "title": _title(markdown, topic, article_id), "core_claim": topic.get("core_claim") if topic else None,
            "lesson_id": topic.get("lesson_id") if topic else None, "stance": topic.get("stance") if topic else None,
            "conclusion": topic.get("conclusion") if topic else None,
            "conclusion_excerpt": (str(topic.get("conclusion", ""))[:140] if topic else None),
            "cta_objective": (topic.get("cta_objective") or topic.get("cta", {}).get("objective")) if topic else None,
            "cta_template": (topic.get("cta_template") or topic.get("cta", {}).get("template")) if topic else None,
            "limitation_slot": bool(limitation), "risk_class": risk_class, "source_present": valid_source,
            "external_source_present": external_source, "meta_finding": [],
            "first_person_finding": [item.message for item in first_person],
            "search_intent": topic.get("search_intent") if topic else None,
            "search_intent_nearby_candidates": [], "same_lesson_count": 0,
            "repeated_phrases": [], "similarity_top3": [], "human_decision": "",
            "_markdown": markdown,
        })
    report: dict[str, Any] = {"schema_version": "blogfactory-cluster-report/v1", "article_count": len(rows),
                              "articles": rows, "metrics": {}, "review_required": []}
    active = {name: _requirement(policy, name) for name in ("lesson_diversity", "framing", "advertorial", "similarity", "grounding", "search_intent")}

    if active["lesson_diversity"][0] != "off":
        counts = Counter(row["lesson_id"] for row in rows if row["lesson_id"])
        clusters = [{"lesson_id": lesson, "count": count, "share": round(count / len(rows), 6),
                     "article_ids": [row["id"] for row in rows if row["lesson_id"] == lesson]}
                    for lesson, count in counts.most_common()]
        for row in rows: row["same_lesson_count"] = counts.get(row["lesson_id"], 0)
        report["metrics"]["lesson_diversity"] = {"distribution": dict(counts), "clusters": clusters}
        threshold = active["lesson_diversity"][1].get("max_single_share")
        if active["lesson_diversity"][0] == "review_required" and threshold is not None:
            for cluster in clusters:
                if cluster["share"] > threshold:
                    for article_id in cluster["article_ids"]: _add_review(report, "lesson_diversity", article_id, cluster["share"], threshold)

    if active["framing"][0] != "off":
        stance = Counter(row["stance"] for row in rows if row["stance"])
        conclusion = Counter(row["conclusion"] for row in rows if row["conclusion"])
        report["metrics"]["framing"] = {"stance_distribution": dict(stance), "conclusion_distribution": dict(conclusion)}
        threshold = active["framing"][1].get("max_single_stance_share")
        if active["framing"][0] == "review_required" and threshold is not None:
            for value, count in stance.items():
                share = count / len(rows)
                if share > threshold:
                    for row in rows:
                        if row["stance"] == value: _add_review(report, "framing", row["id"], share, threshold)

    if active["advertorial"][0] != "off":
        objectives = Counter(row["cta_objective"] for row in rows if row["cta_objective"])
        templates = Counter(row["cta_template"] for row in rows if row["cta_template"])
        limitation_rate = sum(row["limitation_slot"] for row in rows) / len(rows) if rows else 0.0
        report["metrics"]["advertorial"] = {"cta_objective_distribution": dict(objectives), "cta_template_distribution": dict(templates), "limitation_slot_rate": round(limitation_rate, 6)}
        config = active["advertorial"][1]
        if active["advertorial"][0] == "review_required":
            threshold = config.get("cta_max_same_objective_share")
            if threshold is not None:
                for value, count in objectives.items():
                    share = count / len(rows)
                    if share > threshold:
                        for row in rows:
                            if row["cta_objective"] == value: _add_review(report, "advertorial", row["id"], share, threshold)
            minimum = config.get("limitation_min_rate")
            if minimum is not None and limitation_rate < minimum:
                for row in rows:
                    if not row["limitation_slot"]: _add_review(report, "advertorial", row["id"], limitation_rate, minimum)

    if active["similarity"][0] != "off":
        pairs = []
        for left, right in combinations(rows, 2):
            score, heading_score = _jaccard(left["_markdown"], right["_markdown"]), _jaccard("\n".join(_headings(left["_markdown"])), "\n".join(_headings(right["_markdown"])))
            pairs.append({"article_ids": [left["id"], right["id"]], "jaccard_3_5": round(score, 6), "heading_similarity": round(heading_score, 6)})
        for row in rows:
            nearest = sorted((dict(pair, score=pair["jaccard_3_5"]) for pair in pairs if row["id"] in pair["article_ids"]), key=lambda pair: pair["score"], reverse=True)[:3]
            row["similarity_top3"] = nearest
        report["metrics"]["similarity"] = {"max_pair": max(pairs, key=lambda item: item["jaccard_3_5"], default=None), "pairs": sorted(pairs, key=lambda item: item["jaccard_3_5"], reverse=True)}
        threshold = active["similarity"][1].get("review_at")
        if active["similarity"][0] == "review_required" and threshold is not None:
            for pair in pairs:
                if pair["jaccard_3_5"] > threshold: _add_review(report, "similarity", pair["article_ids"], pair["jaccard_3_5"], threshold)

    if active["grounding"][0] != "off":
        at_risk = [row for row in rows if row["risk_class"]]
        external_rate = sum(row["external_source_present"] for row in rows) / len(rows) if rows else 0.0
        report["metrics"]["grounding"] = {"risk_article_count": len(at_risk), "ungrounded_risk_article_ids": [row["id"] for row in at_risk if not row["source_present"]], "external_article_rate": round(external_rate, 6)}
        threshold = active["grounding"][1].get("external_article_min_rate")
        if active["grounding"][0] == "review_required" and threshold is not None and external_rate < threshold:
            for row in rows:
                if not row["external_source_present"]: _add_review(report, "grounding", row["id"], external_rate, threshold)

    if active["search_intent"][0] != "off":
        pairs = []
        for left, right in combinations(rows, 2):
            score = 1.0 if left["search_intent"] and left["search_intent"] == right["search_intent"] else _jaccard(left["title"], right["title"])
            pairs.append({"article_ids": [left["id"], right["id"]], "proximity": round(score, 6)})
        for row in rows:
            row["search_intent_nearby_candidates"] = sorted((pair for pair in pairs if row["id"] in pair["article_ids"]), key=lambda pair: pair["proximity"], reverse=True)[:3]
        report["metrics"]["search_intent"] = {"overlap_pairs": sorted(pairs, key=lambda item: item["proximity"], reverse=True)}
        threshold = active["search_intent"][1].get("overlap_review_at")
        if active["search_intent"][0] == "review_required" and threshold is not None:
            for pair in pairs:
                if pair["proximity"] > threshold: _add_review(report, "search_intent", pair["article_ids"], pair["proximity"], threshold)

    for row in rows:
        words = re.findall(r"[一-龥ぁ-んァ-ヶA-Za-z0-9]{4,}", row["_markdown"])
        row["repeated_phrases"] = [word for word, count in Counter(words).most_common(5) if count > 1]
        row.pop("_markdown")
    if "lesson_diversity" in report["metrics"]:
        # This is deliberately a ranked observation, not a semantic judgement.
        report["lesson_singletonization"] = report["metrics"]["lesson_diversity"]["clusters"][:1]
    return report
