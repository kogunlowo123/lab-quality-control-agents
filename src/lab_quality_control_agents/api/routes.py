"""REST routes. Every /v1 route requires a bearer token and is authorized by policy."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from lab_quality_control_agents.api.schemas import (
    CapaList,
    EvaluationList,
    HoldList,
    InvestigateRequest,
    InvestigationList,
    TargetList,
)
from lab_quality_control_agents.application.commands import (
    ActionCompletion,
    AstmSubmission,
    EstablishTarget,
    Hl7Submission,
    IngestReport,
    InstrumentEventInput,
    ProposeCapa,
    RunSubmission,
    SignatureInput,
    TargetInput,
)
from lab_quality_control_agents.domain.models import (
    Capa,
    CapaStatus,
    InstrumentEvent,
    Investigation,
    QcTarget,
    RunEvaluation,
    RunOutcome,
)
from lab_quality_control_agents.domain.statistics import LeveyJenningsChart, SigmaAssessment
from lab_quality_control_agents.runtime.container import Container
from lab_quality_control_agents.security.authn import Principal

router = APIRouter()

Ident = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-]+$")]
CodeQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")
]
OptionalCode = Annotated[
    str | None, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")
]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0, le=100_000)]


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    return await container(request).authenticator.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
Caller = Annotated[Principal, Depends(principal)]


# ------------------------------------------------------------------ ingestion


@router.post("/v1/qc/hl7", tags=["ingestion"])
async def ingest_hl7(body: Hl7Submission, c: Deps, who: Caller) -> IngestReport:
    """HL7 v2 ORU^R01 QC results from the LIS or instrument middleware."""
    return await c.service.ingest_hl7(who, body)


@router.post("/v1/qc/astm", tags=["ingestion"])
async def ingest_astm(body: AstmSubmission, c: Deps, who: Caller) -> IngestReport:
    """ASTM E1394 records, framed (E1381) or bare."""
    return await c.service.ingest_astm(who, body)


@router.post("/v1/qc/runs", tags=["ingestion"])
async def submit_run(body: RunSubmission, c: Deps, who: Caller) -> IngestReport:
    """A QC run as JSON (one result per control material)."""
    return await c.service.submit_run(who, body)


@router.get("/v1/qc/runs", tags=["qc"])
async def list_runs(
    *,
    c: Deps,
    who: Caller,
    outcome: RunOutcome | None = None,
    analyte: OptionalCode = None,
    instrument_id: OptionalCode = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> EvaluationList:
    items = await c.service.list_evaluations(
        who,
        outcome=outcome,
        analyte=analyte,
        instrument_id=instrument_id,
        limit=limit,
        offset=offset,
    )
    return EvaluationList(items=list(items), limit=limit, offset=offset)


@router.get("/v1/qc/runs/{evaluation_id}", tags=["qc"])
async def get_run(evaluation_id: Ident, c: Deps, who: Caller) -> RunEvaluation:
    return await c.service.get_evaluation(who, evaluation_id)


# -------------------------------------------------------------------- targets


@router.get("/v1/qc/targets", tags=["targets"])
async def list_targets(
    c: Deps, who: Caller, analyte: OptionalCode = None, instrument_id: OptionalCode = None
) -> TargetList:
    items = await c.service.list_targets(who, analyte=analyte, instrument_id=instrument_id)
    return TargetList(items=list(items))


@router.put("/v1/qc/targets", tags=["targets"])
async def put_target(body: TargetInput, c: Deps, who: Caller) -> QcTarget:
    """Set a target (supervisor signature action)."""
    return await c.service.put_target(who, body)


@router.post("/v1/qc/targets/establish", status_code=201, tags=["targets"])
async def establish_target(body: EstablishTarget, c: Deps, who: Caller) -> QcTarget:
    """Establish a laboratory mean and SD from baseline data (CLSI C24)."""
    return await c.service.establish_target(who, body)


@router.get("/v1/qc/levey-jennings", tags=["statistics"])
async def levey_jennings(
    *,
    c: Deps,
    who: Caller,
    analyte: CodeQuery,
    instrument_id: CodeQuery,
    material_id: CodeQuery,
    limit: Limit = 100,
) -> LeveyJenningsChart:
    return await c.service.levey_jennings(who, analyte, instrument_id, material_id, limit=limit)


@router.get("/v1/qc/sigma", tags=["statistics"])
async def sigma(
    c: Deps, who: Caller, analyte: CodeQuery, instrument_id: CodeQuery, material_id: CodeQuery
) -> SigmaAssessment:
    return await c.service.sigma(who, analyte, instrument_id, material_id)


# --------------------------------------------------------- events and holds


@router.post("/v1/instrument-events", status_code=201, tags=["instruments"])
async def record_event(body: InstrumentEventInput, c: Deps, who: Caller) -> InstrumentEvent:
    """Calibration, maintenance, lot changes and faults used by investigations."""
    return await c.service.record_event(who, body)


@router.get("/v1/holds", tags=["qc"])
async def list_holds(c: Deps, who: Caller, active_only: bool = True) -> HoldList:
    """Analyte/instrument pairs whose patient results must not be released."""
    return HoldList(items=list(await c.service.list_holds(who, active_only=active_only)))


# ------------------------------------------------------------ investigations


@router.post("/v1/investigations", status_code=201, tags=["investigations"])
async def investigate(body: InvestigateRequest, c: Deps, who: Caller) -> Investigation:
    return await c.service.investigate(who, body.evaluation_id)


@router.get("/v1/investigations", tags=["investigations"])
async def list_investigations(
    c: Deps, who: Caller, limit: Limit = 100, offset: Offset = 0
) -> InvestigationList:
    items = await c.service.list_investigations(who, limit=limit, offset=offset)
    return InvestigationList(items=list(items), limit=limit, offset=offset)


@router.get("/v1/investigations/{investigation_id}", tags=["investigations"])
async def get_investigation(investigation_id: Ident, c: Deps, who: Caller) -> Investigation:
    return await c.service.get_investigation(who, investigation_id)


# ----------------------------------------------------------------------- CAPA


@router.post("/v1/capas", status_code=201, tags=["capa"])
async def propose_capa(body: ProposeCapa, c: Deps, who: Caller) -> Capa:
    return await c.service.propose_capa(who, body)


@router.get("/v1/capas", tags=["capa"])
async def list_capas(
    c: Deps,
    who: Caller,
    status: CapaStatus | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> CapaList:
    items = await c.service.list_capas(who, status=status, limit=limit, offset=offset)
    return CapaList(items=list(items), limit=limit, offset=offset)


@router.get("/v1/capas/{capa_id}", tags=["capa"])
async def get_capa(capa_id: Ident, c: Deps, who: Caller) -> Capa:
    return await c.service.get_capa(who, capa_id)


@router.post("/v1/capas/{capa_id}/approve", tags=["capa"])
async def approve_capa(capa_id: Ident, body: SignatureInput, c: Deps, who: Caller) -> Capa:
    """Supervisor electronic signature: approve the proposed corrective action."""
    return await c.service.approve_capa(who, capa_id, body)


@router.post("/v1/capas/{capa_id}/reject", tags=["capa"])
async def reject_capa(capa_id: Ident, body: SignatureInput, c: Deps, who: Caller) -> Capa:
    return await c.service.reject_capa(who, capa_id, body)


@router.post("/v1/capas/{capa_id}/complete-action", tags=["capa"])
async def complete_action(capa_id: Ident, body: ActionCompletion, c: Deps, who: Caller) -> Capa:
    """Staff signature that the approved corrective action has been performed."""
    return await c.service.complete_action(who, capa_id, body)


@router.post("/v1/capas/{capa_id}/verify", tags=["capa"])
async def verify_capa(capa_id: Ident, c: Deps, who: Caller) -> Capa:
    """Apply the verification run rules to QC runs performed after the action."""
    return await c.service.verify_capa(who, capa_id)


@router.post("/v1/capas/{capa_id}/close", tags=["capa"])
async def close_capa(capa_id: Ident, body: SignatureInput, c: Deps, who: Caller) -> Capa:
    """Supervisor signature closing a verified CAPA; lifts the result hold."""
    return await c.service.close_capa(who, capa_id, body)


# ----------------------------------------------------------------- operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {"status": "ready"}


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
