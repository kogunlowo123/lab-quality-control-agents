"""Commands and reports shared by the REST API, MCP tools and the A2A endpoint."""

from __future__ import annotations

import math

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from lab_quality_control_agents.domain.models import (
    FULL_MULTIRULE,
    Code,
    FreeText,
    InstrumentEventKind,
    ResultSource,
    RunEvaluation,
    TargetSource,
    Units,
    WestgardRule,
)


class RunResultInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    material_id: Code
    control_lot: Code | None = None
    value: float
    units: Units
    observed_at: AwareDatetime
    reagent_lot: Code | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class RunSubmission(BaseModel):
    """One analytical run of control measurements, one result per control material."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyte: Code
    instrument_id: Code
    run_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")
    results: tuple[RunResultInput, ...] = Field(min_length=1, max_length=12)


class Hl7Submission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=8, max_length=1_000_000, description="HL7 v2 ORU^R01")


class AstmSubmission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: str = Field(min_length=4, max_length=1_000_000)
    framed: bool = Field(
        default=True, description="True for E1381 frames (STX..ETX), false for bare records"
    )


class TargetInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analyte: Code
    instrument_id: Code
    material_id: Code
    control_lot: Code
    level: int = Field(ge=1, le=6)
    mean: float
    sd: float = Field(gt=0)
    units: Units
    source: TargetSource = TargetSource.MANUFACTURER
    rules: tuple[WestgardRule, ...] = Field(default=FULL_MULTIRULE, min_length=1)

    @field_validator("mean", "sd")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be finite")
        return value


class EstablishTarget(BaseModel):
    """Establish a laboratory target from baseline control data (CLSI C24)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyte: Code
    instrument_id: Code
    material_id: Code
    control_lot: Code
    level: int = Field(ge=1, le=6)
    units: Units
    values: tuple[float, ...] = Field(min_length=2, max_length=1000)
    rules: tuple[WestgardRule, ...] = Field(default=FULL_MULTIRULE, min_length=1)


class InstrumentEventInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: Code
    kind: InstrumentEventKind
    occurred_at: AwareDatetime
    analyte: Code | None = None
    lot: Code | None = None
    detail: FreeText = ""


class ProposeCapa(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_id: str = Field(min_length=1, max_length=64)
    hypothesis_id: str | None = Field(default=None, pattern=r"^H\d{1,2}$")
    corrective_actions: tuple[FreeText, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )


class SignatureInput(BaseModel):
    """Statement accompanying an electronic signature (21 CFR 11.50)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=10, max_length=2000)


class ActionCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    note: str = Field(min_length=10, max_length=2000, description="What was done, by whom, when")


class UnmatchedObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    analyte_code: str
    instrument_id: str
    material_id: str
    reason: str


class IngestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: ResultSource
    message_id: str
    stored: int
    duplicates: int
    unmatched: tuple[UnmatchedObservation, ...]
    skipped: tuple[str, ...]
    evaluations: tuple[RunEvaluation, ...]
    holds_placed: tuple[str, ...]
    investigations: tuple[str, ...]
    capas_proposed: tuple[str, ...]
    capas_verified: tuple[str, ...]
    workflow_errors: tuple[str, ...]
    ack: str | None = Field(default=None, description="HL7 acknowledgement for HL7 submissions")
