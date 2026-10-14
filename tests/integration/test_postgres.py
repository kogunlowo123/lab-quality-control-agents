"""Repositories and a full QC cycle against a real PostgreSQL (CI service container).

Runs when LABQC_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://labqc:labqc@localhost:5432/labqc
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab_quality_control_agents.domain.models import CapaStatus, RunOutcome
from lab_quality_control_agents.infrastructure.database import (
    SqlCapaRepository,
    SqlResultRepository,
    create_schema,
    make_engine,
)
from lab_quality_control_agents.runtime.container import build_container
from lab_quality_control_agents.security.jwks import StaticJwks
from tests.conftest import (
    INTERFACE,
    SUPERVISOR,
    FakeClock,
    FakeIdp,
    make_settings,
    run_submission,
    seed_targets,
)

URL = os.environ.get("LABQC_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_schema_is_idempotent_and_timezones_round_trip() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)
    repo = SqlResultRepository(engine)
    assert await repo.add_new([]) == []
    assert await SqlCapaRepository(engine).get("absent") is None
    await engine.dispose()


async def test_qc_cycle_on_postgres(tmp_path: Path) -> None:
    idp = FakeIdp("https://login.lab.test/tenant")
    instrument = f"PG-{uuid.uuid4().hex[:8].upper()}"
    clock = FakeClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    container = await build_container(
        make_settings(tmp_path, database_url=URL), oidc_resolver=StaticJwks(idp.jwks()), clock=clock
    )
    try:
        service = container.service
        await seed_targets(service, instrument=instrument)
        ok = await service.submit_run(
            INTERFACE, run_submission("pg-1", 0, 0.2, -0.3, instrument=instrument)
        )
        assert ok.evaluations[0].outcome is RunOutcome.ACCEPT
        again = await service.submit_run(
            INTERFACE, run_submission("pg-1", 0, 0.2, -0.3, instrument=instrument)
        )
        assert again.duplicates == 2
        failed = await service.submit_run(
            INTERFACE, run_submission("pg-2", 1, 3.6, 0.0, instrument=instrument)
        )
        assert failed.evaluations[0].outcome is RunOutcome.REJECT
        capa = await service.get_capa(SUPERVISOR, failed.capas_proposed[0])
        assert capa.status is CapaStatus.PROPOSED
        holds = [
            h
            for h in await service.list_holds(SUPERVISOR, active_only=True)
            if h.instrument_id == instrument
        ]
        assert len(holds) == 1
        history = await container.service._repos.results.query(
            analyte="GLU", instrument_id=instrument, since=clock.current - timedelta(days=1)
        )
        assert [r.run_id for r in history] == ["pg-1", "pg-1", "pg-2", "pg-2"]
        assert history[0].observed_at.tzinfo is not None
    finally:
        await container.aclose()
