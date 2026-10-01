"""Approval artifact validation for the explicit publication boundary.

No approval is inferred from a report.  Callers must supply the current hashes
to :func:`verify_approval`; any change to a hashed input invalidates approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ApprovalExpiredError(ValueError):
    """The artifact is not an approval for the current publication inputs."""


@dataclass(frozen=True)
class ApprovalArtifact:
    tenant_id: str
    approval_id: str
    unit: str
    # JSON artifacts use arrays; normalized in-memory records use tuples so a
    # verified token cannot be widened by mutating the caller's input dict.
    article_hashes: tuple[str, ...]
    publication_targets: tuple[str, ...]
    effective_policy_sha256: str
    review_report_sha256: str
    approved_by: tuple[str, ...]
    approved_at: str
    decision: str

    @classmethod
    def from_mapping(cls, artifact: dict[str, Any]) -> "ApprovalArtifact":
        if not isinstance(artifact, dict):
            raise ApprovalExpiredError("approval artifact must be an object")
        required = (
            "tenant_id", "approval_id", "unit", "article_hashes",
            "publication_targets", "effective_policy_sha256",
            "review_report_sha256", "approved_by", "approved_at", "decision",
        )
        missing = [name for name in required if name not in artifact]
        if missing:
            raise ApprovalExpiredError(f"approval artifact missing: {', '.join(missing)}")
        if not (_nonempty_strings(artifact["article_hashes"])
                and _nonempty_strings(artifact["publication_targets"])
                and _nonempty_strings(artifact["approved_by"])):
            raise ApprovalExpiredError("approval artifact has invalid list fields")
        values = {name: artifact[name] for name in required}
        values["article_hashes"] = tuple(values["article_hashes"])
        values["publication_targets"] = tuple(values["publication_targets"])
        values["approved_by"] = tuple(values["approved_by"])
        try:
            result = cls(**values)
        except TypeError as exc:
            raise ApprovalExpiredError("invalid approval artifact") from exc
        if (not all(isinstance(getattr(result, name), str) and getattr(result, name)
                    for name in ("tenant_id", "approval_id", "effective_policy_sha256",
                                 "review_report_sha256", "approved_at"))
                or result.unit not in {"article", "wave", "campaign"}
                or result.decision != "approved"
                or not result.article_hashes
                or not result.publication_targets
                or not result.approved_by):
            raise ApprovalExpiredError("approval artifact is not a valid approved decision")
        return result


def _nonempty_strings(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)


_TOKEN_ISSUER = object()


@dataclass(frozen=True, init=False)
class VerifiedApprovalToken:
    """An opaque token that can only be issued by ``verify_approval``.

    The identity-only issuer is deliberately module-private.  The constructor
    rejects every normal external call; adapters only need the read-only
    artifact properties below.
    """

    _artifact: ApprovalArtifact

    def __init__(self, issuer: object, artifact: ApprovalArtifact) -> None:
        if issuer is not _TOKEN_ISSUER:
            raise TypeError("VerifiedApprovalToken is issued only by verify_approval")
        object.__setattr__(self, "_artifact", artifact)

    @property
    def approval_id(self) -> str:
        return self._artifact.approval_id

    @property
    def article_hashes(self) -> tuple[str, ...]:
        return tuple(self._artifact.article_hashes)

    @property
    def publication_targets(self) -> tuple[str, ...]:
        return tuple(self._artifact.publication_targets)

    @property
    def artifact(self) -> ApprovalArtifact:
        return self._artifact


def verify_approval(*, artifact: dict, article_hashes: list[str],
                    effective_policy_sha256: str, review_report_sha256: str,
                    publication_targets: list[str]) -> VerifiedApprovalToken:
    """Return an opaque token only when all current approval bindings match.

    ``article_hashes`` is intentionally an exact ordered collection: it must
    cover title, body, CTA, assets, source state and the approved article set.
    """
    approved = ApprovalArtifact.from_mapping(artifact)
    current_values = {
        "article_hashes": article_hashes,
        "publication_targets": publication_targets,
        "effective_policy_sha256": effective_policy_sha256,
        "review_report_sha256": review_report_sha256,
    }
    if not _nonempty_strings(article_hashes) or not _nonempty_strings(publication_targets):
        raise ApprovalExpiredError("current approval bindings must be non-empty string lists")
    if not all(isinstance(value, str) and value for value in
               (effective_policy_sha256, review_report_sha256)):
        raise ApprovalExpiredError("current approval hashes must be non-empty strings")
    for name, current in current_values.items():
        approved_value = list(getattr(approved, name)) if name in {"article_hashes", "publication_targets"} else getattr(approved, name)
        if approved_value != current:
            raise ApprovalExpiredError(f"approval expired: {name} changed")
    return VerifiedApprovalToken(_TOKEN_ISSUER, approved)
