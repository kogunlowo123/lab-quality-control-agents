"""Verification run rules applied after a corrective action.

A corrective action is verified when, using only QC runs performed after the
action was recorded:

1. enough complete runs exist (every configured control level measured):
   ``min_runs`` for random error, ``min_runs_systematic`` when the failure
   was systematic (a shift needs more than one run to show it has gone);
2. every verification result is within +/-``max_abs_z`` SD (no 1_2s);
3. the Westgard rejection rules raise nothing across the verification runs;
4. for systematic failures, the mean z of each level is within
   +/-``max_mean_abs_z`` SD, i.e. the bias has been removed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from lab_quality_control_agents.domain.models import (
    ErrorType,
    QcResult,
    Severity,
    VerificationResult,
    WestgardRule,
)
from lab_quality_control_agents.domain.westgard import Observation, WestgardEngine


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_runs: int = Field(default=1, ge=1, le=10)
    min_runs_systematic: int = Field(default=2, ge=1, le=10)
    max_abs_z: float = Field(default=2.0, gt=0)
    max_mean_abs_z: float = Field(default=1.0, gt=0)


def _observation(r: QcResult) -> Observation:
    return Observation(
        result_id=r.result_id, level=r.level, z=r.z, observed_at=r.observed_at, run_id=r.run_id
    )


Run = tuple[str, list[QcResult]]


def _range_failures(runs: Sequence[Run], policy: VerificationPolicy) -> list[str]:
    return [
        f"run {run_id} level {r.level} z={r.z:+.2f} outside +/-{policy.max_abs_z:g} SD"
        for run_id, items in runs
        for r in items
        if abs(r.z) > policy.max_abs_z
    ]


def _rule_failures(runs: Sequence[Run], rules: Sequence[WestgardRule]) -> list[str]:
    rejection_rules = [r for r in rules if r is not WestgardRule.R_1_2S] or [WestgardRule.R_1_3S]
    engine = WestgardEngine(rejection_rules)
    history: list[Observation] = []
    failures: list[str] = []
    for run_id, items in runs:
        observations = [_observation(r) for r in items]
        outcome = engine.evaluate(observations, history)
        failures.extend(
            f"run {run_id} violates {v.rule.value}: {v.detail}"
            for v in outcome.violations
            if v.severity is Severity.REJECT
        )
        history.extend(observations)
    return failures


def _mean_z(runs: Sequence[Run]) -> dict[int, float]:
    by_level: dict[int, list[float]] = {}
    for _, items in runs:
        for r in items:
            by_level.setdefault(r.level, []).append(r.z)
    return {level: sum(zs) / len(zs) for level, zs in sorted(by_level.items())}


def verify(
    *,
    results: Iterable[QcResult],
    required_levels: frozenset[int],
    error_types: Sequence[ErrorType],
    rules: Sequence[WestgardRule],
    policy: VerificationPolicy,
    now: datetime,
) -> VerificationResult:
    runs: dict[str, list[QcResult]] = {}
    for result in sorted(results, key=lambda r: (r.observed_at, r.level)):
        runs.setdefault(result.run_id, []).append(result)
    complete = [
        (run_id, items)
        for run_id, items in runs.items()
        if required_levels <= {r.level for r in items}
    ]
    systematic = ErrorType.SYSTEMATIC in error_types
    needed = policy.min_runs_systematic if systematic else policy.min_runs
    if len(complete) < needed:
        return VerificationResult(
            passed=False,
            sufficient_data=False,
            run_ids=tuple(run_id for run_id, _ in complete),
            reasons=(
                f"{len(complete)} complete verification run(s); {needed} required "
                f"with levels {sorted(required_levels)}",
            ),
            evaluated_at=now,
            mean_z_by_level={},
        )
    used = complete[:needed]
    reasons = _range_failures(used, policy) + _rule_failures(used, rules)
    mean_z = _mean_z(used)
    if systematic:
        for level, value in mean_z.items():
            if abs(value) > policy.max_mean_abs_z:
                reasons.append(
                    f"level {level} mean z={value:+.2f} still biased beyond "
                    f"+/-{policy.max_mean_abs_z:g} SD"
                )
    return VerificationResult(
        passed=not reasons,
        sufficient_data=True,
        run_ids=tuple(run_id for run_id, _ in used),
        reasons=tuple(reasons) or ("all verification rules satisfied",),
        evaluated_at=now,
        mean_z_by_level={level: round(value, 4) for level, value in mean_z.items()},
    )
