"""Self-contained human review HTML for Phase 3 audit output."""
from __future__ import annotations

from html import escape
import json
from typing import Any


_NEUTRAL = {"bias": {name: {"requirement": "report"} for name in ("lesson_diversity", "framing", "advertorial", "similarity", "grounding", "search_intent")}}


def _diff(value: Any, neutral: Any, path: str = "") -> list[str]:
    if isinstance(value, dict) and isinstance(neutral, dict):
        changed = []
        for key in sorted(set(value) | set(neutral)):
            changed.extend(_diff(value.get(key), neutral.get(key), f"{path}.{key}" if path else key))
        return changed
    return [] if value == neutral else [f"{path}: {neutral!r} → {value!r}"]


def render_review_package(cluster_report: dict, effective_policy: dict) -> str:
    """Return standalone HTML.  It intentionally contains no automatic disposition."""
    policy = effective_policy.get("effective_policy", effective_policy)
    diffs = _diff(policy, _NEUTRAL)
    flags = cluster_report.get("review_required", [])
    columns = ("id", "title", "core_claim", "lesson_id", "conclusion", "conclusion_excerpt", "same_lesson_count",
               "repeated_phrases", "first_person_finding", "limitation_slot", "risk_class", "source_present",
               "meta_finding", "search_intent", "search_intent_nearby_candidates", "similarity_top3")
    rows = []
    for item in cluster_report.get("articles", []):
        rows.append("<tr>" + "".join(f"<td>{escape(str(item.get(key, '')))}</td>" for key in columns) + "<td>承認 / 差戻し: ____________________</td></tr>")
    clusters = cluster_report.get("metrics", {}).get("lesson_diversity", {}).get("clusters", [])
    pairs = cluster_report.get("metrics", {}).get("similarity", {}).get("pairs", [])[:10]
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>Campaign review package</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#18202a}} table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #b8c2cc;padding:7px;vertical-align:top}}th{{background:#edf2f7}}pre{{white-space:pre-wrap;background:#f5f7f9;padding:12px}}.flag{{color:#8a3b00}}</style>
<h1>Campaign review package</h1><h2>effective policy</h2><pre>{escape(json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True))}</pre>
<h2>設定差分</h2><pre>{escape(chr(10).join(diffs) or '中立設定からの差分なし')}</pre>
<h2>review_required</h2><pre class="flag">{escape(json.dumps(flags, ensure_ascii=False, indent=2))}</pre>
<h2>同一lessonクラスタ</h2><pre>{escape(json.dumps(clusters, ensure_ascii=False, indent=2))}</pre>
<h2>類似上位ペア</h2><pre>{escape(json.dumps(pairs, ensure_ascii=False, indent=2))}</pre>
<h2>記事レビュー</h2><table><tr>{''.join(f'<th>{escape(key)}</th>' for key in columns)}<th>human decision</th></tr>{''.join(rows)}</table>
<p>承認者: ____________________　承認日: ____________________</p><p>差戻し理由: ____________________________________________________________</p></html>'''
