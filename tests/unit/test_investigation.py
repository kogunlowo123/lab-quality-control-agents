"""Root cause scoring scenarios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lab_quality_control_agents.domain.investigation import InvestigationInput, analyse
from lab_quality_control_agents.domain.models import (
    CauseCategory,
    ErrorType,
    InstrumentEvent,
    InstrumentEventKind,
    QcResult,
    ResultSource,
    RuleScope,
    RuleViolation,
    RunEvaluation,
    RunOutcome,
    Severity,
    WestgardRule,
)

RUN_AT = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


def violation(
    rule: WestgardRule,
    error: ErrorType,
    levels: tuple[int, ...] = (1, 2),
    scope: RuleScope = RuleScope.WITHIN_RUN,
) -> RuleViolation:
    return RuleViolation(
        rule=rule,
        severity=Severity.REJECT,
        error_type=error,
        scope=scope,
        direction="high",
        levels=levels,
        result_ids=("r1",),
        detail="test",
    )


def evaluation(*violations: RuleViolation, analyte: str = "GLU") -> RunEvaluation:
    return RunEvaluation(
        evaluation_id="ev-1",
        analyte=analyte,
        instrument_id="CHEM-01",
        run_id="run-9",
        outcome=RunOutcome.REJECT,
        violations=violations,
        rules_applied=tuple(WestgardRule),
        result_ids=("r1", "r2"),
        levels=(1, 2),
        run_observed_at=RUN_AT,
        evaluated_at=RUN_AT,
        history_points=10,
    )


def result(
    level: int, *, lot: str = "L-A", reagent: str | None = "R-1", hours: float = 0
) -> QcResult:
    return QcResult(
        result_id=f"res-{level}-{hours}",
        source_key=f"k-{level}-{hours}",
        source=ResultSource.API,
        analyte="GLU",
        instrument_id="CHEM-01",
        material_id=f"GLU-L{level}",
        control_lot=lot,
        level=level,
        run_id=f"run-{hours}",
        value=100.0,
        units="mg/dL",
        z=0.0,
        observed_at=RUN_AT - timedelta(hours=hours),
        received_at=RUN_AT,
        reagent_lot=reagent,
    )


def event(
    kind: InstrumentEventKind,
    hours_before: float,
    *,
    analyte: str | None = "GLU",
    lot: str | None = None,
) -> InstrumentEvent:
    when = RUN_AT - timedelta(hours=hours_before)
    return InstrumentEvent(
        event_id=f"ev-{kind.value}-{hours_before}",
        instrument_id="CHEM-01",
        kind=kind,
        occurred_at=when,
        analyte=analyte,
        lot=lot,
        recorded_by="tess@lab.test",
        recorded_at=when,
    )


def run(
    ev: RunEvaluation,
    *,
    current: list[QcResult] | None = None,
    previous: list[QcResult] | None = None,
    events: list[InstrumentEvent] | None = None,
    others: list[RunEvaluation] | None = None,
    last_accepted_at: datetime | None = RUN_AT - timedelta(hours=8),
    last_calibration_at: datetime | None = None,
) -> list[CauseCategory]:
    hypotheses = analyse(
        InvestigationInput(
            evaluation=ev,
            run_results=current or [result(1), result(2)],
            previous_results=previous or [result(1, hours=8), result(2, hours=8)],
            events=events or [],
            other_rejections=others or [],
            last_accepted_at=last_accepted_at,
            last_calibration_at=last_calibration_at,
            calibration_interval=timedelta(days=14),
        )
    )
    assert sum(h.probability for h in hypotheses) == pytest.approx(1.0, abs=1e-3)
    assert [h.hypothesis_id for h in hypotheses] == [f"H{i}" for i in range(1, len(hypotheses) + 1)]
    assert all(h.corrective_actions for h in hypotheses)
    return [h.category for h in hypotheses]


SHIFT = violation(WestgardRule.R_2_2S, ErrorType.SYSTEMATIC)
RANDOM = violation(WestgardRule.R_1_3S, ErrorType.RANDOM, levels=(1,), scope=RuleScope.SINGLE)


def test_reagent_lot_change_seen_in_results_leads_a_shift() -> None:
    ranking = run(
        evaluation(SHIFT),
        current=[result(1, reagent="R-2"), result(2, reagent="R-2")],
        previous=[result(1, reagent="R-1", hours=8)],
    )
    assert ranking[0] is CauseCategory.REAGENT_LOT


def test_recent_calibration_leads_a_shift() -> None:
    ranking = run(evaluation(SHIFT), events=[event(InstrumentEventKind.CALIBRATION, 2)])
    assert ranking[0] is CauseCategory.CALIBRATION


def test_isolated_random_error_points_at_the_analytical_run() -> None:
    ranking = run(evaluation(RANDOM))
    assert ranking[0] is CauseCategory.RANDOM_ANALYTICAL


def test_hardware_fault_leads_random_error() -> None:
    ranking = run(
        evaluation(RANDOM), events=[event(InstrumentEventKind.HARDWARE_FAULT, 1, analyte=None)]
    )
    assert ranking[0] is CauseCategory.INSTRUMENT


def test_single_level_with_control_lot_change_points_at_control_material() -> None:
    one_level = violation(
        WestgardRule.R_2_2S, ErrorType.SYSTEMATIC, levels=(2,), scope=RuleScope.ACROSS_RUNS
    )
    ranking = run(
        evaluation(one_level),
        current=[result(1), result(2, lot="L-B")],
        previous=[result(1, hours=8), result(2, lot="L-A", hours=8)],
    )
    assert ranking[0] is CauseCategory.CONTROL_MATERIAL


def test_other_analytes_failing_on_the_instrument_add_instrument_evidence() -> None:
    others = [evaluation(RANDOM, analyte="NA"), evaluation(RANDOM, analyte="K")]
    hypotheses = analyse(
        InvestigationInput(
            evaluation=evaluation(RANDOM),
            run_results=[result(1)],
            previous_results=[],
            events=[],
            other_rejections=others,
            last_accepted_at=None,
            last_calibration_at=None,
            calibration_interval=timedelta(days=14),
        )
    )
    instrument = next(h for h in hypotheses if h.category is CauseCategory.INSTRUMENT)
    assert hypotheses[0].category is CauseCategory.INSTRUMENT
    assert any("K, NA" in e.description for e in instrument.evidence)


def test_trend_with_overdue_calibration_supports_drift_and_calibration() -> None:
    trend = violation(WestgardRule.R_10_X, ErrorType.SYSTEMATIC, scope=RuleScope.ACROSS_RUNS)
    hypotheses = analyse(
        InvestigationInput(
            evaluation=evaluation(trend),
            run_results=[result(1), result(2)],
            previous_results=[],
            events=[],
            other_rejections=[],
            last_accepted_at=None,
            last_calibration_at=RUN_AT - timedelta(days=20),
            calibration_interval=timedelta(days=14),
        )
    )
    categories = [h.category for h in hypotheses][:2]
    assert set(categories) == {CauseCategory.DRIFT, CauseCategory.CALIBRATION}
    drift = next(h for h in hypotheses if h.category is CauseCategory.DRIFT)
    assert any("20 days" in e.description for e in drift.evidence)


def test_events_after_the_run_or_for_other_analytes_are_ignored() -> None:
    later = event(InstrumentEventKind.HARDWARE_FAULT, -1)
    other = event(InstrumentEventKind.HARDWARE_FAULT, 1, analyte="NA")
    hypotheses = analyse(
        InvestigationInput(
            evaluation=evaluation(RANDOM),
            run_results=[result(1)],
            previous_results=[],
            events=[later, other],
            other_rejections=[],
            last_accepted_at=None,
            last_calibration_at=None,
            calibration_interval=timedelta(days=14),
        )
    )
    assert hypotheses[0].category is CauseCategory.RANDOM_ANALYTICAL
    assert all(e.kind != "event" for h in hypotheses for e in h.evidence)


def test_events_before_last_accepted_run_count_half() -> None:
    def weight(last_accepted: datetime | None) -> float:
        hypotheses = analyse(
            InvestigationInput(
                evaluation=evaluation(SHIFT),
                run_results=[result(1), result(2)],
                previous_results=[],
                events=[event(InstrumentEventKind.CALIBRATION, 10, lot="CAL-7")],
                other_rejections=[],
                last_accepted_at=last_accepted,
                last_calibration_at=None,
                calibration_interval=timedelta(days=14),
            )
        )
        calibration = next(h for h in hypotheses if h.category is CauseCategory.CALIBRATION)
        evidence = next(e for e in calibration.evidence if e.kind == "event")
        assert "lot CAL-7" in evidence.description
        return evidence.weight

    assert weight(RUN_AT - timedelta(hours=5)) == pytest.approx(weight(None) / 2, rel=1e-3)
