"""Deterministic hard-block gate for experience provenance and high-risk claims."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .plan_schema import topics_from_plan
from .source_registry import SourceRegistry

HARD_BLOCK = "hard_block"


@dataclass(frozen=True)
class Finding:
    type: str
    severity: str
    location: str
    message: str


def _finding(kind: str, location: str, message: str) -> Finding:
    return Finding(kind, HARD_BLOCK, location, message)


# Bounded predicates deliberately target assertions of observed work/results, not
# ordinary first-person opinion such as 「私はそう思う」.
_EXPERIENCE_PATTERNS = (
    ("first_person_experience", re.compile(r"(?:私|僕|わたし|当人|筆者|著者)(?:は|が|も)?[^。\n]{0,90}(?:導入|実装|運用|構築|開発|作成|達成|改善|削減|増加|検証|測定|試験|成功|失敗|経験|取り組|組ん|使)[^。\n]{0,30}(?:した|しました|してきた|してきました|できた|ました|きた人|経験者|実績)")),
    ("author_experience_profile", re.compile(r"(?:筆者|著者)(?:は|が)?[^。\n]{0,100}(?:実体験|経験|実績|発信者|運営|仕事|取り組)[^。\n]{0,35}(?:です|である|しています|している|した)")),
    ("third_person_experience", re.compile(r"(?:同社|当社|弊社|その会社|同チーム|同法人|[一-龥々ァ-ヶA-Za-z0-9]+(?:社|社団|チーム))(?:は|が|では)?[^。\n]{0,90}(?:導入|実装|運用|構築|開発|作成|達成|改善|削減|増加|検証|測定|試験|成功|受賞|公開)[^。\n]{0,30}(?:した|しました|している|していました|した実績|を達成)")),
    ("attributed_experience", re.compile(r"(?:担当者|関係者|代表者|利用者|顧客|[一-龥々ァ-ヶA-Za-z]{2,}氏)(?:によると|は)[^。\n]{0,90}(?:導入|実装|運用|構築|達成|改善|削減|増加|検証|測定|経験|成功|失敗)[^。\n]{0,30}(?:した|しました|している|だった|と(?:いう|述べ))")),
)

_NUMBER = r"(?:[0-9０-９][0-9０-９,，]*|[〇零一二三四五六七八九十百千万億]+)"
_RISK_PATTERNS = (
    ("money", re.compile(_NUMBER + r"(?:\s*[-〜~]\s*" + _NUMBER + r")?\s*(?:円|ドル|万円|千円)")),
    ("rate", re.compile(_NUMBER + r"(?:[.．][0-9０-９]+)?\s*(?:%|％|パーセント|倍)")),
    ("date", re.compile(r"(?:20[0-9０-９]{2}|19[0-9０-９]{2})\s*(?:年|/|-)\s*[0-9０-９]{1,2}(?:\s*月|/|-)")),
    ("institution", re.compile(r"(?:制度|法律|法令|補助金|助成金|保険適用|認定)[^。\n]{0,50}(?:です|である|となる|がある|を受け|対象)")),
    ("public_result", re.compile(r"(?:通過率|合格率|成功率|利用者数|導入社数|売上|公開実績|受賞)[^。\n]{0,40}")),
)


def _line_location(markdown: str, offset: int) -> str:
    return f"article.md:{markdown.count(chr(10), 0, offset) + 1}"


def detect_first_person_findings(article_md: str) -> list[Finding]:
    """Expose the Phase 2 first-person detector for visibility-only audit use."""
    pattern = _EXPERIENCE_PATTERNS[0][1]
    return [_finding("first_person_experience", _line_location(article_md, match.start()),
                     "本文に一人称の経験・実績表現があります") for match in pattern.finditer(article_md)]


def _contains_claim(article_md: str, claim: str) -> bool:
    return claim in article_md or claim.replace(" ", "") in article_md.replace(" ", "")


def _valid_source(registry: SourceRegistry, source_ref: str | None, tenant_id: str,
                  voice_mode: str | None = None, *, require_owner: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    if not source_ref:
        return None, "source_ref がありません"
    source = registry.find(source_ref)
    if source is None:
        return None, "source_ref が registry にありません"
    if not registry.is_tenant_match(source, tenant_id):
        return None, "source の tenant_id が campaign と一致しません"
    if not registry.is_approved(source):
        return None, "source が公開承認されていません"
    if not registry.is_unexpired(source):
        return None, "source の承認期限が切れています"
    if not registry.has_locator(source):
        return None, "source に locator がありません"
    if voice_mode is not None and not registry.allows_voice(source, voice_mode):
        return None, "source の allowed_voice が voice_mode を許可していません"
    if require_owner and not registry.owner_matches_voice(source, voice_mode or "unspecified"):
        return None, "voice_mode に対応する source owner がありません"
    return source, None


def _plan_findings(plan: dict[str, Any], registry: SourceRegistry, tenant_id: str) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    topics = topics_from_plan(plan)
    for topic in topics:
        for index, claim in enumerate(topic["experience_claims"]):
            _, problem = _valid_source(registry, claim["source_ref"], tenant_id, topic["voice_mode"], require_owner=True)
            if problem:
                findings.append(_finding("experience_provenance", f"plan:{topic['topic_id']}.experience_claims[{index}]", problem))
    return findings, topics


def check(*, article_md: str, plan: dict[str, Any] | None, registry: SourceRegistry,
          effective_policy: dict, campaign_tenant_id: str) -> list[Finding]:
    """Return hard-block findings only; this function never edits or regenerates text."""
    if not isinstance(campaign_tenant_id, str) or not campaign_tenant_id:
        raise ValueError("campaign_tenant_id must be a non-empty string")
    if not isinstance(article_md, str):
        raise ValueError("article_md must be a string")
    # The floor is invariant even when grounding.requirement is off.
    if effective_policy and effective_policy.get("tenant_id") not in (None, campaign_tenant_id):
        raise ValueError("effective_policy tenant_id does not match campaign_tenant_id")

    findings: list[Finding] = []
    topics: list[dict[str, Any]] = []
    if plan is not None:
        plan_findings, topics = _plan_findings(plan, registry, campaign_tenant_id)
        findings.extend(plan_findings)

    planned_claims = [claim for topic in topics for claim in topic["experience_claims"]]
    for kind, pattern in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(article_md):
            if not any(_contains_claim(article_md[match.start():match.end()], claim["text"]) or
                       _contains_claim(claim["text"], match.group(0)) for claim in planned_claims):
                findings.append(_finding(kind, _line_location(article_md, match.start()),
                    "本文の経験・実績表現に対応する plan.experience_claims の来歴がありません"))

    valid_article_sources = []
    for topic in topics:
        for source_ref in topic["source_refs"]:
            source, problem = _valid_source(registry, source_ref, campaign_tenant_id)
            if source is not None:
                valid_article_sources.append(source)
    for kind, pattern in _RISK_PATTERNS:
        for match in pattern.finditer(article_md):
            if not valid_article_sources:
                findings.append(_finding("unsupported_high_risk_" + kind, _line_location(article_md, match.start()),
                    "高リスクの断定に source_ref と locator を持つ根拠がありません"))
    return findings
