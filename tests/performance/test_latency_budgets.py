"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lab_quality_control_agents.application.service import QualityControlService
from lab_quality_control_agents.domain.westgard import Observation, WestgardEngine
from lab_quality_control_agents.integrations import astm, hl7
from tests.conftest import INTERFACE, run_submission, seed_targets
from tests.unit.test_astm import RECORDS
from tests.unit.test_hl7 import TWO_LEVELS

pytestmark = pytest.mark.performance


def best_ms(fn: Callable[[], object], *, batches: int = 5, per_batch: int = 100) -> float:
    """Fastest per-call time over several batches: robust to noisy shared runners."""
    best = float("inf")
    for _ in range(batches):
        started = time.perf_counter()
        for _ in range(per_batch):
            fn()
        best = min(best, (time.perf_counter() - started) * 1000 / per_batch)
    return best


def test_westgard_evaluation_budget() -> None:
    rng = random.Random(7)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [
        Observation(f"h{i}-{lvl}", lvl, rng.gauss(0, 1), start + timedelta(hours=i), f"r{i}")
        for i in range(100)
        for lvl in (1, 2)
    ]
    engine = WestgardEngine()
    current = [
        Observation("c1", 1, 0.4, start + timedelta(days=30), "c"),
        Observation("c2", 2, -0.3, start + timedelta(days=30), "c"),
    ]
    assert best_ms(lambda: engine.evaluate(current, history)) < 5


def test_message_parsing_budget() -> None:
    utc = ZoneInfo("UTC")
    framed = astm.build_frames(RECORDS)
    assert best_ms(lambda: hl7.extract_qc(TWO_LEVELS, default_tz=utc)) < 5
    assert best_ms(lambda: astm.extract_qc(framed, framed=True, default_tz=utc)) < 5


async def test_ingest_and_evaluate_budget(service: QualityControlService) -> None:
    """Steady-state p95 of the full ingestion path (SLO: 500 ms) on SQLite."""
    await seed_targets(service)
    rng = random.Random(11)
    for i in range(3):  # warm-up: schema, connection pool, audit file creation
        await service.submit_run(INTERFACE, run_submission(f"w{i}", i, 0.1, -0.1))
    timings = []
    for i in range(40):
        z1, z2 = rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5)
        submission = run_submission(f"p{i}", 10 + i, z1, z2)
        started = time.perf_counter()
        await service.submit_run(INTERFACE, submission)
        timings.append(time.perf_counter() - started)
    timings.sort()
    p95_ms = timings[int(len(timings) * 0.95) - 1] * 1000
    assert p95_ms < 500
