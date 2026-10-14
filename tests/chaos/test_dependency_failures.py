"""Fault injection: QC decisions and holds survive dependency failures; nothing fails open."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from lab_quality_control_agents.application.service import QualityControlService
from lab_quality_control_agents.domain.errors import UnauthorizedError
from lab_quality_control_agents.domain.models import RunOutcome
from lab_quality_control_agents.infrastructure.database import make_engine
from lab_quality_control_agents.runtime.container import build_container, build_router
from lab_quality_control_agents.security.jwks import RemoteJwks, StaticJwks
from tests.conftest import (
    INTERFACE,
    TECH,
    FakeClock,
    FakeIdp,
    make_settings,
    run_submission,
    seed_targets,
)
from tests.unit.test_narrative import ENV

pytestmark = pytest.mark.chaos

PROVIDERS = Path(__file__).resolve().parents[2] / "configs" / "providers.yaml"


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200  # liveness unaffected
    finally:
        container.engine = healthy


async def test_identity_fabric_outage_keeps_the_hold(tmp_path: Path, idp: FakeIdp) -> None:
    """The agents cannot obtain credentials; the run is still rejected and held."""

    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    token_file = tmp_path / "sa-token"
    token_file.write_text("projected-token", encoding="utf-8")
    settings = make_settings(
        tmp_path, identity_fabric_url="https://identity.lab.test", workload_token_file=token_file
    )
    container = await build_container(
        settings,
        oidc_resolver=StaticJwks(idp.jwks()),
        clock=FakeClock(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(down)),
    )
    try:
        await seed_targets(container.service)
        report = await container.service.submit_run(INTERFACE, run_submission("r1", 1, 3.6, 0.0))
        assert report.evaluations[0].outcome is RunOutcome.REJECT
        assert report.holds_placed
        assert report.investigations == ()
        assert any("identity fabric returned 503" in e for e in report.workflow_errors)
    finally:
        await container.aclose()


async def test_model_provider_outage_still_investigates(tmp_path: Path, idp: FakeIdp) -> None:
    calls: list[str] = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        raise httpx.ConnectError("refused", request=request)

    settings = make_settings(tmp_path, providers_file=PROVIDERS)
    router = build_router(settings, env=ENV, transport=httpx.MockTransport(down))
    assert router is not None
    router._retry = router._retry.model_copy(update={"max_attempts": 1})
    container = await build_container(
        settings, oidc_resolver=StaticJwks(idp.jwks()), clock=FakeClock(), router=router
    )
    try:
        await seed_targets(container.service)
        report = await container.service.submit_run(INTERFACE, run_submission("r1", 1, 3.6, 0.0))
        investigation = await container.service.get_investigation(TECH, report.investigations[0])
        assert investigation.narrative is None
        assert investigation.hypotheses
        assert set(calls) == {"aoai.lab.test", "foundry.lab.test"}
        exposition = generate_latest(container.metrics.registry).decode()
        assert 'labqc_narratives_total{outcome="unavailable"} 1.0' in exposition
    finally:
        await container.aclose()


async def test_audit_sink_failure_blocks_the_action(service: QualityControlService) -> None:
    """If evidence cannot be written, the evaluation must not complete silently."""
    await seed_targets(service)

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    service._ctx.audit._sink.append = broken_append  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        await service.submit_run(INTERFACE, run_submission("r1", 1, 0.1, 0.1))


async def test_oidc_jwks_outage_fails_closed(idp: FakeIdp) -> None:
    from lab_quality_control_agents.security.authn import Authenticator

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        auth = Authenticator(
            RemoteJwks("https://login.lab.test/jwks", client=http),
            issuer=idp.issuer,
            audience="lab-quality-control",
        )
        with pytest.raises(UnauthorizedError, match="cannot fetch"):
            await auth.authenticate(f"Bearer {idp.mint(sub='x')}")


async def test_concurrent_redelivery_is_idempotent(service: QualityControlService) -> None:
    """An interface engine retrying the same message concurrently stores it once."""
    await seed_targets(service)
    submission = run_submission("dup", 1, 0.2, -0.1)
    reports = await asyncio.gather(
        *(service.submit_run(INTERFACE, submission) for _ in range(5)), return_exceptions=True
    )
    stored = sum(r.stored for r in reports if not isinstance(r, BaseException))
    evaluations = sum(len(r.evaluations) for r in reports if not isinstance(r, BaseException))
    assert stored == 2
    assert evaluations == 1
    assert all(
        not isinstance(r, BaseException) or type(r).__name__ == "ConflictError" for r in reports
    )
