"""Adapters that let blogfactory use the existing one-article Blog Agent.

This module intentionally only prepares approved source material and generates an
article.  ``runner.run_campaign`` remains the single place that hard-gates
provenance, audits a campaign, and stops before publication.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .source_registry import SourceRegistry

BuildFn = Callable[[str, dict[str, Any]], str]
SourceLoader = Callable[[dict[str, Any]], str]


_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _load_local_source(source: dict[str, Any]) -> str:
    """Read a locally approved ``file:`` source; network URIs need injection."""
    uri = str(source["uri"])
    # Windows のドライブ付きパス (``C:\...`` / ``C:/...``) は urlparse が scheme="c" と
    # 解釈するため、 URI 判定より先にローカルパスとして扱う。
    # (実測: 先に urlparse すると "local file sources only" の ValueError)
    if _WINDOWS_DRIVE_PATH.match(uri):
        return Path(uri).read_text(encoding="utf-8-sig")
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise ValueError("default source_loader supports local file sources only")
    if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
        raise ValueError("file URI must refer to the local host")
    if parsed.scheme == "file":
        # ``file:///C:/x`` の path は ``/C:/x``。 そのまま Path に渡すと Windows で
        # ``\C:\x`` になり OSError 22。 url2pathname が OS ごとの変換と percent 復号を行う。
        path = Path(url2pathname(parsed.path))
    else:
        path = Path(unquote(uri))
    # 承認済みソースは人が用意する。BOM 付きでも読む
    # (utf-8-sig は BOM が無ければ utf-8 と同じ挙動)。
    return path.read_text(encoding="utf-8-sig")


def _voice_mode(topic: dict[str, Any], effective_policy: dict[str, Any]) -> str:
    value = topic.get("voice_mode")
    if not value:
        value = (effective_policy.get("persona") or {}).get("voice_default", "unspecified")
    return str(value)


def _directives(topic: dict[str, Any], effective_policy: dict[str, Any], voice_mode: str) -> dict[str, Any]:
    """Map plan fields to Blog Agent's title and audience-oriented input."""
    voice_author = {
        "first_person": "承認済みの当事者記録に基づく書き手",
        "third_person": "編集部",
        "attributed": "出典を明示する編集者",
        "unspecified": "編集者",
    }.get(voice_mode, "編集者")
    limitation = topic.get("limitation_or_counterexample") or {}
    purpose = " / ".join(part for part in (
        str(topic.get("content_type") or "記事"),
        f"主張: {topic.get('core_claim', '')}",
        f"lesson: {topic.get('lesson_id', '')}",
        f"限界・反例: {limitation.get('claim', '')}" if isinstance(limitation, dict) else "",
    ) if part)
    backend = effective_policy.get("llm_backend") or os.environ.get("STUDIO_LLM_BACKEND") or "codex"
    generation = effective_policy.get("generation") or {}
    configured_mode = generation.get("mode", "unspecified")
    mode = configured_mode if configured_mode in ("faithful", "seo_long") else "faithful"
    target_chars = generation.get("target_chars")
    return {
        "title": str(topic["title"]),
        "voice_mode": voice_mode,
        "content_type": str(topic.get("content_type") or "article"),
        "core_claim": str(topic.get("core_claim") or ""),
        "audience": {
            "author": voice_author,
            "reader": str(topic.get("audience_intent") or "読者"),
            "purpose": purpose,
            "tone": f"{voice_mode}の視点。根拠の範囲を超えて断定しない。",
            "reason": "blogfactory plan",
        },
        "llm_backend": str(backend),
        # An unspecified generation policy is deliberately source-faithful.
        "mode": mode,
        "target": target_chars if isinstance(target_chars, int) else None,
    }


def _default_build(source_text: str, directives: dict[str, Any]) -> str:
    """Call the existing Blog Agent lazily, so mocked tests import no LLM code."""
    agents_dir = Path(__file__).resolve().parents[1]
    blog_dir = agents_dir / "blog"
    studio_dir = agents_dir.parent
    slide_studio = studio_dir.parent / "slide-studio"
    for path in (blog_dir, studio_dir / "core", slide_studio / "engine", slide_studio / "api"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import blog_core  # noqa: PLC0415 -- Blog Agent has runtime path dependencies.

    previous_backend = os.environ.get("STUDIO_LLM_BACKEND")
    os.environ["STUDIO_LLM_BACKEND"] = directives["llm_backend"]
    try:
        target = directives["target"]
        # Do not fall back to Blog Agent's long-form default when policy leaves
        # target unspecified; faithful mode follows the approved source instead.
        if target is None:
            target = len(source_text)
        result = blog_core.generate(
            source_text, directives["title"], directives["audience"],
            mode=directives["mode"], target=target,
        )
    finally:
        if previous_backend is None:
            os.environ.pop("STUDIO_LLM_BACKEND", None)
        else:
            os.environ["STUDIO_LLM_BACKEND"] = previous_backend
    return str(result["markdown"])


def make_blog_agent_generator(*, registry: SourceRegistry, build_fn: BuildFn | None = None,
                              source_loader: SourceLoader | None = None) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, str]]:
    """Return the runner-compatible Blog Agent generator.

    Only tenant-matching, approved, unexpired sources that allow the chosen voice
    are handed to the build function.  This is input scoping, not a provenance
    guarantee: the runner must still run its hard provenance gate on the output.
    """
    build = build_fn or _default_build
    load_source = source_loader or _load_local_source

    def generate(topic: dict[str, Any], effective_policy: dict[str, Any]) -> dict[str, str]:
        tenant_id = effective_policy.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("effective_policy must contain tenant_id")
        voice_mode = _voice_mode(topic, effective_policy)
        sources: list[str] = []
        for source_id in topic.get("source_refs", []):
            source = registry.find(source_id)
            if not source:
                continue
            if not (registry.is_tenant_match(source, tenant_id) and registry.is_approved(source)
                    and registry.is_unexpired(source) and registry.allows_voice(source, voice_mode)):
                continue
            sources.append(load_source(source))
        article_md = build("\n\n".join(sources), _directives(topic, effective_policy, voice_mode))
        if not isinstance(article_md, str):
            raise TypeError("build_fn must return article Markdown as str")
        return {"id": topic["topic_id"], "article_md": article_md}

    return generate
