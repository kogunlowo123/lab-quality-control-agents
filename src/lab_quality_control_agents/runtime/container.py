"""Dependency container built once per process from Settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.models.factory import ProvidersConfig, build_providers
from agentic_core.models.router import ModelRouter
from agentic_core.observability import ModelTelemetryObserver
from agentic_core.policy import PolicyEngine, PolicySet
from agentic_core.security import ContentGuard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine

from lab_quality_control_agents.application.narrative import NarrativeWriter
from lab_quality_control_agents.application.service import QualityControlService
from lab_quality_control_agents.config import Settings
from lab_quality_control_agents.domain.catalog import AnalyteCatalog
from lab_quality_control_agents.domain.ports import Clock, Repositories
from lab_quality_control_agents.domain.verification import VerificationPolicy
from lab_quality_control_agents.infrastructure.database import (
    SqlCapaRepository,
    SqlEvaluationRepository,
    SqlEventRepository,
    SqlHoldRepository,
    SqlInvestigationRepository,
    SqlResultRepository,
    SqlTargetRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from lab_quality_control_agents.integrations.identity_fabric import (
    AgentCredentials,
    IdentityFabricClient,
)
from lab_quality_control_agents.observability.metrics import QcMetrics
from lab_quality_control_agents.orchestration.agents import (
    AgentContext,
    AgentIdentity,
    CorrectiveActionAgent,
    InvestigationAgent,
    QcWorkflow,
    VerificationAgent,
)
from lab_quality_control_agents.security.authn import Authenticator
from lab_quality_control_agents.security.authz import Authorizer
from lab_quality_control_agents.security.jwks import KeyResolver, RemoteJwks


@dataclass
class Container:
    settings: Settings
    service: QualityControlService
    authenticator: Authenticator
    authorizer: Authorizer
    metrics: QcMetrics
    engine: AsyncEngine
    http: httpx.AsyncClient
    audit: AuditLog
    router: ModelRouter | None

    async def aclose(self) -> None:
        if self.router is not None:
            await self.router.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


def build_router(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelRouter | None:
    if settings.providers_file is None:
        return None
    config = ProvidersConfig.from_file(settings.providers_file, env=env)
    providers = build_providers(config, env=env, transport=transport)
    return ModelRouter(providers, observers=[ModelTelemetryObserver()])


async def build_container(
    settings: Settings,
    *,
    oidc_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
    router: ModelRouter | None = None,
    metrics: QcMetrics | None = None,
) -> Container:
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    clock = clock or SystemClock()
    engine = make_engine(settings.database_url)
    await create_schema(engine)

    signer, key_id = _audit_signer(settings)
    audit = AuditLog(JsonlAuditSink(settings.audit_log_path), signer, key_id=key_id)
    catalog = AnalyteCatalog.from_file(settings.analytes_file)
    metrics = metrics or QcMetrics()

    fabric: IdentityFabricClient | None = None
    credentials: AgentCredentials | None = None
    fabric_url = settings.fabric_url_str
    if fabric_url is not None and settings.workload_token_file is not None:
        fabric = IdentityFabricClient(fabric_url, http=http)
        credentials = AgentCredentials(
            fabric,
            agent_id=settings.agent_id,
            workload_token_file=settings.workload_token_file,
            clock=clock.now,
        )

    authorizer = Authorizer(
        PolicyEngine(PolicySet.from_file(settings.policy_file)), audit, fabric=fabric
    )
    authenticator = Authenticator(
        oidc_resolver or RemoteJwks(settings.oidc_jwks_url, client=http),
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        roles_claim=settings.roles_claim,
        name_claim=settings.name_claim,
        fabric=fabric,
    )
    model_router = router if router is not None else build_router(settings)
    repos = Repositories(
        targets=SqlTargetRepository(engine),
        results=SqlResultRepository(engine),
        evaluations=SqlEvaluationRepository(engine),
        events=SqlEventRepository(engine),
        investigations=SqlInvestigationRepository(engine),
        capas=SqlCapaRepository(engine),
        holds=SqlHoldRepository(engine),
    )
    ctx = AgentContext(
        repos=repos,
        authorizer=authorizer,
        audit=audit,
        clock=clock,
        metrics=metrics,
        catalog=catalog,
    )
    identity = AgentIdentity(
        settings.agent_id,
        scopes=frozenset(settings.agent_scopes.split()),
        credentials=credentials,
        fabric_issuer=fabric_url,
    )
    workflow = QcWorkflow(
        ctx,
        identity,
        InvestigationAgent(
            ctx,
            NarrativeWriter(model_router, ContentGuard()),
            lookback=timedelta(hours=settings.investigation_lookback_hours),
        ),
        CorrectiveActionAgent(ctx),
        VerificationAgent(
            ctx,
            VerificationPolicy(
                min_runs=settings.verification_min_runs,
                min_runs_systematic=settings.verification_min_runs_systematic,
            ),
        ),
    )
    service = QualityControlService(
        ctx,
        workflow,
        site_timezone=settings.zone,
        target_min_points=settings.target_min_points,
        sigma_min_points=settings.sigma_min_points,
    )
    return Container(
        settings=settings,
        service=service,
        authenticator=authenticator,
        authorizer=authorizer,
        metrics=metrics,
        engine=engine,
        http=http,
        audit=audit,
        router=model_router,
    )
