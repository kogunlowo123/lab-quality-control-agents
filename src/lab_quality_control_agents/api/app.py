"""FastAPI application factory: REST API, MCP at /mcp/, A2A at /a2a."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lab_quality_control_agents import __version__
from lab_quality_control_agents.api import a2a
from lab_quality_control_agents.api.mcp_server import build_mcp
from lab_quality_control_agents.api.routes import router
from lab_quality_control_agents.domain.errors import MessageFormatError, QcError
from lab_quality_control_agents.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]
DEFAULT_MCP_HOSTS = ("127.0.0.1:*", "localhost:*")

log = get_logger("lab_quality_control_agents.api")


def create_app(
    factory: ContainerFactory, *, mcp_allowed_hosts: Sequence[str] = DEFAULT_MCP_HOSTS
) -> FastAPI:
    app: FastAPI

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        app.state.container = container
        log.info("startup", version=__version__)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Lab Quality Control Agents",
        version=__version__,
        description=(
            "Westgard multirule QC, Levey-Jennings and sigma statistics, HL7 v2 and ASTM E1394 "
            "ingestion, root cause investigation, supervisor-approved CAPA and verification."
        ),
        lifespan=lifespan,
    )
    mcp, mcp_http = build_mcp(app, allowed_hosts=mcp_allowed_hosts)
    app.include_router(router)
    app.include_router(a2a.router)
    app.mount("/mcp", mcp_http)

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(
            route, "path", "/mcp" if request.url.path.startswith("/mcp") else "unmatched"
        )
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(QcError)
    async def qc_error(_: Request, exc: QcError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        content: dict[str, object] = {"error": exc.code, "error_description": exc.message}
        if isinstance(exc, MessageFormatError) and exc.ack:
            content["ack"] = exc.ack
        return JSONResponse(status_code=exc.status, content=content, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )

    return app
