"""Settings, container wiring (local and fabric modes), persistence edge cases, contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from lab_quality_control_agents.contracts import main as contracts_main
from lab_quality_control_agents.domain.errors import ConflictError, NotFoundError
from lab_quality_control_agents.domain.models import CapaStatus, QcHold, RunOutcome
from lab_quality_control_agents.infrastructure.database import (
    SqlCapaRepository,
    SqlEvaluationRepository,
    SqlHoldRepository,
    create_schema,
    make_engine,
    utc,
)
from lab_quality_control_agents.runtime.container import build_container
from lab_quality_control_agents.security.jwks import StaticJwks
from tests.conftest import (
    AGENT_ID,
    INTERFACE,
    ROOT,
    START,
    FakeClock,
    FakeIdp,
    make_settings,
    run_submission,
    seed_targets,
)

FABRIC = "https://identity.lab.test"


class TestSettings:
    def test_production_requires_an_audit_key(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="AUDIT_KEY_FILE"):
            make_settings(tmp_path, dev_ephemeral_audit_key=False)
        with pytest.raises(ValidationError, match="cannot be used in production"):
            make_settings(tmp_path, environment="production")

    def test_fabric_requires_workload_token(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="WORKLOAD_TOKEN_FILE"):
            make_settings(tmp_path, identity_fabric_url=FABRIC)

    def test_rejects_unknown_zone_and_bad_agent_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="unknown time zone"):
            make_settings(tmp_path, site_timezone="Mars/Olympus")
        with pytest.raises(ValidationError):
            make_settings(tmp_path, agent_id="qc-agent")

    def test_properties(self, tmp_path: Path) -> None:
        settings = make_settings(
            tmp_path,
            site_timezone="Europe/Berlin",
            identity_fabric_url=FABRIC + "/",
            workload_token_file=tmp_path / "t",
        )
        assert settings.zone.key == "Europe/Berlin"
        assert settings.public_url_str == "https://lab-qc.lab.test"
        assert settings.fabric_url_str == FABRIC


async def test_audit_key_file_is_loaded(tmp_path: Path, idp: FakeIdp) -> None:
    key_file = tmp_path / "audit-2026-10.pem"
    key_file.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = make_settings(
        tmp_path, environment="production", dev_ephemeral_audit_key=False, audit_key_file=key_file
    )
    container = await build_container(settings, oidc_resolver=StaticJwks(idp.jwks()))
    assert container.audit._key_id == "audit-2026-10"
    await container.aclose()

    wrong = tmp_path / "audit-ec.pem"
    wrong.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="Ed25519"):
        await build_container(
            make_settings(
                tmp_path,
                environment="production",
                dev_ephemeral_audit_key=False,
                audit_key_file=wrong,
            ),
            oidc_resolver=StaticJwks(idp.jwks()),
        )


def fabric_handler(seen: list[str], decisions: dict[str, str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fabric-agent-token",
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "scope": "qc:read investigation:run capa:propose capa:read capa:verify",
                },
            )
        if request.url.path == "/v1/authorize":
            action = json.loads(request.content)["action"]
            return httpx.Response(
                200,
                json={
                    "decision": decisions.get(action, "allow"),
                    "agent": AGENT_ID,
                    "action": action,
                    "resource": "x",
                    "reason": "fabric policy",
                },
            )
        return httpx.Response(404, json={"error": "not_found"})

    return handler


async def fabric_container(
    tmp_path: Path, idp: FakeIdp, seen: list[str], decisions: dict[str, str]
) -> Any:
    token_file = tmp_path / "sa-token"
    token_file.write_text("projected-service-account-token", encoding="utf-8")
    settings = make_settings(tmp_path, identity_fabric_url=FABRIC, workload_token_file=token_file)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fabric_handler(seen, decisions)))
    return await build_container(
        settings, oidc_resolver=StaticJwks(idp.jwks()), clock=FakeClock(), http=http
    )


async def test_agents_act_under_fabric_credentials(tmp_path: Path, idp: FakeIdp) -> None:
    seen: list[str] = []
    container = await fabric_container(tmp_path, idp, seen, {})
    try:
        await seed_targets(container.service)
        report = await container.service.submit_run(INTERFACE, run_submission("r1", 1, 3.6, 0.0))
        assert report.investigations
        assert report.capas_proposed
        assert seen.count("/oauth2/token") == 1  # credential cached across both agent steps
        assert seen.count("/v1/authorize") == 2  # investigation:run and capa:propose
    finally:
        await container.aclose()


async def test_fabric_denial_stops_the_agent_but_not_the_hold(tmp_path: Path, idp: FakeIdp) -> None:
    seen: list[str] = []
    container = await fabric_container(tmp_path, idp, seen, {"capa:propose": "deny"})
    try:
        await seed_targets(container.service)
        report = await container.service.submit_run(INTERFACE, run_submission("r1", 1, 3.6, 0.0))
        assert report.holds_placed
        assert report.investigations
        assert report.capas_proposed == ()
        assert any("propose_capa" in e for e in report.workflow_errors)
    finally:
        await container.aclose()


class TestPersistence:
    async def test_capa_optimistic_concurrency_and_missing_rows(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        await create_schema(engine)
        await create_schema(engine)  # idempotent
        from tests.unit.test_capa_and_verification import proposed

        repo = SqlCapaRepository(engine)
        capa = proposed()
        await repo.add(capa)
        bumped = capa.model_copy(update={"version": 2, "status": CapaStatus.APPROVED})
        await repo.update(bumped, expected_version=1)
        with pytest.raises(ConflictError, match="concurrently"):
            await repo.update(bumped, expected_version=1)
        with pytest.raises(NotFoundError):
            await repo.update(capa.model_copy(update={"capa_id": "nope"}), expected_version=1)
        assert (await repo.open_for("GLU", "CHEM-01")) == bumped

        holds = SqlHoldRepository(engine)
        with pytest.raises(NotFoundError):
            await holds.update(
                QcHold(
                    hold_id="h",
                    analyte="GLU",
                    instrument_id="CHEM-01",
                    evaluation_id="e",
                    placed_at=START,
                )
            )
        await engine.dispose()

    async def test_duplicate_evaluation_conflicts(self, tmp_path: Path) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
        await create_schema(engine)
        from tests.unit.test_narrative import EVALUATION

        repo = SqlEvaluationRepository(engine)
        await repo.add(EVALUATION)
        with pytest.raises(ConflictError, match="already been evaluated"):
            await repo.add(EVALUATION.model_copy(update={"evaluation_id": "ev-2"}))
        assert await repo.get("missing") is None
        assert (
            await repo.list(
                outcome=RunOutcome.REJECT, since=START, until=EVALUATION.run_observed_at
            )
        ) == [EVALUATION]
        await engine.dispose()

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        naive = START.replace(tzinfo=None)
        assert utc(naive) == START


class TestContracts:
    def test_committed_contracts_are_current(self) -> None:
        assert contracts_main(["--check"], root=ROOT) == 0

    def test_export_and_check_round_trip(self, tmp_path: Path) -> None:
        assert contracts_main(["--check"], root=tmp_path) == 1
        assert contracts_main([], root=tmp_path) == 0
        assert contracts_main(["--check"], root=tmp_path) == 0
        assert (tmp_path / "schemas" / "run-submission.request.schema.json").is_file()
        assert (tmp_path / "docs" / "api" / "openapi.json").is_file()
