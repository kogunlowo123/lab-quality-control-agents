"""SQLAlchemy Core persistence. PostgreSQL in production; SQLite in tests.

Aggregates are stored as JSON documents with the columns needed for lookup
indexed alongside. Timestamps in indexed columns are normalised to UTC so
comparisons behave the same on dialects without timezone support.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lab_quality_control_agents.domain.errors import ConflictError, NotFoundError
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

metadata = MetaData()

qc_targets = Table(
    "qc_targets",
    metadata,
    Column("analyte", String(64), primary_key=True),
    Column("instrument_id", String(64), primary_key=True),
    Column("material_id", String(64), primary_key=True),
    Column("document", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

qc_results = Table(
    "qc_results",
    metadata,
    Column("result_id", String(64), primary_key=True),
    Column("source_key", String(256), nullable=False, unique=True),
    Column("analyte", String(64), nullable=False),
    Column("instrument_id", String(64), nullable=False),
    Column("material_id", String(64), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_results_series", "analyte", "instrument_id", "observed_at"),
    Index("ix_results_run", "analyte", "instrument_id", "run_id"),
)

qc_evaluations = Table(
    "qc_evaluations",
    metadata,
    Column("evaluation_id", String(64), primary_key=True),
    Column("analyte", String(64), nullable=False),
    Column("instrument_id", String(64), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("run_observed_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("analyte", "instrument_id", "run_id", name="uq_evaluation_run"),
    Index("ix_evaluations_series", "analyte", "instrument_id", "run_observed_at"),
    Index("ix_evaluations_outcome", "outcome", "run_observed_at"),
)

instrument_events = Table(
    "instrument_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("instrument_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("analyte", String(64), nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_events_instrument", "instrument_id", "occurred_at"),
)

investigations = Table(
    "investigations",
    metadata,
    Column("investigation_id", String(64), primary_key=True),
    Column("evaluation_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_investigations_created", "created_at"),
)

capas = Table(
    "capas",
    metadata,
    Column("capa_id", String(64), primary_key=True),
    Column("status", String(24), nullable=False),
    Column("analyte", String(64), nullable=False),
    Column("instrument_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_capas_status", "status", "created_at"),
    Index("ix_capas_series", "analyte", "instrument_id"),
)

qc_holds = Table(
    "qc_holds",
    metadata,
    Column("hold_id", String(64), primary_key=True),
    Column("analyte", String(64), nullable=False),
    Column("instrument_id", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("placed_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=True),
    Column("document", Text, nullable=False),
    Index("ix_holds_series", "analyte", "instrument_id", "active"),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def utc(value: datetime) -> datetime:
    """Normalise to UTC for indexed columns (SQLite stores the UTC wall time)."""
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


class SqlTargetRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def put(self, target: QcTarget) -> None:
        key = and_(
            qc_targets.c.analyte == target.analyte,
            qc_targets.c.instrument_id == target.instrument_id,
            qc_targets.c.material_id == target.material_id,
        )
        values = {"document": target.model_dump_json(), "updated_at": utc(target.effective_from)}
        async with self._engine.begin() as conn:
            exists = (await conn.execute(select(qc_targets.c.analyte).where(key))).first()
            if exists:
                await conn.execute(update(qc_targets).where(key).values(**values))
            else:
                await conn.execute(
                    insert(qc_targets).values(
                        analyte=target.analyte,
                        instrument_id=target.instrument_id,
                        material_id=target.material_id,
                        **values,
                    )
                )

    async def get(self, analyte: str, instrument_id: str, material_id: str) -> QcTarget | None:
        query = select(qc_targets.c.document).where(
            qc_targets.c.analyte == analyte,
            qc_targets.c.instrument_id == instrument_id,
            qc_targets.c.material_id == material_id,
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return QcTarget.model_validate_json(row[0]) if row else None

    async def list(
        self, *, analyte: str | None = None, instrument_id: str | None = None
    ) -> Sequence[QcTarget]:
        query = select(qc_targets.c.document).order_by(
            qc_targets.c.analyte, qc_targets.c.instrument_id, qc_targets.c.material_id
        )
        if analyte is not None:
            query = query.where(qc_targets.c.analyte == analyte)
        if instrument_id is not None:
            query = query.where(qc_targets.c.instrument_id == instrument_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [QcTarget.model_validate_json(r[0]) for r in rows]


class SqlResultRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_new(self, results: Sequence[QcResult]) -> list[QcResult]:
        if not results:
            return []
        keys = [r.source_key for r in results]
        try:
            async with self._engine.begin() as conn:
                existing = {
                    row[0]
                    for row in await conn.execute(
                        select(qc_results.c.source_key).where(qc_results.c.source_key.in_(keys))
                    )
                }
                fresh: list[QcResult] = []
                seen: set[str] = set()
                for r in results:
                    if r.source_key in existing or r.source_key in seen:
                        continue
                    seen.add(r.source_key)
                    fresh.append(r)
                if fresh:
                    await conn.execute(
                        insert(qc_results),
                        [
                            {
                                "result_id": r.result_id,
                                "source_key": r.source_key,
                                "analyte": r.analyte,
                                "instrument_id": r.instrument_id,
                                "material_id": r.material_id,
                                "run_id": r.run_id,
                                "observed_at": utc(r.observed_at),
                                "document": r.model_dump_json(),
                            }
                            for r in fresh
                        ],
                    )
        except IntegrityError as exc:
            raise ConflictError("concurrent delivery of the same QC results; retry") from exc
        return fresh

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
        query = (
            select(qc_results.c.document)
            .where(qc_results.c.analyte == analyte, qc_results.c.instrument_id == instrument_id)
            .order_by(qc_results.c.observed_at.desc(), qc_results.c.result_id.desc())
            .limit(limit)
        )
        if since is not None:
            query = query.where(qc_results.c.observed_at > utc(since))
        if until is not None:
            query = query.where(qc_results.c.observed_at <= utc(until))
        if material_id is not None:
            query = query.where(qc_results.c.material_id == material_id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [QcResult.model_validate_json(r[0]) for r in reversed(rows)]

    async def for_run(self, analyte: str, instrument_id: str, run_id: str) -> Sequence[QcResult]:
        query = (
            select(qc_results.c.document)
            .where(
                qc_results.c.analyte == analyte,
                qc_results.c.instrument_id == instrument_id,
                qc_results.c.run_id == run_id,
            )
            .order_by(qc_results.c.observed_at, qc_results.c.result_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [QcResult.model_validate_json(r[0]) for r in rows]


class SqlEvaluationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, evaluation: RunEvaluation) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(qc_evaluations).values(
                        evaluation_id=evaluation.evaluation_id,
                        analyte=evaluation.analyte,
                        instrument_id=evaluation.instrument_id,
                        run_id=evaluation.run_id,
                        outcome=evaluation.outcome.value,
                        run_observed_at=utc(evaluation.run_observed_at),
                        document=evaluation.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(f"run {evaluation.run_id} has already been evaluated") from exc

    async def get(self, evaluation_id: str) -> RunEvaluation | None:
        query = select(qc_evaluations.c.document).where(
            qc_evaluations.c.evaluation_id == evaluation_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return RunEvaluation.model_validate_json(row[0]) if row else None

    async def find_run(self, analyte: str, instrument_id: str, run_id: str) -> RunEvaluation | None:
        query = select(qc_evaluations.c.document).where(
            qc_evaluations.c.analyte == analyte,
            qc_evaluations.c.instrument_id == instrument_id,
            qc_evaluations.c.run_id == run_id,
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return RunEvaluation.model_validate_json(row[0]) if row else None

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
    ) -> Sequence[RunEvaluation]:
        query = (
            select(qc_evaluations.c.document)
            .order_by(qc_evaluations.c.run_observed_at.desc(), qc_evaluations.c.evaluation_id)
            .limit(limit)
            .offset(offset)
        )
        if outcome is not None:
            query = query.where(qc_evaluations.c.outcome == outcome.value)
        if analyte is not None:
            query = query.where(qc_evaluations.c.analyte == analyte)
        if instrument_id is not None:
            query = query.where(qc_evaluations.c.instrument_id == instrument_id)
        if since is not None:
            query = query.where(qc_evaluations.c.run_observed_at >= utc(since))
        if until is not None:
            query = query.where(qc_evaluations.c.run_observed_at <= utc(until))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [RunEvaluation.model_validate_json(r[0]) for r in rows]

    async def last_accepted_before(
        self, analyte: str, instrument_id: str, before: datetime
    ) -> RunEvaluation | None:
        query = (
            select(qc_evaluations.c.document)
            .where(
                qc_evaluations.c.analyte == analyte,
                qc_evaluations.c.instrument_id == instrument_id,
                qc_evaluations.c.outcome != RunOutcome.REJECT.value,
                qc_evaluations.c.run_observed_at < utc(before),
            )
            .order_by(qc_evaluations.c.run_observed_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return RunEvaluation.model_validate_json(row[0]) if row else None


class SqlEventRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, event: InstrumentEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(instrument_events).values(
                    event_id=event.event_id,
                    instrument_id=event.instrument_id,
                    kind=event.kind.value,
                    analyte=event.analyte,
                    occurred_at=utc(event.occurred_at),
                    document=event.model_dump_json(),
                )
            )

    async def between(
        self, instrument_id: str, since: datetime, until: datetime
    ) -> Sequence[InstrumentEvent]:
        query = (
            select(instrument_events.c.document)
            .where(
                instrument_events.c.instrument_id == instrument_id,
                instrument_events.c.occurred_at >= utc(since),
                instrument_events.c.occurred_at <= utc(until),
            )
            .order_by(instrument_events.c.occurred_at)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [InstrumentEvent.model_validate_json(r[0]) for r in rows]

    async def latest(
        self, instrument_id: str, kind: InstrumentEventKind, analyte: str, before: datetime
    ) -> InstrumentEvent | None:
        query = (
            select(instrument_events.c.document)
            .where(
                instrument_events.c.instrument_id == instrument_id,
                instrument_events.c.kind == kind.value,
                or_(instrument_events.c.analyte == analyte, instrument_events.c.analyte.is_(None)),
                instrument_events.c.occurred_at <= utc(before),
            )
            .order_by(instrument_events.c.occurred_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return InstrumentEvent.model_validate_json(row[0]) if row else None


class SqlInvestigationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, investigation: Investigation) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(investigations).values(
                    investigation_id=investigation.investigation_id,
                    evaluation_id=investigation.evaluation_id,
                    created_at=utc(investigation.created_at),
                    document=investigation.model_dump_json(),
                )
            )

    async def get(self, investigation_id: str) -> Investigation | None:
        query = select(investigations.c.document).where(
            investigations.c.investigation_id == investigation_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return Investigation.model_validate_json(row[0]) if row else None

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[Investigation]:
        query = (
            select(investigations.c.document)
            .order_by(investigations.c.created_at.desc(), investigations.c.investigation_id)
            .limit(limit)
            .offset(offset)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Investigation.model_validate_json(r[0]) for r in rows]


_OPEN = [
    CapaStatus.PROPOSED.value,
    CapaStatus.APPROVED.value,
    CapaStatus.ACTION_COMPLETED.value,
    CapaStatus.VERIFIED.value,
]


class SqlCapaRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, capa: Capa) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(capas).values(
                    capa_id=capa.capa_id,
                    status=capa.status.value,
                    analyte=capa.analyte,
                    instrument_id=capa.instrument_id,
                    version=capa.version,
                    created_at=utc(capa.created_at),
                    document=capa.model_dump_json(),
                )
            )

    async def get(self, capa_id: str) -> Capa | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(capas.c.document).where(capas.c.capa_id == capa_id))
            ).first()
        return Capa.model_validate_json(row[0]) if row else None

    async def update(self, capa: Capa, *, expected_version: int) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(capas)
                .where(capas.c.capa_id == capa.capa_id, capas.c.version == expected_version)
                .values(
                    status=capa.status.value, version=capa.version, document=capa.model_dump_json()
                )
            )
            if result.rowcount == 0:
                exists = (
                    await conn.execute(
                        select(capas.c.capa_id).where(capas.c.capa_id == capa.capa_id)
                    )
                ).first()
                if not exists:
                    raise NotFoundError(f"CAPA {capa.capa_id} not found")
                raise ConflictError(
                    f"CAPA {capa.capa_id} was modified concurrently; reload and retry"
                )

    async def list(
        self, *, status: CapaStatus | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[Capa]:
        query = (
            select(capas.c.document)
            .order_by(capas.c.created_at.desc(), capas.c.capa_id)
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            query = query.where(capas.c.status == status.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Capa.model_validate_json(r[0]) for r in rows]

    async def open_for(self, analyte: str, instrument_id: str) -> Capa | None:
        query = (
            select(capas.c.document)
            .where(
                capas.c.analyte == analyte,
                capas.c.instrument_id == instrument_id,
                capas.c.status.in_(_OPEN),
            )
            .order_by(capas.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return Capa.model_validate_json(row[0]) if row else None


class SqlHoldRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, hold: QcHold) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(qc_holds).values(
                    hold_id=hold.hold_id,
                    analyte=hold.analyte,
                    instrument_id=hold.instrument_id,
                    active=hold.active,
                    placed_at=utc(hold.placed_at),
                    released_at=utc(hold.released_at) if hold.released_at else None,
                    document=hold.model_dump_json(),
                )
            )

    async def active(self, analyte: str, instrument_id: str) -> QcHold | None:
        query = select(qc_holds.c.document).where(
            qc_holds.c.analyte == analyte,
            qc_holds.c.instrument_id == instrument_id,
            qc_holds.c.active.is_(True),
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return QcHold.model_validate_json(row[0]) if row else None

    async def list(self, *, active_only: bool = True) -> Sequence[QcHold]:
        query = select(qc_holds.c.document).order_by(qc_holds.c.placed_at.desc())
        if active_only:
            query = query.where(qc_holds.c.active.is_(True))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [QcHold.model_validate_json(r[0]) for r in rows]

    async def update(self, hold: QcHold) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(qc_holds)
                .where(qc_holds.c.hold_id == hold.hold_id)
                .values(
                    active=hold.active,
                    released_at=utc(hold.released_at) if hold.released_at else None,
                    document=hold.model_dump_json(),
                )
            )
            if result.rowcount == 0:
                raise NotFoundError(f"hold {hold.hold_id} not found")

    async def last_released(self, analyte: str, instrument_id: str) -> QcHold | None:
        query = (
            select(qc_holds.c.document)
            .where(
                qc_holds.c.analyte == analyte,
                qc_holds.c.instrument_id == instrument_id,
                qc_holds.c.active.is_(False),
            )
            .order_by(qc_holds.c.released_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return QcHold.model_validate_json(row[0]) if row else None


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
