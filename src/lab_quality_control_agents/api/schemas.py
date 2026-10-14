"""HTTP response envelopes and request bodies not shared with MCP/A2A."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lab_quality_control_agents.domain.models import (
    Capa,
    Investigation,
    QcHold,
    QcTarget,
    RunEvaluation,
)


class EvaluationList(BaseModel):
    items: list[RunEvaluation]
    limit: int
    offset: int


class TargetList(BaseModel):
    items: list[QcTarget]


class InvestigationList(BaseModel):
    items: list[Investigation]
    limit: int
    offset: int


class CapaList(BaseModel):
    items: list[Capa]
    limit: int
    offset: int


class HoldList(BaseModel):
    items: list[QcHold]


class InvestigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(min_length=1, max_length=64)


class OAuthError(BaseModel):
    error: str
    error_description: str
