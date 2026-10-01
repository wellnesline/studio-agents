"""Side-effect-free publication adapter boundary.

There is deliberately no build-and-publish helper in this module.  An explicit
approval verification and an explicit adapter operation are both required.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Protocol

from .approval import VerifiedApprovalToken


class TargetValidationError(ValueError):
    pass


class UnsupportedCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublisherCapabilities:
    supports_drafts: bool
    supports_preview: bool
    supports_update_published: bool


@dataclass(frozen=True)
class PreparedPost:
    article_id: str
    title: str
    html: str
    excerpt: str | None
    slug: str | None
    categories: tuple[str, ...]
    assets: tuple[dict[str, Any], ...]
    content_sha256: str
    target: str


@dataclass(frozen=True)
class DraftRef:
    draft_id: str
    content_sha256: str
    is_public: bool = False


@dataclass(frozen=True)
class PreviewRef:
    preview_id: str
    content_sha256: str


@dataclass(frozen=True)
class PublicationReceipt:
    publication_id: str
    content_sha256: str
    publication_bundle_json: str


class PublisherAdapter(Protocol):
    def validate_target(self, target: str) -> None: ...
    def capabilities(self) -> PublisherCapabilities: ...
    def prepare(self, article_bundle: dict[str, Any], target: str) -> PreparedPost: ...
    def create_draft(self, post: PreparedPost, credentials: Any) -> DraftRef: ...
    def preview(self, post: PreparedPost, draft: DraftRef | None = None) -> PreviewRef: ...
    def publish(self, draft: DraftRef, approval: VerifiedApprovalToken,
                credentials: Any) -> PublicationReceipt: ...
    def update_published(self, receipt: PublicationReceipt, post: PreparedPost,
                         approval: VerifiedApprovalToken, credentials: Any) -> PublicationReceipt: ...


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ExportAdapter:
    """Built-in local export; it does not import or call networking libraries."""

    adapter_id = "export"

    def __init__(self) -> None:
        # Draft references are local capabilities, not user-supplied status
        # strings.  Keeping their canonical payloads prevents a forged ref
        # from selecting content that was never prepared by this adapter.
        self._local_drafts: dict[str, tuple[str, str]] = {}

    def validate_target(self, target: str) -> None:
        if target != "export":
            raise TargetValidationError(f"export adapter does not support target: {target}")

    def capabilities(self) -> PublisherCapabilities:
        # No remote draft service exists; DraftRef is only a local non-public handle.
        return PublisherCapabilities(supports_drafts=False, supports_preview=True,
                                     supports_update_published=False)

    def prepare(self, article_bundle: dict[str, Any], target: str) -> PreparedPost:
        self.validate_target(target)
        if not isinstance(article_bundle, dict):
            raise ValueError("article_bundle must be an object")
        article_id = str(article_bundle.get("id", ""))
        if not article_id:
            raise ValueError("article_bundle.id is required")
        html = article_bundle.get("article_html", article_bundle.get("html", article_bundle.get("article_md", "")))
        if not isinstance(html, str):
            raise ValueError("article content must be a string")
        title = article_bundle.get("title")
        if not isinstance(title, str):
            title = _title_from_markdown(article_bundle.get("article_md", ""), article_id)
        categories = article_bundle.get("categories", [])
        assets = article_bundle.get("assets", [])
        if not isinstance(categories, list) or not all(isinstance(item, str) for item in categories):
            raise ValueError("categories must be a list of strings")
        if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
            raise ValueError("assets must be a list of objects")
        payload = {"article_id": article_id, "title": title, "html": html,
                   "excerpt": article_bundle.get("excerpt"), "slug": article_bundle.get("slug"),
                   "categories": categories, "assets": assets, "target": target}
        return PreparedPost(article_id, title, html, payload["excerpt"], payload["slug"],
                            tuple(categories), tuple(assets), _sha256(payload), target)

    def create_draft(self, post: PreparedPost, credentials: Any) -> DraftRef:
        _require_post(post)
        # This is strictly an in-notes/local reference and never a published status.
        draft = DraftRef(draft_id=f"local-export:{post.content_sha256}", content_sha256=post.content_sha256)
        self._local_drafts[draft.draft_id] = (post.content_sha256, _canonical_json(asdict(post)))
        return draft

    def preview(self, post: PreparedPost, draft: DraftRef | None = None) -> PreviewRef:
        _require_post(post)
        if draft is not None and (not isinstance(draft, DraftRef) or draft.content_sha256 != post.content_sha256):
            raise ValueError("draft does not match prepared post")
        return PreviewRef(preview_id=f"local-preview:{post.content_sha256}", content_sha256=post.content_sha256)

    def publish(self, draft: DraftRef, approval: VerifiedApprovalToken,
                credentials: Any) -> PublicationReceipt:
        if not isinstance(draft, DraftRef):
            raise TypeError("publish requires a DraftRef")
        if not isinstance(approval, VerifiedApprovalToken):
            raise TypeError("publish requires a VerifiedApprovalToken")
        stored = self._local_drafts.get(draft.draft_id)
        if stored is None or stored[0] != draft.content_sha256:
            raise ValueError("publish requires a local draft created by this adapter")
        if "export" not in approval.publication_targets:
            raise TargetValidationError("approval is not bound to export")
        if draft.content_sha256 not in approval.article_hashes:
            raise TargetValidationError("approval is not bound to this prepared content")
        bundle = {"schema_version": "blogfactory-publication-bundle/v1", "adapter_id": self.adapter_id,
                  "approval_id": approval.approval_id, "article_hashes": list(approval.article_hashes),
                  "publication_targets": list(approval.publication_targets),
                  "content_sha256": draft.content_sha256, "post": json.loads(stored[1])}
        encoded = _canonical_json(bundle)
        return PublicationReceipt(publication_id=f"local-export:{_sha256(bundle)}",
                                  content_sha256=draft.content_sha256,
                                  publication_bundle_json=encoded)

    def update_published(self, receipt: PublicationReceipt, post: PreparedPost,
                         approval: VerifiedApprovalToken, credentials: Any) -> PublicationReceipt:
        if not isinstance(approval, VerifiedApprovalToken):
            raise TypeError("update_published requires a VerifiedApprovalToken")
        raise UnsupportedCapabilityError("export does not update published remote content")


def _title_from_markdown(markdown: Any, fallback: str) -> str:
    if not isinstance(markdown, str):
        return fallback
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _require_post(post: object) -> None:
    if not isinstance(post, PreparedPost):
        raise TypeError("expected PreparedPost")


class PublisherRegistry:
    def __init__(self, adapters: dict[str, PublisherAdapter] | None = None) -> None:
        self._adapters: dict[str, PublisherAdapter] = {"export": ExportAdapter()}
        if adapters:
            self._adapters.update(adapters)

    def get(self, target: str) -> PublisherAdapter:
        if not isinstance(target, str) or not target:
            raise TargetValidationError("publication target must be a non-empty string")
        adapter_id = target.split(":", 1)[0]
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise TargetValidationError(f"no installed publisher adapter for target: {target}")
        adapter.validate_target(target)
        return adapter


registry = PublisherRegistry()
