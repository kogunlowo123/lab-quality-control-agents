"""Agent-to-Agent (A2A) protocol: agent card and JSON-RPC ``message/send``.

Other agents (an LIS orchestration agent, a lab operations agent) delegate QC
work here. Requests carry a bearer credential (identity fabric or enterprise
OIDC) checked at the HTTP layer; each skill is then authorized by the same
policy as the REST API. Tasks complete synchronously and are returned in the
response; they are not retained, so ``tasks/get`` reports TaskNotFound.

Skill routing: a ``data`` part with a ``skill`` field selects the skill
explicitly. A ``text`` part starting with ``MSH`` is treated as an HL7 v2
message; one starting with ``H|`` or containing STX framing as ASTM.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agentic_core.domain.identifiers import new_id
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from lab_quality_control_agents import __version__
from lab_quality_control_agents.api.schemas import HoldList
from lab_quality_control_agents.application.commands import (
    AstmSubmission,
    Hl7Submission,
    RunSubmission,
)
from lab_quality_control_agents.domain.errors import (
    ApprovalRequiredError,
    ForbiddenError,
    QcError,
)
from lab_quality_control_agents.runtime.container import Container
from lab_quality_control_agents.security.authn import Principal

PROTOCOL_VERSION = "0.3.0"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND = -32001

SKILLS: tuple[dict[str, Any], ...] = (
    {
        "id": "qc.evaluate_run",
        "name": "Evaluate a QC run",
        "description": (
            "Score control results against their targets, apply the Westgard multirule "
            "procedure, place a result hold and start an investigation when the run is rejected."
        ),
        "tags": ["westgard", "quality-control", "laboratory"],
        "inputModes": ["application/json"],
        "outputModes": ["application/json"],
    },
    {
        "id": "qc.ingest_hl7",
        "name": "Ingest HL7 v2 QC results",
        "description": "Parse an ORU^R01 control-specimen message and evaluate each run.",
        "tags": ["hl7", "lis"],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    },
    {
        "id": "qc.ingest_astm",
        "name": "Ingest ASTM E1394 QC results",
        "description": "Parse framed (E1381) or bare E1394 QC records and evaluate each run.",
        "tags": ["astm", "instrument"],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    },
    {
        "id": "qc.investigation",
        "name": "Get a QC investigation",
        "description": "Ranked root-cause hypotheses with evidence for a rejected run.",
        "tags": ["root-cause", "capa"],
        "inputModes": ["application/json"],
        "outputModes": ["application/json"],
    },
    {
        "id": "qc.holds",
        "name": "List result holds",
        "description": "Analyte/instrument pairs whose patient results must not be released.",
        "tags": ["release", "hold"],
        "examples": ['{"skill": "qc.holds"}'],
        "inputModes": ["application/json"],
        "outputModes": ["application/json"],
    },
)


def agent_card(public_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Lab Quality Control Agents",
        "description": (
            "Westgard multirule QC evaluation, Levey-Jennings and sigma statistics, root cause "
            "investigation and supervisor-approved CAPA for clinical laboratories. Never "
            "releases patient results; rejected runs place a hold that only a supervisor lifts."
        ),
        "url": f"{public_url}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": list(SKILLS),
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "agent-identity-fabric credential or enterprise OIDC token",
            }
        },
        "security": [{"bearer": []}],
    }


def _error(request_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


def _task(
    request_id: Any,
    message: dict[str, Any],
    state: str,
    *,
    skill: str,
    data: Any = None,
    note: str | None = None,
) -> JSONResponse:
    task: dict[str, Any] = {
        "kind": "task",
        "id": new_id(),
        "contextId": str(message.get("contextId") or new_id()),
        "status": {"state": state, "timestamp": datetime.now().astimezone().isoformat()},
        "history": [message],
    }
    if note is not None:
        task["status"]["message"] = {
            "kind": "message",
            "role": "agent",
            "messageId": new_id(),
            "parts": [{"kind": "text", "text": note}],
        }
    if data is not None:
        task["artifacts"] = [
            {"artifactId": new_id(), "name": skill, "parts": [{"kind": "data", "data": data}]}
        ]
    return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": task})


def _route(parts: list[Any]) -> tuple[str, dict[str, Any]]:
    for part in parts:
        if (
            isinstance(part, dict)
            and part.get("kind") == "data"
            and isinstance(part.get("data"), dict)
        ):
            data = dict(part["data"])
            skill = data.pop("skill", None)
            if isinstance(skill, str):
                return skill, data
    for part in parts:
        if (
            isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("text"), str)
        ):
            text = part["text"]
            stripped = text.lstrip("\x0b\r\n ")
            if stripped.startswith("MSH"):
                return "qc.ingest_hl7", {"message": text}
            if stripped.startswith("H|"):
                return "qc.ingest_astm", {"data": text, "framed": False}
            if "\x02" in text:
                return "qc.ingest_astm", {"data": text, "framed": True}
    raise ValueError("no data part with a skill and no HL7/ASTM text part")


async def _dispatch(c: Container, who: Principal, skill: str, args: dict[str, Any]) -> BaseModel:
    service = c.service
    if skill == "qc.evaluate_run":
        return await service.submit_run(who, RunSubmission.model_validate(args))
    if skill == "qc.ingest_hl7":
        return await service.ingest_hl7(who, Hl7Submission.model_validate(args))
    if skill == "qc.ingest_astm":
        return await service.ingest_astm(who, AstmSubmission.model_validate(args))
    if skill == "qc.investigation":
        investigation_id = args.get("investigation_id")
        if not isinstance(investigation_id, str) or not investigation_id:
            raise ValueError("investigation_id is required")
        return await service.get_investigation(who, investigation_id)
    if skill == "qc.holds":
        return HoldList(items=list(await service.list_holds(who, active_only=True)))
    raise LookupError(skill)


router = APIRouter()


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def card(request: Request) -> dict[str, Any]:
    c: Container = request.app.state.container
    return agent_card(c.settings.public_url_str)


def _envelope(raw: bytes) -> tuple[Any, str, dict[str, Any]] | JSONResponse:
    """Validate the JSON-RPC 2.0 envelope; return (id, method, payload) or an error."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return _error(None, PARSE_ERROR, "parse error")
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "invalid JSON-RPC 2.0 request")
    request_id = payload.get("id")
    if not isinstance(request_id, str | int) and request_id is not None:
        return _error(None, INVALID_REQUEST, "id must be a string, number or null")
    method = payload.get("method")
    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "method is required")
    return request_id, method, payload


def _failure(
    c: Container, request_id: Any, message: dict[str, Any], skill: str, exc: Exception
) -> JSONResponse:
    """Map a skill failure to a JSON-RPC error or a rejected/failed task."""
    if isinstance(exc, LookupError):
        return _error(request_id, INVALID_PARAMS, f"unknown skill {skill!r}")
    if isinstance(exc, ValidationError):
        return _error(request_id, INVALID_PARAMS, f"invalid skill input: {exc.errors()[0]['msg']}")
    if isinstance(exc, ForbiddenError | ApprovalRequiredError):
        c.metrics.tool_calls.labels("a2a", skill, "rejected").inc()
        return _task(request_id, message, "rejected", skill=skill, note=exc.message)
    if isinstance(exc, QcError):
        c.metrics.tool_calls.labels("a2a", skill, "failed").inc()
        return _task(request_id, message, "failed", skill=skill, note=f"{exc.code}: {exc.message}")
    return _error(request_id, INVALID_PARAMS, f"invalid skill input: {exc}")


async def _send(
    c: Container, who: Principal, request_id: Any, payload: dict[str, Any]
) -> JSONResponse:
    params = payload.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return _error(request_id, INVALID_PARAMS, "params.message.parts is required")
    try:
        skill, args = _route(message["parts"])
    except ValueError as exc:
        return _error(request_id, INVALID_PARAMS, str(exc))
    try:
        result = await _dispatch(c, who, skill, args)
    except (LookupError, ValueError, QcError) as exc:
        return _failure(c, request_id, message, skill, exc)
    c.metrics.tool_calls.labels("a2a", skill, "completed").inc()
    return _task(request_id, message, "completed", skill=skill, data=result.model_dump(mode="json"))


@router.post("/a2a", tags=["a2a"])
async def jsonrpc(request: Request) -> JSONResponse:
    c: Container = request.app.state.container
    envelope = _envelope(await request.body())
    if isinstance(envelope, JSONResponse):
        return envelope
    request_id, method, payload = envelope
    try:
        who = await c.authenticator.authenticate(request.headers.get("authorization"))
    except QcError as exc:
        c.metrics.tool_calls.labels("a2a", "authenticate", "unauthenticated").inc()
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "error_description": exc.message},
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        )
    if method == "tasks/get":
        return _error(
            request_id, TASK_NOT_FOUND, "tasks complete synchronously and are not retained"
        )
    if method != "message/send":
        return _error(request_id, METHOD_NOT_FOUND, f"method {method!r} not supported")
    return await _send(c, who, request_id, payload)
