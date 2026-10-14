"""Ports the application layer depends on. Adapters live in infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from lab_quality_control_agents.domain.models import (
    Capa,
    CapaStatus,
    InstrumentEvent,
    InstrumentEventKind,
    Investigation,
    QcHold,
    QcResult,
    QcTarget,
    RunEvaluation,
    RunOutcome,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class TargetRepository(Protocol):
    async def put(self, target: QcTarget) -> None: ...

    async def get(self, analyte: str, instrument_id: str, material_id: str) -> QcTarget | None: ...

    async def list(
        self, *, analyte: str | None = None, instrument_id: str | None = None
    ) -> Sequence[QcTarget]: ...


class ResultRepository(Protocol):
    async def add_new(self, results: Sequence[QcResult]) -> list[QcResult]:
        """Insert results whose source_key is unseen; return the inserted ones."""
        ...

    async def query(
        self,
        *,
        analyte: str,
        instrument_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        material_id: str | None = None,
        limit: int = 500,
    ) -> Sequence[QcResult]:
        """Most recent ``limit`` results in chronological order."""
        ...

    async def for_run(
        self, analyte: str, instrument_id: str, run_id: str
    ) -> Sequence[QcResult]: ...


class EvaluationRepository(Protocol):
    async def add(self, evaluation: RunEvaluation) -> None: ...

    async def get(self, evaluation_id: str) -> RunEvaluation | None: ...

    async def find_run(
        self, analyte: str, instrument_id: str, run_id: str
    ) -> RunEvaluation | None: ...

    async def list(
        self,
        *,
        outcome: RunOutcome | None = None,
        analyte: str | None = None,
        instrument_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[RunEvaluation]: ...

    async def last_accepted_before(
        self, analyte: str, instrument_id: str, before: datetime
    ) -> RunEvaluation | None: ...


class EventRepository(Protocol):
    async def add(self, event: InstrumentEvent) -> None: ...

    async def between(
        self, instrument_id: str, since: datetime, until: datetime
    ) -> Sequence[InstrumentEvent]: ...

    async def latest(
        self, instrument_id: str, kind: InstrumentEventKind, analyte: str, before: datetime
    ) -> InstrumentEvent | None:
        """Latest event of ``kind`` for the analyte or instrument-wide, before a time."""
        ...


class InvestigationRepository(Protocol):
    async def add(self, investigation: Investigation) -> None: ...

    async def get(self, investigation_id: str) -> Investigation | None: ...

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[Investigation]: ...


class CapaRepository(Protocol):
    async def add(self, capa: Capa) -> None: ...

    async def get(self, capa_id: str) -> Capa | None: ...

    async def update(self, capa: Capa, *, expected_version: int) -> None: ...

    async def list(
        self, *, status: CapaStatus | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[Capa]: ...

    async def open_for(self, analyte: str, instrument_id: str) -> Capa | None: ...


class HoldRepository(Protocol):
    async def add(self, hold: QcHold) -> None: ...

    async def active(self, analyte: str, instrument_id: str) -> QcHold | None: ...

    async def list(self, *, active_only: bool = True) -> Sequence[QcHold]: ...

    async def update(self, hold: QcHold) -> None: ...

    async def last_released(self, analyte: str, instrument_id: str) -> QcHold | None: ...


@dataclass(frozen=True)
class Repositories:
    targets: TargetRepository
    results: ResultRepository
    evaluations: EvaluationRepository
    events: EventRepository
    investigations: InvestigationRepository
    capas: CapaRepository
    holds: HoldRepository
