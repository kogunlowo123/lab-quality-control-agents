"""Authorization: local ABAC policy, plus the identity fabric for fabric agents.

For an agent holding a fabric credential the fabric's ``/v1/authorize``
decision and the local policy are both evaluated and the more restrictive
outcome wins (deny > require_approval > allow). Everyone else is decided by
the local policy alone. There is no allow-all fallback: a missing rule is a
deny. Every decision is written to the signed audit chain.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import Decision, RiskLevel
from agentic_core.observability import correlation_id
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict

from lab_quality_control_agents.domain.errors import ApprovalRequiredError, ForbiddenError
from lab_quality_control_agents.integrations.identity_fabric import IdentityFabricClient
from lab_quality_control_agents.security.authn import Principal, PrincipalKind

_RANK = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}


class AuthorizationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    action: str
    resource: str
    reason: str
    matched_rules: tuple[str, ...]
    policy_digest: str
    sources: tuple[str, ...]


class Authorizer:
    def __init__(
        self, policy: PolicyEngine, audit: AuditLog, *, fabric: IdentityFabricClient | None = None
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._fabric = fabric

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    def _local(
        self, principal: Principal, action: str, resource: Resource, environment: dict[str, Any]
    ) -> AuthorizationOutcome:
        subject = Subject(
            id=principal.subject,
            kind=principal.kind.value,
            roles=principal.roles,
            attributes={
                "action_in_scope": any(fnmatch.fnmatchcase(action, s) for s in principal.scopes),
                "via_fabric": principal.via_fabric,
                "delegated": bool(principal.delegation_chain),
            },
        )
        risk = RiskLevel.MODERATE if principal.kind is PrincipalKind.HUMAN else RiskLevel.HIGH
        decision = self._policy.evaluate(
            PolicyRequest(
                subject=subject,
                action=action,
                resource=resource,
                risk=risk,
                environment=environment,
            )
        )
        return AuthorizationOutcome(
            decision=decision.decision,
            action=action,
            resource=f"{resource.type}:{resource.id}",
            reason=decision.reason,
            matched_rules=decision.matched_rules,
            policy_digest=decision.policy_digest,
            sources=("local",),
        )

    async def decide(
        self,
        principal: Principal,
        action: str,
        resource: Resource,
        environment: dict[str, Any] | None = None,
    ) -> AuthorizationOutcome:
        env = environment or {}
        outcome = self._local(principal, action, resource, env)
        if principal.via_fabric and self._fabric is not None and principal.token:
            remote = await self._fabric.authorize(
                principal.token,
                action=action,
                resource_type=resource.type,
                resource_id=resource.id,
                classification=resource.classification,
                attributes=resource.attributes,
                environment=env,
            )
            if _RANK[remote.decision] > _RANK[outcome.decision]:
                outcome = outcome.model_copy(
                    update={
                        "decision": remote.decision,
                        "reason": f"identity fabric: {remote.reason}",
                        "matched_rules": remote.matched_rules,
                        "policy_digest": remote.policy_digest,
                    }
                )
            outcome = outcome.model_copy(update={"sources": ("fabric", "local")})
        await self._audit.record(
            actor=principal.subject,
            action=f"authorize:{action}",
            resource=outcome.resource,
            outcome=outcome.decision.value,
            correlation_id=correlation_id(),
            details={
                "kind": principal.kind.value,
                "rules": list(outcome.matched_rules),
                "policy_digest": outcome.policy_digest,
                "sources": list(outcome.sources),
                "chain": list(principal.delegation_chain),
            },
        )
        return outcome

    async def require(
        self,
        principal: Principal,
        action: str,
        resource: Resource,
        environment: dict[str, Any] | None = None,
    ) -> AuthorizationOutcome:
        outcome = await self.decide(principal, action, resource, environment)
        if outcome.decision is Decision.DENY:
            raise ForbiddenError(f"{action} on {outcome.resource} denied: {outcome.reason}")
        if outcome.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(
                f"{action} on {outcome.resource} requires a qualified human: {outcome.reason}"
            )
        return outcome
