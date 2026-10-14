"""Shared fixtures: an in-process OIDC issuer, a fixed clock, a container on SQLite, an app.

All data is synthetic: a glucose method on a fictitious analyser with two
control levels (level 1 mean 100 mg/dL SD 2.5; level 2 mean 300 mg/dL SD 6).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from lab_quality_control_agents.api.app import create_app
from lab_quality_control_agents.application.commands import (
    RunResultInput,
    RunSubmission,
    TargetInput,
)
from lab_quality_control_agents.application.service import QualityControlService
from lab_quality_control_agents.config import Settings
from lab_quality_control_agents.runtime.container import Container, build_container
from lab_quality_control_agents.security.authn import Principal, PrincipalKind
from lab_quality_control_agents.security.jwks import StaticJwks

ROOT = Path(__file__).resolve().parents[1]
OIDC_ISSUER = "https://login.lab.test/tenant"
AUDIENCE = "lab-quality-control"
AGENT_ID = "spiffe://lab.test/agent/lab-qc/qc-agents"
INSTRUMENT = "CHEM-01"
START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

LEVELS = {
    1: {"material_id": "GLU-L1", "lot": "L1-2026A", "mean": 100.0, "sd": 2.5},
    2: {"material_id": "GLU-L2", "lot": "L2-2026A", "mean": 300.0, "sd": 6.0},
}


@dataclass
class FakeIdp:
    """Minimal OIDC issuer: ES256 keypair plus JWKS."""

    issuer: str
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str = AUDIENCE, ttl: int = 300, **extra: Any) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl}
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


@dataclass
class FakeClock:
    current: datetime = START

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    db = os.environ.get("LABQC_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'qc.db'}")
    values: dict[str, Any] = {
        "environment": "test",
        "public_url": "https://lab-qc.lab.test",
        "database_url": db,
        "dev_ephemeral_audit_key": True,
        "audit_log_path": tmp_path / "audit.jsonl",
        "oidc_issuer": OIDC_ISSUER,
        "oidc_audience": AUDIENCE,
        "oidc_jwks_url": "https://login.lab.test/tenant/jwks",
        "agent_id": AGENT_ID,
        "policy_file": ROOT / "policies" / "lab-qc.yaml",
        "analytes_file": ROOT / "configs" / "analytes.yaml",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp(OIDC_ISSUER)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def container(settings: Settings, idp: FakeIdp, clock: FakeClock) -> AsyncIterator[Container]:
    built = await build_container(settings, oidc_resolver=StaticJwks(idp.jwks()), clock=clock)
    yield built
    await built.aclose()


@pytest.fixture
def service(container: Container) -> QualityControlService:
    return container.service


def human(subject: str = "tess@lab.test", *roles: str, name: str = "Tess Tech") -> Principal:
    return Principal(
        subject=subject,
        kind=PrincipalKind.HUMAN,
        name=name,
        roles=frozenset(roles or ("qc-technologist",)),
        issuer=OIDC_ISSUER,
    )


TECH = human("tess@lab.test", "qc-technologist")
TECH2 = human("omar@lab.test", "qc-technologist", name="Omar Ops")
SUPERVISOR = human("sam@lab.test", "qc-supervisor", name="Sam Supervisor")
SUPERVISOR2 = human("rae@lab.test", "qc-supervisor", name="Rae Reviewer")
AUDITOR = human("ari@lab.test", "qc-auditor", name="Ari Auditor")
INTERFACE = Principal(
    subject="lis-interface",
    kind=PrincipalKind.SERVICE,
    name="LIS interface engine",
    roles=frozenset({"qc-interface"}),
    issuer=OIDC_ISSUER,
)


def target_input(level: int, *, analyte: str = "GLU", instrument: str = INSTRUMENT) -> TargetInput:
    spec = LEVELS[level]
    return TargetInput(
        analyte=analyte,
        instrument_id=instrument,
        material_id=str(spec["material_id"]),
        control_lot=str(spec["lot"]),
        level=level,
        mean=float(spec["mean"]),
        sd=float(spec["sd"]),
        units="mg/dL",
    )


async def seed_targets(service: QualityControlService, instrument: str = INSTRUMENT) -> None:
    for level in (1, 2):
        await service.put_target(SUPERVISOR, target_input(level, instrument=instrument))


def value(level: int, z: float) -> float:
    spec = LEVELS[level]
    return round(float(spec["mean"]) + z * float(spec["sd"]), 4)


def run_submission(
    run_id: str,
    hours: float,
    z1: float,
    z2: float,
    *,
    reagent_lot: str | None = "R-100",
    instrument: str = INSTRUMENT,
) -> RunSubmission:
    when = START + timedelta(hours=hours)
    return RunSubmission(
        analyte="GLU",
        instrument_id=instrument,
        run_id=run_id,
        results=tuple(
            RunResultInput(
                material_id=str(LEVELS[level]["material_id"]),
                control_lot=str(LEVELS[level]["lot"]),
                value=value(level, z),
                units="mg/dL",
                observed_at=when,
                reagent_lot=reagent_lot,
            )
            for level, z in ((1, z1), (2, z2))
        ),
    )


def bearer(idp: FakeIdp, sub: str, *roles: str, **extra: Any) -> dict[str, str]:
    name = sub.split("@", maxsplit=1)[0].title()
    token = idp.mint(sub=sub, roles=list(roles), name=name, **extra)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(settings: Settings, idp: FakeIdp, clock: FakeClock) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""

    async def factory() -> Container:
        return await build_container(settings, oidc_resolver=StaticJwks(idp.jwks()), clock=clock)

    with TestClient(create_app(factory, mcp_allowed_hosts=["testserver"])) as test_client:
        yield test_client
