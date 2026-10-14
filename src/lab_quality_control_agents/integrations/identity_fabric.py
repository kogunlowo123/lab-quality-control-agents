"""Typed client for agent-identity-fabric.

Wire formats match the fabric's published contract:

* ``POST /oauth2/token`` RFC 7523 jwt-bearer grant (form encoded)
* ``POST /oauth2/introspect`` RFC 7662 (form encoded)
* ``POST /v1/authorize`` policy decision for an action (JSON, agent bearer token)

Transport errors, 5xx and malformed bodies raise ``FabricUnavailableError``;
4xx responses raise ``FabricRejectedError``. Either way callers fail closed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, ValidationError

from lab_quality_control_agents.domain.errors import QcError

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class FabricUnavailableError(QcError):
    code = "identity_fabric_unavailable"
    status = 503


class FabricRejectedError(QcError):
    """The fabric refused the credential or request (4xx): treat as unauthenticated."""

    code = "unauthorized"
    status = 401


class FabricToken(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str
    token_type: str
    expires_in: int
    scope: str = ""


class FabricIntrospection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    iss: str | None = None
    act: dict[str, Any] | None = None


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...] = ()
    policy_version: str = ""
    policy_digest: str = ""
    reason: str = ""
    delegation_chain: tuple[str, ...] = ()


class IdentityFabricClient:
    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    @property
    def base_url(self) -> str:
        return self._base

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.post(f"{self._base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise FabricUnavailableError(
                f"identity fabric unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 500:
            raise FabricUnavailableError(f"identity fabric returned {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise FabricUnavailableError("identity fabric returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise FabricUnavailableError("identity fabric returned an unexpected body")
        if response.status_code >= 400:
            error = str(body.get("error", "error"))
            raise FabricRejectedError(f"identity fabric rejected the request: {error}")
        return body

    async def token(
        self, *, client_id: str, assertion: str, scope: str | None = None
    ) -> FabricToken:
        form = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scope:
            form["scope"] = scope
        body = await self._post("/oauth2/token", data=form)
        try:
            return FabricToken.model_validate(body)
        except ValidationError as exc:
            raise FabricUnavailableError("malformed token response") from exc

    async def introspect(self, token: str) -> FabricIntrospection:
        body = await self._post("/oauth2/introspect", data={"token": token})
        try:
            return FabricIntrospection.model_validate(body)
        except ValidationError as exc:
            raise FabricUnavailableError("malformed introspection response") from exc

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        classification: DataClassification,
        attributes: dict[str, Any] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> FabricDecision:
        body = await self._post(
            "/v1/authorize",
            json={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_classification": int(classification),
                "resource_attributes": attributes or {},
                "environment": environment or {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            return FabricDecision.model_validate(body)
        except ValidationError as exc:
            raise FabricUnavailableError("malformed authorization response") from exc


class AgentCredentials:
    """This service's own agent credential, exchanged from its workload token.

    The projected service-account token is re-read on every exchange because
    the kubelet rotates it. Credentials are cached until one minute before
    expiry, measured with the injected clock.
    """

    def __init__(
        self,
        client: IdentityFabricClient,
        *,
        agent_id: str,
        workload_token_file: Path,
        clock: Callable[[], datetime],
        refresh_margin: timedelta = timedelta(seconds=60),
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._file = workload_token_file
        self._clock = clock
        self._margin = refresh_margin
        self._credential: FabricToken | None = None
        self._expires: datetime | None = None

    async def credential(self) -> FabricToken:
        now = self._clock()
        if self._credential and self._expires and now < self._expires - self._margin:
            return self._credential
        try:
            assertion = self._file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FabricUnavailableError("workload token file is unreadable") from exc
        issued = await self._client.token(client_id=self._agent_id, assertion=assertion)
        self._credential = issued
        self._expires = now + timedelta(seconds=issued.expires_in)
        return issued
