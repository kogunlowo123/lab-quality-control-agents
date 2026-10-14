"""agent-identity-fabric client, authentication and authorization."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from agentic_core.audit import AuditLog, InMemoryAuditSink
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.policy import PolicyEngine, PolicySet, Resource
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab_quality_control_agents.domain.errors import (
    ApprovalRequiredError,
    ForbiddenError,
    UnauthorizedError,
)
from lab_quality_control_agents.integrations.identity_fabric import (
    JWT_BEARER,
    AgentCredentials,
    FabricRejectedError,
    FabricUnavailableError,
    IdentityFabricClient,
)
from lab_quality_control_agents.security.authn import Authenticator, Principal, PrincipalKind
from lab_quality_control_agents.security.authz import Authorizer
from lab_quality_control_agents.security.jwks import RemoteJwks, StaticJwks, unverified_issuer
from tests.conftest import AUDIENCE, OIDC_ISSUER, ROOT, SUPERVISOR, TECH, FakeIdp

FABRIC = "https://identity.lab.test"
AGENT = "spiffe://lab.test/agent/lims/orchestrator"


def fabric_client(handler: Callable[[httpx.Request], httpx.Response]) -> IdentityFabricClient:
    return IdentityFabricClient(
        FABRIC + "/", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def decision(value: str, reason: str = "explicit allow") -> dict[str, Any]:
    return {
        "decision": value,
        "agent": AGENT,
        "action": "qc:read",
        "resource": "qc-run:*",
        "matched_rules": ["allow-scoped-actions"],
        "policy_version": "2026.10.0",
        "policy_digest": "sha256:abc",
        "reason": reason,
        "delegation_chain": [],
    }


class TestFabricClient:
    async def test_token_introspect_authorize_wire_formats(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/oauth2/token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "agent-token",
                        "token_type": "Bearer",
                        "expires_in": 600,
                        "scope": "qc:read",
                    },
                )
            if request.url.path == "/oauth2/introspect":
                return httpx.Response(200, json={"active": True, "sub": AGENT, "scope": "qc:read"})
            return httpx.Response(200, json=decision("allow"))

        client = fabric_client(handler)
        assert client.base_url == FABRIC
        token = await client.token(client_id=AGENT, assertion="wl-token", scope="qc:read")
        assert token.access_token == "agent-token"
        assert (await client.introspect("agent-token")).active
        verdict = await client.authorize(
            "agent-token",
            action="qc:read",
            resource_type="qc-run",
            resource_id="*",
            classification=DataClassification.INTERNAL,
        )
        assert verdict.decision is Decision.ALLOW
        form = dict(httpx.QueryParams(seen[0].content.decode()))
        assert form == {
            "grant_type": JWT_BEARER,
            "client_id": AGENT,
            "assertion": "wl-token",
            "scope": "qc:read",
        }
        assert seen[2].headers["authorization"] == "Bearer agent-token"
        assert json.loads(seen[2].content)["resource_classification"] == 1

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (503, {"json": {"error": "down"}}, FabricUnavailableError),
            (200, {"text": "not json"}, FabricUnavailableError),
            (200, {"json": ["list"]}, FabricUnavailableError),
            (401, {"json": {"error": "invalid_token"}}, FabricRejectedError),
            (200, {"json": {"unexpected": True}}, FabricUnavailableError),
        ],
    )
    async def test_failures_fail_closed(
        self, status: int, body: dict[str, Any], error: type[Exception]
    ) -> None:
        client = fabric_client(lambda _: httpx.Response(status, **body))
        with pytest.raises(error):
            await client.introspect("t")
        with pytest.raises(error):
            await client.token(client_id=AGENT, assertion="a")
        with pytest.raises(error):
            await client.authorize(
                "t",
                action="a",
                resource_type="r",
                resource_id="i",
                classification=DataClassification.INTERNAL,
            )

    async def test_transport_error_fails_closed(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(FabricUnavailableError, match="unreachable"):
            await fabric_client(broken).introspect("t")


class TestAgentCredentials:
    async def test_cached_until_near_expiry_then_refreshed(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("projected-sa-token-1\n", encoding="utf-8")
        assertions: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assertions.append(dict(httpx.QueryParams(request.content.decode()))["assertion"])
            return httpx.Response(
                200,
                json={
                    "access_token": f"t{len(assertions)}",
                    "token_type": "Bearer",
                    "expires_in": 600,
                },
            )

        now = [datetime(2026, 10, 1, tzinfo=UTC)]
        creds = AgentCredentials(
            fabric_client(handler),
            agent_id=AGENT,
            workload_token_file=token_file,
            clock=lambda: now[0],
        )
        assert (await creds.credential()).access_token == "t1"
        now[0] += timedelta(seconds=500)
        assert (await creds.credential()).access_token == "t1"
        token_file.write_text("projected-sa-token-2", encoding="utf-8")
        now[0] += timedelta(seconds=60)
        assert (await creds.credential()).access_token == "t2"
        assert assertions == ["projected-sa-token-1", "projected-sa-token-2"]

    async def test_missing_token_file(self, tmp_path: Path) -> None:
        creds = AgentCredentials(
            fabric_client(lambda _: httpx.Response(500)),
            agent_id=AGENT,
            workload_token_file=tmp_path / "absent",
            clock=lambda: datetime.now(UTC),
        )
        with pytest.raises(FabricUnavailableError, match="unreadable"):
            await creds.credential()


class TestAuthentication:
    async def test_oidc_roles_decide_principal_kind(self, idp: FakeIdp) -> None:
        auth = Authenticator(StaticJwks(idp.jwks()), issuer=OIDC_ISSUER, audience=AUDIENCE)
        human = await auth.authenticate(
            f"Bearer {idp.mint(sub='sam@lab.test', roles=['qc-supervisor'], name='Sam S')}"
        )
        assert (human.kind, human.name, human.roles) == (
            PrincipalKind.HUMAN,
            "Sam S",
            frozenset({"qc-supervisor"}),
        )
        agent = await auth.authenticate(
            f"Bearer {idp.mint(sub='app-1', roles=['qc-agent'], scp='qc:read capa:propose')}"
        )
        assert agent.kind is PrincipalKind.AGENT
        assert agent.scopes == frozenset({"qc:read", "capa:propose"})
        assert agent.name == "app-1"
        service = await auth.authenticate(f"Bearer {idp.mint(sub='lis', roles='qc-interface')}")
        assert service.kind is PrincipalKind.SERVICE
        assert "token" not in human.model_dump()

    @pytest.mark.parametrize(
        "header", [None, "", "Basic abc", "Bearer ", "Bearer not.a.jwt", "Bearer x.!!!.y"]
    )
    async def test_missing_or_malformed(self, idp: FakeIdp, header: str | None) -> None:
        auth = Authenticator(StaticJwks(idp.jwks()), issuer=OIDC_ISSUER, audience=AUDIENCE)
        with pytest.raises(UnauthorizedError):
            await auth.authenticate(header)

    async def test_fabric_credentials_are_introspected(self, idp: FakeIdp) -> None:
        fabric_idp = FakeIdp(FABRIC, kid="fabric")
        responses = {
            "active": True,
            "sub": AGENT,
            "scope": "qc:read",
            "act": {"sub": "spiffe://lab.test/agent/a/b"},
        }
        client = fabric_client(lambda _: httpx.Response(200, json=responses))
        auth = Authenticator(
            StaticJwks(idp.jwks()), issuer=OIDC_ISSUER, audience=AUDIENCE, fabric=client
        )
        token = fabric_idp.mint(sub=AGENT, aud="agent-platform")
        principal = await auth.authenticate(f"Bearer {token}")
        assert principal.via_fabric
        assert principal.kind is PrincipalKind.AGENT
        assert principal.delegation_chain == ("spiffe://lab.test/agent/a/b",)
        responses["active"] = False
        with pytest.raises(UnauthorizedError, match="not active"):
            await auth.authenticate(f"Bearer {token}")

    def test_unverified_issuer_reads_payload_only(self, idp: FakeIdp) -> None:
        assert unverified_issuer(idp.mint(sub="x")) == OIDC_ISSUER
        assert unverified_issuer("e30.e30.sig") is None

    async def test_remote_jwks_refresh_and_outage(self, idp: FakeIdp) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=idp.jwks()) if calls["n"] == 1 else httpx.Response(503)

        clock = [0.0]
        jwks = RemoteJwks(
            "https://login.lab.test/jwks",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            clock=lambda: clock[0],
        )
        assert await jwks.key_for("k1") is not None
        clock[0] = 400.0  # stale: refresh fails but the cached key still serves
        assert await jwks.key_for("k1") is not None
        with pytest.raises(UnauthorizedError):
            await jwks.key_for("unknown")


def authorizer(fabric: IdentityFabricClient | None = None) -> tuple[Authorizer, InMemoryAuditSink]:
    sink = InMemoryAuditSink()
    audit = AuditLog(sink, Ed25519PrivateKey.generate(), key_id="t")
    policy = PolicyEngine(PolicySet.from_file(ROOT / "policies" / "lab-qc.yaml"))
    return Authorizer(policy, audit, fabric=fabric), sink


AGENT_PRINCIPAL = Principal(
    subject=AGENT,
    kind=PrincipalKind.AGENT,
    name="orchestrator",
    scopes=frozenset({"qc:read", "capa:*"}),
    issuer=FABRIC,
    via_fabric=True,
    token="agent-token",
)
CAPA = Resource(type="capa", id="c1", classification=DataClassification.CONFIDENTIAL)


class TestAuthorization:
    async def test_local_decisions_are_audited(self) -> None:
        authz, sink = authorizer()
        outcome = await authz.require(SUPERVISOR, "capa:approve", CAPA)
        assert outcome.matched_rules == ("supervisor-signs",)
        with pytest.raises(ForbiddenError):
            await authz.require(TECH, "capa:approve", CAPA)
        assert [e.outcome for e in sink.events] == ["allow", "deny"]
        assert sink.events[0].details["policy_digest"].startswith("sha256:")
        assert authz.policy.policy.name == "lab-quality-control"

    async def test_agents_get_require_approval_for_signatures(self) -> None:
        authz, _ = authorizer(fabric_client(lambda _: httpx.Response(200, json=decision("allow"))))
        with pytest.raises(ApprovalRequiredError):
            await authz.require(AGENT_PRINCIPAL, "capa:approve", CAPA)

    async def test_fabric_and_local_most_restrictive_wins(self) -> None:
        authz, sink = authorizer(
            fabric_client(lambda _: httpx.Response(200, json=decision("deny", "suspended")))
        )
        outcome = await authz.decide(AGENT_PRINCIPAL, "qc:read", Resource(type="qc-run"))
        assert outcome.decision is Decision.DENY
        assert outcome.reason == "identity fabric: suspended"
        assert outcome.sources == ("fabric", "local")
        assert sink.events[-1].details["sources"] == ["fabric", "local"]

    async def test_fabric_allow_cannot_widen_local_deny(self) -> None:
        authz, _ = authorizer(fabric_client(lambda _: httpx.Response(200, json=decision("allow"))))
        outside_scope = AGENT_PRINCIPAL.model_copy(update={"scopes": frozenset({"capa:read"})})
        outcome = await authz.decide(outside_scope, "qc:ingest", Resource(type="qc-result"))
        assert outcome.decision is Decision.DENY

    async def test_restricted_data_is_denied_for_everyone(self) -> None:
        authz, _ = authorizer()
        restricted = Resource(type="qc-run", classification=DataClassification.RESTRICTED)
        outcome = await authz.decide(SUPERVISOR, "qc:read", restricted)
        assert outcome.matched_rules == ("deny-restricted-data",)
