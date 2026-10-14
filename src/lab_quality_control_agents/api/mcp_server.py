"""MCP server (official SDK, streamable HTTP, stateless JSON responses).

Tools expose the QC domain to agent runtimes. Each tool authenticates the
caller from the HTTP ``Authorization`` header of the MCP request, is
authorized by the same policy as the REST API (inside the service call), and
is recorded in the audit chain. There are deliberately no tools for
approving, rejecting or closing CAPA records: those are human signatures.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, TypeVar

from agentic_core.observability import correlation_id
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from lab_quality_control_agents import __version__
from lab_quality_control_agents.api.schemas import HoldList
from lab_quality_control_agents.application.commands import (
    IngestReport,
    RunResultInput,
    RunSubmission,
)
from lab_quality_control_agents.domain.errors import QcError
from lab_quality_control_agents.domain.models import Capa, Investigation
from lab_quality_control_agents.domain.statistics import LeveyJenningsChart, SigmaAssessment
from lab_quality_control_agents.runtime.container import Container
from lab_quality_control_agents.security.authn import Principal

T = TypeVar("T", bound=BaseModel)

Code = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")]
Ident = Annotated[str, Field(min_length=1, max_length=64)]


def build_mcp(app: FastAPI, *, allowed_hosts: Sequence[str]) -> tuple[MCPServer[object], Starlette]:
    mcp: MCPServer[object] = MCPServer(
        "lab-quality-control",
        version=__version__,
        instructions=(
            "Laboratory QC tools. Results are evaluated with Westgard rules; rejected runs place "
            "a patient-result hold. Agents may investigate and propose; only humans sign."
        ),
    )

    async def call(
        ctx: Context, operation: str, fn: Callable[[Container, Principal], Awaitable[T]]
    ) -> T:
        container: Container = app.state.container
        headers = ctx.headers or {}
        outcome = "error"
        try:
            principal = await container.authenticator.authenticate(headers.get("authorization"))
            result = await fn(container, principal)
            outcome = "ok"
            await container.audit.record(
                actor=principal.subject,
                action=f"mcp.tool.{operation}",
                resource="mcp",
                outcome="success",
                correlation_id=correlation_id(),
                details={"kind": principal.kind.value},
            )
            return result
        except QcError as exc:
            outcome = exc.code
            raise
        finally:
            container.metrics.tool_calls.labels("mcp", operation, outcome).inc()

    @mcp.tool(
        name="evaluate_qc_run",
        description=(
            "Submit one QC run (one result per control material) for Westgard multirule "
            "evaluation. Returns the evaluation, any hold placed and any investigation started."
        ),
    )
    async def evaluate_qc_run(
        analyte: Code,
        instrument_id: Code,
        run_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")],
        results: Annotated[list[RunResultInput], Field(min_length=1, max_length=12)],
        ctx: Context,
    ) -> IngestReport:
        submission = RunSubmission(
            analyte=analyte, instrument_id=instrument_id, run_id=run_id, results=tuple(results)
        )
        return await call(ctx, "evaluate_qc_run", lambda c, p: c.service.submit_run(p, submission))

    @mcp.tool(
        name="levey_jennings",
        description=(
            "Levey-Jennings chart data: limits, points with rule flags, observed statistics."
        ),
    )
    async def levey_jennings(
        analyte: Code,
        instrument_id: Code,
        material_id: Code,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> LeveyJenningsChart:
        return await call(
            ctx,
            "levey_jennings",
            lambda c, p: c.service.levey_jennings(
                p, analyte, instrument_id, material_id, limit=limit
            ),
        )

    @mcp.tool(
        name="sigma_metric",
        description=(
            "Sigma metric from TEa, bias and CV with the recommended QC rules "
            "(Westgard Sigma Rules)."
        ),
    )
    async def sigma_metric(
        analyte: Code, instrument_id: Code, material_id: Code, ctx: Context
    ) -> SigmaAssessment:
        return await call(
            ctx,
            "sigma_metric",
            lambda c, p: c.service.sigma(p, analyte, instrument_id, material_id),
        )

    @mcp.tool(
        name="get_investigation",
        description="A root cause investigation with ranked hypotheses and evidence.",
    )
    async def get_investigation(investigation_id: Ident, ctx: Context) -> Investigation:
        return await call(
            ctx, "get_investigation", lambda c, p: c.service.get_investigation(p, investigation_id)
        )

    @mcp.tool(name="get_capa", description="A CAPA record with its status and signatures.")
    async def get_capa(capa_id: Ident, ctx: Context) -> Capa:
        return await call(ctx, "get_capa", lambda c, p: c.service.get_capa(p, capa_id))

    @mcp.tool(
        name="list_holds",
        description="Active patient-result holds (analyte and instrument pairs).",
    )
    async def list_holds(ctx: Context) -> HoldList:
        async def run(c: Container, p: Principal) -> HoldList:
            return HoldList(items=list(await c.service.list_holds(p, active_only=True)))

        return await call(ctx, "list_holds", run)

    http_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=list(allowed_hosts)),
    )
    return mcp, http_app
