"""Non-configurable blogfactory safety-floor invariants (A)."""
from __future__ import annotations


SAFETY_FLOOR_VERSION = "1.0.0"


class SafetyFloorValidator:
    """Validate A invariants after B policy resolution.

    Provenance, high-risk evidence, tenant ACL, and verified human publication
    approval are implementation gates, never policy switches.  Thus a B metric
    set to ``off`` cannot disable them.
    """

    provenance_gate_enabled = True
    high_risk_evidence_gate_enabled = True
    tenant_acl_gate_enabled = True
    human_publication_approval_required = True

    def enforce(self, effective_policy: dict) -> None:
        if not isinstance(effective_policy, dict):
            raise ValueError("effective_policy must be an object")
        if not isinstance(effective_policy.get("tenant_id"), str) or not effective_policy["tenant_id"].strip():
            raise ValueError("Safety Floor requires a non-empty tenant_id")

        approval = effective_policy.get("approval")
        if not isinstance(approval, dict) or approval.get("minimum_approvers", 0) < 1:
            raise ValueError("Safety Floor requires minimum_approvers >= 1")

        # Defensive checks make direct callers unable to smuggle config switches
        # around the strict resolver schema.
        publication = effective_policy.get("publication", {})
        if not isinstance(publication, dict) or "auto_publish" in publication:
            raise ValueError("Safety Floor forbids automatic publication")
        if "required" in approval:
            raise ValueError("Safety Floor does not permit approval.required")
        if "safety" in effective_policy or "allow_fabrication" in effective_policy:
            raise ValueError("Safety Floor settings are not configurable")

        if not all((self.provenance_gate_enabled, self.high_risk_evidence_gate_enabled,
                    self.tenant_acl_gate_enabled, self.human_publication_approval_required)):
            raise RuntimeError("Safety Floor implementation invariant disabled")
