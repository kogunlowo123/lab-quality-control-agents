"""Levey-Jennings statistics, target establishment and sigma metrics.

* Target establishment follows CLSI C24: the laboratory's own mean and SD
  from at least 20 control measurements (configurable) on separate runs.
* SD is the sample standard deviation (n - 1 denominator).
* Sigma metric: sigma = (TEa% - |bias%|) / CV%, with bias measured against
  the assigned target mean and CV from the observed imprecision.
* Rule recommendations follow the Westgard Sigma Rules for two control
  levels per run (N = controls per run, R = runs the rules span).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab_quality_control_agents.domain.errors import InsufficientDataError
from lab_quality_control_agents.domain.models import QcTarget, WestgardRule


class DescriptiveStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    mean: float
    sd: float
    cv_percent: float | None = Field(description="None when the mean is zero")
    minimum: float
    maximum: float


def describe(values: Sequence[float]) -> DescriptiveStats:
    if len(values) < 2:
        raise InsufficientDataError("at least two values are needed for an SD")
    if not all(math.isfinite(v) for v in values):
        raise InsufficientDataError("values must be finite numbers")
    n = len(values)
    mean = math.fsum(values) / n
    variance = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(variance)
    cv = abs(sd / mean) * 100 if mean != 0 else None
    return DescriptiveStats(
        n=n, mean=mean, sd=sd, cv_percent=cv, minimum=min(values), maximum=max(values)
    )


def establish(values: Sequence[float], *, min_n: int = 20) -> DescriptiveStats:
    """Laboratory target from baseline data; refuses too few points or zero spread."""
    if len(values) < min_n:
        raise InsufficientDataError(
            f"{len(values)} values supplied; at least {min_n} are required to establish a target"
        )
    stats = describe(values)
    if stats.sd == 0:
        raise InsufficientDataError("baseline values have zero spread; SD cannot be zero")
    return stats


class ControlLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: float
    sd: float
    plus_1sd: float
    minus_1sd: float
    plus_2sd: float
    minus_2sd: float
    plus_3sd: float
    minus_3sd: float

    @classmethod
    def of(cls, target: QcTarget) -> ControlLimits:
        m, s = target.mean, target.sd
        return cls(
            mean=m,
            sd=s,
            plus_1sd=m + s,
            minus_1sd=m - s,
            plus_2sd=m + 2 * s,
            minus_2sd=m - 2 * s,
            plus_3sd=m + 3 * s,
            minus_3sd=m - 3 * s,
        )


class LeveyJenningsPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    result_id: str
    run_id: str
    observed_at: datetime
    value: float
    z: float
    rules_violated: tuple[WestgardRule, ...]


class LeveyJenningsChart(BaseModel):
    model_config = ConfigDict(frozen=True)

    analyte: str
    instrument_id: str
    material_id: str
    control_lot: str
    level: int
    units: str
    limits: ControlLimits
    points: tuple[LeveyJenningsPoint, ...]
    observed: DescriptiveStats | None
    bias_percent: float | None = Field(description="Observed mean vs target mean")


def bias_percent(observed_mean: float, target_mean: float) -> float:
    if target_mean == 0:
        raise InsufficientDataError("bias percent is undefined for a zero target mean")
    return (observed_mean - target_mean) / target_mean * 100


class AllowableError(BaseModel):
    """Total allowable error: a percentage, an absolute amount, or the greater of both."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    percent: float | None = Field(default=None, gt=0, le=100)
    absolute: float | None = Field(default=None, gt=0)
    source: str = Field(min_length=3)

    @model_validator(mode="after")
    def _one_limit(self) -> AllowableError:
        if self.percent is None and self.absolute is None:
            raise ValueError("percent or absolute is required")
        return self

    def percent_at(self, concentration: float) -> float:
        """TEa as a percentage at a concentration (the greater limit applies)."""
        candidates = []
        if self.percent is not None:
            candidates.append(self.percent)
        if self.absolute is not None:
            if concentration <= 0:
                raise InsufficientDataError("absolute TEa needs a positive concentration")
            candidates.append(self.absolute / concentration * 100)
        return max(candidates)


class SigmaQuality(StrEnum):
    WORLD_CLASS = "world_class"  # >= 6
    EXCELLENT = "excellent"  # 5 - 6
    GOOD = "good"  # 4 - 5
    MARGINAL = "marginal"  # 3 - 4
    POOR = "poor"  # 2 - 3
    UNACCEPTABLE = "unacceptable"  # < 2


class QcDesign(BaseModel):
    model_config = ConfigDict(frozen=True)

    quality: SigmaQuality
    rules: tuple[WestgardRule, ...]
    controls_per_run: int = Field(description="N: control measurements per run")
    runs: int = Field(description="R: runs across which the rules are applied")
    method_improvement_required: bool


class SigmaAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    analyte: str
    instrument_id: str
    material_id: str
    level: int
    n: int
    target_mean: float
    observed_mean: float
    cv_percent: float
    bias_percent: float
    tea_percent: float
    tea_source: str
    sigma: float
    design: QcDesign


def sigma_metric(tea_percent: float, bias_pct: float, cv_percent: float) -> float:
    if cv_percent <= 0:
        raise InsufficientDataError("CV must be positive to compute a sigma metric")
    return (tea_percent - abs(bias_pct)) / cv_percent


_MULTIRULE = (
    WestgardRule.R_1_3S,
    WestgardRule.R_2_2S,
    WestgardRule.R_R_4S,
    WestgardRule.R_4_1S,
    WestgardRule.R_10_X,
)


def recommend(sigma: float) -> QcDesign:
    """Westgard Sigma Rules for two control levels."""
    if sigma >= 6:
        return QcDesign(
            quality=SigmaQuality.WORLD_CLASS,
            rules=(WestgardRule.R_1_3S,),
            controls_per_run=2,
            runs=1,
            method_improvement_required=False,
        )
    if sigma >= 5:
        return QcDesign(
            quality=SigmaQuality.EXCELLENT,
            rules=(WestgardRule.R_1_3S, WestgardRule.R_2_2S, WestgardRule.R_R_4S),
            controls_per_run=2,
            runs=1,
            method_improvement_required=False,
        )
    if sigma >= 4:
        return QcDesign(
            quality=SigmaQuality.GOOD,
            rules=(
                WestgardRule.R_1_3S,
                WestgardRule.R_2_2S,
                WestgardRule.R_R_4S,
                WestgardRule.R_4_1S,
            ),
            controls_per_run=4,
            runs=1,
            method_improvement_required=False,
        )
    quality = (
        SigmaQuality.MARGINAL
        if sigma >= 3
        else SigmaQuality.POOR if sigma >= 2 else SigmaQuality.UNACCEPTABLE
    )
    return QcDesign(
        quality=quality,
        rules=(WestgardRule.R_1_2S, *_MULTIRULE),
        controls_per_run=4,
        runs=2,
        method_improvement_required=sigma < 3,
    )


def assess_sigma(
    target: QcTarget,
    values: Sequence[float],
    tea: AllowableError,
    *,
    min_n: int = 20,
) -> SigmaAssessment:
    if len(values) < min_n:
        raise InsufficientDataError(
            f"{len(values)} results available; at least {min_n} are required for a sigma metric"
        )
    stats = describe(values)
    if stats.cv_percent is None or stats.cv_percent == 0:
        raise InsufficientDataError("observed CV is zero or undefined")
    bias = bias_percent(stats.mean, target.mean)
    tea_pct = tea.percent_at(target.mean)
    sigma = sigma_metric(tea_pct, bias, stats.cv_percent)
    return SigmaAssessment(
        analyte=target.analyte,
        instrument_id=target.instrument_id,
        material_id=target.material_id,
        level=target.level,
        n=stats.n,
        target_mean=target.mean,
        observed_mean=stats.mean,
        cv_percent=stats.cv_percent,
        bias_percent=bias,
        tea_percent=tea_pct,
        tea_source=tea.source,
        sigma=sigma,
        design=recommend(sigma),
    )
