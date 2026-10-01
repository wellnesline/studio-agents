"""Strict plan.json topic schema used by the provenance gate."""
from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

PLAN_SCHEMA_VERSION = "blogfactory-plan/v1"

_STRING = {"type": "string", "minLength": 1}

# topic_id は そのまま保存先ディレクトリ名になる (runner.py: articles/<topic_id>/article.md)。
# 検査が無いと以下が通ってしまうため、 単一のパス要素として安全な形だけを許す。
#   ../outside, C:/outside -> 保存先の外へ書き込む
#   CON, NUL 等            -> Windows の予約デバイス名で保存できない
#   Topic-A と topic-a      -> 大小文字を区別しない Windows で衝突・上書き
_TOPIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
_TOPIC_ID = {"type": "string", "pattern": _TOPIC_ID_PATTERN}

# 拡張子が付いても予約のまま (CON.md も不可)。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_topic_id(topic_id: Any) -> None:
    """保存先ディレクトリ名として安全かを検査する。 生成を始める前に弾く。"""
    if not isinstance(topic_id, str) or not re.match(_TOPIC_ID_PATTERN, topic_id):
        raise ValueError(
            f"topic_id は英数字で始まり [A-Za-z0-9._-] のみ・100文字以内で指定してください: {topic_id!r}"
        )
    if topic_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise ValueError(f"topic_id に Windows の予約デバイス名は使えません: {topic_id!r}")
    if topic_id.endswith("."):
        raise ValueError(f"topic_id を . で終えることはできません (Windows が末尾を落とす): {topic_id!r}")
_SOURCE_REFS = {"type": "array", "uniqueItems": True, "items": _STRING}

TOPIC_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "topic_id", "title", "audience_intent", "search_intent", "core_claim", "lesson_id",
        "conclusion", "content_type", "voice_mode", "experience_claims", "source_refs", "risk_tags",
        "limitation_or_counterexample",
    ],
    "properties": {
        "topic_id": _TOPIC_ID, "title": _STRING, "audience_intent": _STRING, "search_intent": _STRING,
        "core_claim": _STRING, "lesson_id": _STRING, "conclusion": _STRING, "content_type": _STRING,
        "voice_mode": {"type": "string", "enum": ["first_person", "third_person", "attributed", "unspecified"]},
        "experience_claims": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["text", "source_ref"], "properties": {"text": _STRING, "source_ref": {"type": ["string", "null"]}}}},
        "source_refs": _SOURCE_REFS,
        "risk_tags": {"type": "array", "uniqueItems": True, "items": _STRING},
        "limitation_or_counterexample": {"oneOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["claim", "source_ref"], "properties": {"claim": _STRING, "source_ref": _STRING}}]},
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["schema_version", "topics"],
    "properties": {"schema_version": {"const": PLAN_SCHEMA_VERSION},
                   "topics": {"type": "array", "items": TOPIC_ENTRY_SCHEMA}},
}


def validate_topic_entry(topic: dict[str, Any]) -> None:
    Draft202012Validator(TOPIC_ENTRY_SCHEMA).validate(topic)
    # 予約名・末尾ドットは正規表現で表しにくいので、 スキーマ通過後に個別検査する。
    validate_topic_id(topic.get("topic_id"))


def validate_plan(plan: dict[str, Any]) -> None:
    Draft202012Validator(PLAN_SCHEMA).validate(plan)
    for topic in plan.get("topics") or []:
        validate_topic_id(topic.get("topic_id"))


def topics_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and return entries; a lone topic is accepted for one-article checks."""
    if "topics" in plan:
        validate_plan(plan)
        return plan["topics"]
    validate_topic_entry(plan)
    return [plan]
