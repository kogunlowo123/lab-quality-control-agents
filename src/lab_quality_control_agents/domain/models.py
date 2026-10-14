"""Domain model: control targets, QC results, run evaluations, instrument events,
investigations, CAPA records and analytical holds.

All timestamps are timezone-aware. Identifiers for analytes, instruments and
control materials are restricted to a conservative character set because they
arrive from instrument interfaces and end up in URLs, metrics labels and logs.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")]
Units = Annotated[str, StringConstraints(min_length=1, max_length=32)]
FreeText = Annotated[str, StringConstraints(max_length=2000)]


class WestgardRule(StrEnum):
    """Control rules in Westgard notation (N_L: N observations beyond L)."""

    R_1_2S = "1_2s"  # warning: one result beyond 2 SD
    R_1_3S = "1_3s"  # reject (random): one result beyond 3 SD
    R_2_2S = "2_2s"  # reject (systematic): two consecutive beyond 2 SD, same side
    R_R_4S = "R_4s"  # reject (random): one beyond +2 SD and another beyond -2 SD in a run
    R_4_1S = "4_1s"  # reject (systematic): four consecutive beyond 1 SD, same side
    R_10_X = "10_x"  # reject (systematic): ten consecutive on the same side of the mean


FULL_MULTIRULE: tuple[WestgardRule, ...] = tuple(WestgardRule)


class TargetSource(StrEnum):
    MANUFACTURER = "manufacturer"  # package-insert range, provisional
    LABORATORY = "laboratory"  # established from the laboratory's own data (CLSI C24)


class QcTarget(BaseModel):
    """Assigned mean and SD for one control material lot on one analyte and instrument."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyte: Code
    instrument_id: Code
    material_id: Code = Field(description="Control material identifier, one per level")
    control_lot: Code
    level: int = Field(ge=1, le=6)
    mean: float
    sd: float = Field(gt=0)
    units: Units
    source: TargetSource = TargetSource.LABORATORY
    established_n: int | None = Field(default=None, ge=2)
    rules: tuple[WestgardRule, ...] = Field(default=FULL_MULTIRULE, min_length=1)
    effective_from: AwareDatetime

    @field_validator("mean", "sd")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        return value

    @field_validator("rules")
    @classmethod
    def _unique_rules(cls, rules: tuple[WestgardRule, ...]) -> tuple[WestgardRule, ...]:
        if len(set(rules)) != len(rules):
            raise ValueError("rules must be unique")
        return rules

    def z(self, value: float) -> float:
        return (value - self.mean) / self.sd


class ResultSource(StrEnum):
    HL7 = "hl7"
    ASTM = "astm"
    API = "api"


class QcResult(BaseModel):
    """One control measurement, scored against the target that was effective."""

    model_config = ConfigDict(frozen=True)

    result_id: str
    source_key: str = Field(description="Idempotency key derived from the source message")
    source: ResultSource
    analyte: Code
    instrument_id: Code
    material_id: Code
    control_lot: Code
    level: int
    run_id: str = Field(min_length=1, max_length=128)
    value: float
    units: Units
    z: float
    observed_at: AwareDatetime
    received_at: AwareDatetime
    reagent_lot: str | None = None


class Severity(StrEnum):
    WARNING = "warning"
    REJECT = "reject"


class ErrorType(StrEnum):
    RANDOM = "random"
    SYSTEMATIC = "systematic"


class RuleScope(StrEnum):
    SINGLE = "single"  # one observation
    WITHIN_RUN = "within_run"  # across control levels in the current run
    ACROSS_RUNS = "across_runs"  # one control level over consecutive runs
    ACROSS_LEVELS_AND_RUNS = "across_levels_and_runs"


class RuleViolation(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: WestgardRule
    severity: Severity
    error_type: ErrorType
    scope: RuleScope
    direction: Literal["high", "low", "both"]
    levels: tuple[int, ...]
    result_ids: tuple[str, ...]
    detail: str


class RunOutcome(StrEnum):
    ACCEPT = "accept"
    WARNING = "warning"
    REJECT = "reject"


class RunEvaluation(BaseModel):
    """Outcome of applying the multirule procedure to one analytical run."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str
    analyte: Code
    instrument_id: Code
    run_id: str
    outcome: RunOutcome
    violations: tuple[RuleViolation, ...]
    rules_applied: tuple[WestgardRule, ...]
    result_ids: tuple[str, ...]
    levels: tuple[int, ...]
    run_observed_at: AwareDatetime
    evaluated_at: AwareDatetime
    history_points: int = Field(ge=0, description="Prior observations considered")

    @property
    def error_types(self) -> frozenset[ErrorType]:
        return frozenset(v.error_type for v in self.violations if v.severity is Severity.REJECT)


class InstrumentEventKind(StrEnum):
    CALIBRATION = "calibration"
    MAINTENANCE = "maintenance"
    REAGENT_LOT_CHANGE = "reagent_lot_change"
    CONTROL_LOT_CHANGE = "control_lot_change"
    CONTROL_VIAL_OPENED = "control_vial_opened"
    HARDWARE_FAULT = "hardware_fault"
    SOFTWARE_UPDATE = "software_update"


class InstrumentEvent(BaseModel):
    """A change on an instrument that can explain a QC failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    instrument_id: Code
    kind: InstrumentEventKind
    occurred_at: AwareDatetime
    analyte: Code | None = Field(default=None, description="None means instrument-wide")
    lot: Code | None = None
    detail: FreeText = ""
    recorded_by: str
    recorded_at: AwareDatetime


class CauseCategory(StrEnum):
    REAGENT_LOT = "reagent_lot"
    CALIBRATION = "calibration"
    CONTROL_MATERIAL = "control_material"
    INSTRUMENT = "instrument"
    RANDOM_ANALYTICAL = "random_analytical"
    DRIFT = "drift"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["rule", "event", "lot_change", "cross_analyte", "timing", "pattern"]
    description: str
    reference: str | None = None
    weight: float


class Hypothesis(BaseModel):
    model_config = ConfigDict(frozen=True)

    hypothesis_id: str
    category: CauseCategory
    probability: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0)
    evidence: tuple[Evidence, ...]
    corrective_actions: tuple[str, ...]
    preventive_actions: tuple[str, ...]


class Investigation(BaseModel):
    """Root cause analysis for one rejected run."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    evaluation_id: str
    analyte: Code
    instrument_id: Code
    run_id: str
    error_types: tuple[ErrorType, ...]
    hypotheses: tuple[Hypothesis, ...]
    other_analytes_rejected: tuple[str, ...]
    impact_window_start: AwareDatetime | None = Field(
        description="Last accepted run before the failure; patient results since then need review"
    )
    impact_window_end: AwareDatetime
    narrative: str | None = None
    narrative_provider: str | None = None
    created_at: AwareDatetime
    created_by: str

    @property
    def leading(self) -> Hypothesis:
        return self.hypotheses[0]


class CapaStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTION_COMPLETED = "action_completed"
    VERIFIED = "verified"
    CLOSED = "closed"


class SignatureMeaning(StrEnum):
    """21 CFR 11.50(a)(3): the meaning associated with a signature."""

    APPROVAL = "approval"
    REJECTION = "rejection"
    PERFORMED = "performed"
    CLOSURE = "closure"


class ElectronicSignature(BaseModel):
    model_config = ConfigDict(frozen=True)

    signer: str = Field(description="Authenticated subject")
    signer_name: str = Field(description="Printed name from the identity provider")
    meaning: SignatureMeaning
    signed_at: AwareDatetime
    record_digest: str = Field(description="sha256 of the CAPA content that was signed")
    statement: FreeText


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    sufficient_data: bool = Field(description="False when too few complete runs exist yet")
    run_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    evaluated_at: AwareDatetime
    mean_z_by_level: dict[int, float]


class Capa(BaseModel):
    """Corrective and preventive action record."""

    model_config = ConfigDict(frozen=True)

    capa_id: str
    investigation_id: str
    evaluation_id: str
    analyte: Code
    instrument_id: Code
    status: CapaStatus
    root_cause: CauseCategory
    hypothesis_id: str
    error_types: tuple[ErrorType, ...]
    corrective_actions: tuple[str, ...] = Field(min_length=1)
    preventive_actions: tuple[str, ...]
    proposed_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    signatures: tuple[ElectronicSignature, ...] = ()
    action_completed_at: AwareDatetime | None = None
    action_note: str | None = None
    verifications: tuple[VerificationResult, ...] = ()
    version: int = Field(default=1, ge=1)

    def signed(self, meaning: SignatureMeaning) -> ElectronicSignature | None:
        for signature in reversed(self.signatures):
            if signature.meaning is meaning:
                return signature
        return None


class QcHold(BaseModel):
    """Patient-result release hold for an analyte on an instrument.

    Placed automatically when a run is rejected; lifted only when a
    supervisor closes a verified CAPA.
    """

    model_config = ConfigDict(frozen=True)

    hold_id: str
    analyte: Code
    instrument_id: Code
    evaluation_id: str
    placed_at: AwareDatetime
    active: bool = True
    capa_id: str | None = None
    released_at: datetime | None = None
    released_by: str | None = None
