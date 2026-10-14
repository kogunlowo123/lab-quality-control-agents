"""Root cause analysis for rejected QC runs.

Scoring is deterministic and explainable. Each candidate cause starts from a
prior that depends on the error pattern (random vs systematic, one level vs
all levels, trend rules) and accumulates evidence:

* recorded instrument events (calibration, maintenance, lot changes, faults)
  weighted by recency relative to the rejected run, with half the weight for
  events before the last accepted run (they were already "tested" by QC);
* reagent and control lot changes seen directly in the results;
* other analytes rejected on the same instrument (points at the instrument);
* calibration age against the analyte's calibration interval.

Scores are normalised to probabilities. The ranking is authoritative; an
optional language-model narrative may only describe it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from lab_quality_control_agents.domain.models import (
    CauseCategory,
    ErrorType,
    Evidence,
    Hypothesis,
    InstrumentEvent,
    InstrumentEventKind,
    QcResult,
    RuleScope,
    RunEvaluation,
    Severity,
    WestgardRule,
)

CORRECTIVE_ACTIONS: dict[CauseCategory, tuple[str, ...]] = {
    CauseCategory.REAGENT_LOT: (
        "Perform lot-to-lot verification of the new reagent lot with patient comparison samples",
        "Recalibrate with the new reagent lot where the manufacturer requires lot calibration",
        "Repeat QC at all levels after recalibration",
    ),
    CauseCategory.CALIBRATION: (
        "Review the calibration curve, calibrator lot and calibrator expiry",
        "Recalibrate with freshly reconstituted calibrator",
        "Repeat QC at all levels after recalibration",
    ),
    CauseCategory.CONTROL_MATERIAL: (
        "Open or reconstitute a fresh vial of control material and repeat QC",
        "Check control lot expiry, storage temperature and reconstitution records",
        "Confirm the assigned target values for the control lot in use",
    ),
    CauseCategory.INSTRUMENT: (
        "Inspect sample and reagent probes, lamp, cuvettes and reaction temperature",
        "Review instrument error logs and complete outstanding maintenance",
        "Repeat QC at all levels after maintenance",
    ),
    CauseCategory.RANDOM_ANALYTICAL: (
        "Inspect the control aliquot for bubbles, clots, evaporation or mislabelling",
        "Repeat QC once on a fresh aliquot; investigate further if it fails again",
    ),
    CauseCategory.DRIFT: (
        "Review the Levey-Jennings chart to locate the onset of the shift or trend",
        "Replace the on-board reagent pack if on-board stability is exceeded",
        "Recalibrate and repeat QC at all levels",
    ),
}

PREVENTIVE_ACTIONS: dict[CauseCategory, tuple[str, ...]] = {
    CauseCategory.REAGENT_LOT: (
        "Require documented lot-to-lot verification before a reagent lot enters service",
    ),
    CauseCategory.CALIBRATION: (
        "Review calibration frequency against the manufacturer interval and QC trend",
    ),
    CauseCategory.CONTROL_MATERIAL: (
        "Record control vial open dates and enforce open-vial stability limits",
    ),
    CauseCategory.INSTRUMENT: (
        "Review the preventive maintenance schedule and instrument error trends",
    ),
    CauseCategory.RANDOM_ANALYTICAL: (
        "Review control handling technique and pipetting checks with staff",
    ),
    CauseCategory.DRIFT: ("Include 4_1s and 10_x trend review in the daily QC review",),
}

_TREND_RULES = frozenset({WestgardRule.R_4_1S, WestgardRule.R_10_X})
_RECENCY_HOURS = 24.0


@dataclass(frozen=True)
class InvestigationInput:
    evaluation: RunEvaluation
    run_results: Sequence[QcResult]
    previous_results: Sequence[QcResult]
    events: Sequence[InstrumentEvent]
    other_rejections: Sequence[RunEvaluation]
    last_accepted_at: datetime | None
    last_calibration_at: datetime | None
    calibration_interval: timedelta


@dataclass
class _Score:
    prior: float
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.prior + sum(e.weight for e in self.evidence)


def _recency(event_at: datetime, run_at: datetime, last_accepted_at: datetime | None) -> float:
    hours = max((run_at - event_at).total_seconds() / 3600, 0.0)
    weight = math.exp(-hours / _RECENCY_HOURS)
    if last_accepted_at is not None and event_at < last_accepted_at:
        weight *= 0.5
    return weight


def _applies(event: InstrumentEvent, analyte: str) -> bool:
    return event.analyte is None or event.analyte == analyte


def analyse(data: InvestigationInput) -> tuple[Hypothesis, ...]:
    evaluation = data.evaluation
    run_at = evaluation.run_observed_at
    rejects = [v for v in evaluation.violations if v.severity is Severity.REJECT]
    systematic = any(v.error_type is ErrorType.SYSTEMATIC for v in rejects)
    random_error = any(v.error_type is ErrorType.RANDOM for v in rejects)
    # A systematic pattern confined to one control level while the other
    # levels behave points at that control material; single-result rules
    # (1_3s) are one level by nature and carry no such signal.
    affected_levels = {
        lvl for v in rejects if v.error_type is ErrorType.SYSTEMATIC for lvl in v.levels
    }
    single_level = len(affected_levels) == 1 and len(set(evaluation.levels)) > 1
    trend = any(v.rule in _TREND_RULES and v.scope is not RuleScope.WITHIN_RUN for v in rejects)

    scores: dict[CauseCategory, _Score] = {
        CauseCategory.REAGENT_LOT: _Score(0.3 if systematic else 0.05),
        CauseCategory.CALIBRATION: _Score(0.3 if systematic else 0.05),
        CauseCategory.CONTROL_MATERIAL: _Score(0.4 if single_level else 0.1),
        CauseCategory.INSTRUMENT: _Score(0.25 if random_error else 0.1),
        CauseCategory.RANDOM_ANALYTICAL: _Score(0.3 if random_error else 0.0),
        CauseCategory.DRIFT: _Score(0.25 if trend else (0.05 if systematic else 0.0)),
    }
    for violation in rejects:
        target = (
            CauseCategory.DRIFT
            if violation.rule in _TREND_RULES and trend
            else (
                CauseCategory.RANDOM_ANALYTICAL
                if violation.error_type is ErrorType.RANDOM
                else CauseCategory.CALIBRATION
            )
        )
        scores[target].evidence.append(
            Evidence(
                kind="rule",
                description=f"{violation.rule.value} ({violation.scope.value}): {violation.detail}",
                weight=0.0,
            )
        )
    if single_level:
        scores[CauseCategory.CONTROL_MATERIAL].evidence.append(
            Evidence(
                kind="pattern",
                description=f"only control level {next(iter(affected_levels))} is affected",
                weight=0.3,
            )
        )

    _lot_changes(data, scores)
    _events(data, scores, systematic=systematic)

    distinct_other = sorted({e.analyte for e in data.other_rejections})
    if distinct_other:
        scores[CauseCategory.INSTRUMENT].evidence.append(
            Evidence(
                kind="cross_analyte",
                description=(
                    f"{len(distinct_other)} other analyte(s) rejected on {evaluation.instrument_id}"
                    f" in the same window: {', '.join(distinct_other)}"
                ),
                weight=min(0.4 * len(distinct_other), 1.2),
            )
        )

    if data.last_calibration_at is not None:
        age = run_at - data.last_calibration_at
        if age > data.calibration_interval:
            overdue = Evidence(
                kind="timing",
                description=(
                    f"last calibration {age.days} days before the run exceeds the "
                    f"{data.calibration_interval.days}-day interval"
                ),
                weight=0.5,
            )
            scores[CauseCategory.CALIBRATION].evidence.append(overdue)
            if systematic:
                scores[CauseCategory.DRIFT].evidence.append(
                    overdue.model_copy(update={"weight": 0.4})
                )

    relevant_events = [
        e for e in data.events if e.occurred_at <= run_at and _applies(e, evaluation.analyte)
    ]
    if random_error and not relevant_events and not distinct_other:
        scores[CauseCategory.RANDOM_ANALYTICAL].evidence.append(
            Evidence(
                kind="pattern",
                description="isolated random error with no recorded instrument change",
                weight=0.4,
            )
        )

    ranked = sorted(
        ((cat, s) for cat, s in scores.items() if s.total > 0),
        key=lambda item: (-item[1].total, list(CauseCategory).index(item[0])),
    )
    total = sum(s.total for _, s in ranked)
    return tuple(
        Hypothesis(
            hypothesis_id=f"H{index}",
            category=category,
            probability=round(score.total / total, 4),
            score=round(score.total, 4),
            evidence=tuple(score.evidence),
            corrective_actions=CORRECTIVE_ACTIONS[category],
            preventive_actions=PREVENTIVE_ACTIONS[category],
        )
        for index, (category, score) in enumerate(ranked, start=1)
    )


def _lot_changes(data: InvestigationInput, scores: dict[CauseCategory, _Score]) -> None:
    previous_by_level: dict[int, QcResult] = {}
    for result in sorted(data.previous_results, key=lambda r: r.observed_at):
        previous_by_level[result.level] = result
    previous_reagent = next(
        (
            r.reagent_lot
            for r in sorted(data.previous_results, key=lambda r: r.observed_at, reverse=True)
            if r.reagent_lot
        ),
        None,
    )
    reagent_reported = False
    for result in data.run_results:
        prior = previous_by_level.get(result.level)
        if prior is not None and prior.control_lot != result.control_lot:
            scores[CauseCategory.CONTROL_MATERIAL].evidence.append(
                Evidence(
                    kind="lot_change",
                    description=(
                        f"control lot for level {result.level} changed "
                        f"{prior.control_lot} -> {result.control_lot}"
                    ),
                    reference=result.result_id,
                    weight=0.9,
                )
            )
        if (
            not reagent_reported
            and result.reagent_lot
            and previous_reagent
            and result.reagent_lot != previous_reagent
        ):
            reagent_reported = True
            scores[CauseCategory.REAGENT_LOT].evidence.append(
                Evidence(
                    kind="lot_change",
                    description=f"reagent lot changed {previous_reagent} -> {result.reagent_lot}",
                    reference=result.result_id,
                    weight=1.0,
                )
            )


_EVENT_EFFECTS: dict[InstrumentEventKind, tuple[CauseCategory, float, float]] = {
    # kind -> (category, weight when systematic, weight when random)
    InstrumentEventKind.REAGENT_LOT_CHANGE: (CauseCategory.REAGENT_LOT, 0.8, 0.3),
    InstrumentEventKind.CALIBRATION: (CauseCategory.CALIBRATION, 0.9, 0.3),
    InstrumentEventKind.CONTROL_LOT_CHANGE: (CauseCategory.CONTROL_MATERIAL, 0.7, 0.5),
    InstrumentEventKind.CONTROL_VIAL_OPENED: (CauseCategory.CONTROL_MATERIAL, 0.6, 0.5),
    InstrumentEventKind.MAINTENANCE: (CauseCategory.INSTRUMENT, 0.4, 0.6),
    InstrumentEventKind.HARDWARE_FAULT: (CauseCategory.INSTRUMENT, 0.8, 1.0),
    InstrumentEventKind.SOFTWARE_UPDATE: (CauseCategory.INSTRUMENT, 0.4, 0.4),
}


def _events(
    data: InvestigationInput, scores: dict[CauseCategory, _Score], *, systematic: bool
) -> None:
    run_at = data.evaluation.run_observed_at
    for event in sorted(data.events, key=lambda e: e.occurred_at):
        if event.occurred_at > run_at or not _applies(event, data.evaluation.analyte):
            continue
        category, w_sys, w_rand = _EVENT_EFFECTS[event.kind]
        recency = _recency(event.occurred_at, run_at, data.last_accepted_at)
        hours = (run_at - event.occurred_at).total_seconds() / 3600
        scope = event.analyte or "all analytes"
        scores[category].evidence.append(
            Evidence(
                kind="event",
                description=(
                    f"{event.kind.value} ({scope}) {hours:.1f} h before the run"
                    + (f", lot {event.lot}" if event.lot else "")
                ),
                reference=event.event_id,
                weight=round((w_sys if systematic else w_rand) * recency, 4),
            )
        )
