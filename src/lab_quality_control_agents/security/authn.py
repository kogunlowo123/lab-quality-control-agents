"""Caller authentication.

Two credential types are accepted on every protected interface (REST, MCP, A2A):

* Agent credentials issued by agent-identity-fabric (when configured). They
  are introspected at the fabric on every request, so suspension or
  revocation of the agent takes effect immediately.
* Tokens from the enterprise OIDC issuer for laboratory staff, interface
  engines and agents that have no fabric identity. They are verified locally
  against the issuer JWKS. App roles decide the principal kind:
  ``qc-agent`` -> agent, ``qc-interface`` -> service, otherwise human.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lab_quality_control_agents.domain.errors import UnauthorizedError
from lab_quality_control_agents.integrations.identity_fabric import IdentityFabricClient
from lab_quality_control_agents.security.jwks import KeyResolver, unverified_issuer, verify_jwt

AGENT_ROLE = "qc-agent"
INTERFACE_ROLE = "qc-interface"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    kind: PrincipalKind
    name: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    issuer: str
    via_fabric: bool = False
    delegation_chain: tuple[str, ...] = ()
    token: str | None = Field(default=None, exclude=True, repr=False)


def _chain(act: dict[str, Any] | None) -> tuple[str, ...]:
    chain: list[str] = []
    while isinstance(act, dict) and isinstance(act.get("sub"), str):
        chain.append(act["sub"])
        nested = act.get("act")
        act = nested if isinstance(nested, dict) else None
    return tuple(chain)


def _strings(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, list):
        return frozenset(v for v in value if isinstance(v, str))
    return frozenset()


class Authenticator:
    def __init__(
        self,
        resolver: KeyResolver,
        *,
        issuer: str,
        audience: str,
        roles_claim: str = "roles",
        name_claim: str = "name",
        fabric: IdentityFabricClient | None = None,
        fabric_issuer: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience
        self._roles_claim = roles_claim
        self._name_claim = name_claim
        self._fabric = fabric
        self._fabric_issuer = (fabric_issuer or (fabric.base_url if fabric else "")).rstrip("/")

    async def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError("bearer token required")
        token = authorization[7:].strip()
        if not token:
            raise UnauthorizedError("bearer token required")
        issuer = unverified_issuer(token)
        if self._fabric is not None and issuer and issuer.rstrip("/") == self._fabric_issuer:
            return await self._fabric_principal(self._fabric, token)
        claims = await verify_jwt(
            token, self._resolver, issuer=self._issuer, audience=self._audience
        )
        roles = _strings(claims.get(self._roles_claim))
        if AGENT_ROLE in roles:
            kind = PrincipalKind.AGENT
        elif INTERFACE_ROLE in roles:
            kind = PrincipalKind.SERVICE
        else:
            kind = PrincipalKind.HUMAN
        subject = str(claims["sub"])
        name = claims.get(self._name_claim) or claims.get("preferred_username") or subject
        return Principal(
            subject=subject,
            kind=kind,
            name=str(name),
            roles=roles,
            scopes=_strings(claims.get("scp") or claims.get("scope")),
            issuer=self._issuer,
            token=token,
        )

    async def _fabric_principal(self, fabric: IdentityFabricClient, token: str) -> Principal:
        introspection = await fabric.introspect(token)
        if not introspection.active or not introspection.sub:
            raise UnauthorizedError("agent credential is not active")
        return Principal(
            subject=introspection.sub,
            kind=PrincipalKind.AGENT,
            name=introspection.sub,
            scopes=_strings(introspection.scope),
            issuer=self._fabric_issuer,
            via_fabric=True,
            delegation_chain=_chain(introspection.act),
            token=token,
        )
