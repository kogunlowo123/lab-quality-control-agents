"""Westgard multirule engine verified against worked examples.

Single-material examples use the classic teaching control: cholesterol with
mean 200 mg/dL and SD 4 mg/dL, so the limits are 196/204 (1 SD), 192/208
(2 SD) and 188/212 (3 SD). Two-level examples use the glucose controls in
conftest (level 1: 100 +/- 2.5, level 2: 300 +/- 6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lab_quality_control_agents.domain.models import (
    ErrorType,
    RuleScope,
    RunOutcome,
    Severity,
    WestgardRule,
)
from lab_quality_control_agents.domain.westgard import Observation, WestgardEngine, side

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
ENGINE = WestgardEngine()


def chol(value: float) -> float:
    return (value - 200.0) / 4.0


def obs(rid: str, z: float, *, run: int, level: int = 1) -> Observation:
    return Observation(
        result_id=rid, level=level, z=z, observed_at=T0 + timedelta(hours=run), run_id=f"run-{run}"
    )


def history(values: list[float], *, level: int = 1, start: int = 0) -> list[Observation]:
    return [obs(f"h{level}-{i}", chol(v), run=start + i, level=level) for i, v in enumerate(values)]


def rules(result: object) -> set[tuple[WestgardRule, RuleScope]]:
    return {(v.rule, v.scope) for v in result.violations}  # type: ignore[attr-defined]


class TestSingleObservationRules:
    def test_value_inside_2sd_accepts(self) -> None:
        result = ENGINE.evaluate([obs("c", chol(207.0), run=50)])
        assert result.outcome is RunOutcome.ACCEPT
        assert result.violations == ()

    def test_exactly_2sd_is_not_beyond(self) -> None:
        assert side(chol(208.0), 2.0) == 0
        assert ENGINE.evaluate([obs("c", chol(208.0), run=50)]).outcome is RunOutcome.ACCEPT

    def test_1_2s_warning_at_209(self) -> None:
        result = ENGINE.evaluate([obs("c", chol(209.0), run=50)])
        assert result.outcome is RunOutcome.WARNING
        (violation,) = result.violations
        assert violation.rule is WestgardRule.R_1_2S
        assert violation.severity is Severity.WARNING
        assert violation.direction == "high"
        assert "z=+2.25" in violation.detail

    def test_1_3s_rejects_213_as_random_error(self) -> None:
        result = ENGINE.evaluate([obs("c", chol(213.0), run=50)])
        assert result.outcome is RunOutcome.REJECT
        assert (WestgardRule.R_1_3S, RuleScope.SINGLE) in rules(result)
        reject = next(v for v in result.violations if v.rule is WestgardRule.R_1_3S)
        assert reject.error_type is ErrorType.RANDOM
        assert reject.result_ids == ("c",)

    def test_1_3s_low_side(self) -> None:
        result = ENGINE.evaluate([obs("c", chol(187.0), run=50)])
        reject = next(v for v in result.violations if v.rule is WestgardRule.R_1_3S)
        assert reject.direction == "low"


class TestTwoTwoS:
    def test_consecutive_runs_same_level(self) -> None:
        past = history([209.0])
        result = ENGINE.evaluate([obs("c", chol(210.0), run=50)], past)
        assert result.outcome is RunOutcome.REJECT
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_2_2S)
        assert violation.scope is RuleScope.ACROSS_RUNS
        assert violation.error_type is ErrorType.SYSTEMATIC
        assert violation.result_ids == ("h1-0", "c")

    def test_opposite_sides_do_not_count(self) -> None:
        past = history([191.0])
        result = ENGINE.evaluate([obs("c", chol(209.0), run=50)], past)
        assert WestgardRule.R_2_2S not in {v.rule for v in result.violations}

    def test_within_run_across_two_levels(self) -> None:
        current = [obs("l1", 2.2, run=50, level=1), obs("l2", 2.2, run=50, level=2)]
        result = ENGINE.evaluate(current)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_2_2S)
        assert violation.scope is RuleScope.WITHIN_RUN
        assert violation.levels == (1, 2)

    def test_old_pair_is_not_reported_against_a_later_run(self) -> None:
        past = history([209.0, 210.0])
        result = ENGINE.evaluate([obs("c", chol(201.0), run=50)], past)
        assert result.outcome is RunOutcome.ACCEPT


class TestRangeFourS:
    def test_one_above_plus_two_one_below_minus_two(self) -> None:
        current = [obs("l1", chol(209.0), run=50, level=1), obs("l2", chol(191.0), run=50, level=2)]
        result = ENGINE.evaluate(current)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_R_4S)
        assert violation.scope is RuleScope.WITHIN_RUN
        assert violation.direction == "both"
        assert violation.error_type is ErrorType.RANDOM
        assert "range 4.50 SD" in violation.detail

    def test_range_over_4sd_without_crossing_both_limits_is_not_r4s(self) -> None:
        # +1.9 and -2.4: range 4.3 SD but the high result is inside +2 SD.
        current = [obs("l1", 1.9, run=50, level=1), obs("l2", -2.4, run=50, level=2)]
        result = ENGINE.evaluate(current)
        assert WestgardRule.R_R_4S not in {v.rule for v in result.violations}


class TestFourOneS:
    def test_four_consecutive_beyond_1sd_one_level(self) -> None:
        past = history([205.0, 206.0, 205.0])
        result = ENGINE.evaluate([obs("c", chol(207.0), run=50)], past)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_4_1S)
        assert violation.scope is RuleScope.ACROSS_RUNS
        assert violation.result_ids == ("h1-0", "h1-1", "h1-2", "c")
        assert result.outcome is RunOutcome.REJECT

    def test_exactly_1sd_breaks_the_streak(self) -> None:
        past = history([205.0, 204.0, 205.0])
        result = ENGINE.evaluate([obs("c", chol(207.0), run=50)], past)
        assert WestgardRule.R_4_1S not in {v.rule for v in result.violations}

    def test_across_levels_and_runs(self) -> None:
        past = [obs("p1", 1.2, run=1, level=1), obs("p2", 1.5, run=1, level=2)]
        current = [obs("c1", 1.1, run=2, level=1), obs("c2", 1.3, run=2, level=2)]
        result = ENGINE.evaluate(current, past)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_4_1S)
        assert violation.scope is RuleScope.ACROSS_LEVELS_AND_RUNS
        assert violation.levels == (1, 2)


class TestTenX:
    def test_ten_consecutive_above_mean_one_level(self) -> None:
        past = history([201.0, 202.0, 203.0, 201.0, 202.0, 201.0, 203.0, 202.0, 201.0])
        result = ENGINE.evaluate([obs("c", chol(202.0), run=50)], past)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_10_X)
        assert violation.scope is RuleScope.ACROSS_RUNS
        assert len(violation.result_ids) == 10
        assert "above the mean" in violation.detail

    def test_nine_is_not_enough(self) -> None:
        past = history([201.0, 202.0, 203.0, 201.0, 202.0, 201.0, 203.0, 202.0])
        result = ENGINE.evaluate([obs("c", chol(202.0), run=50)], past)
        assert WestgardRule.R_10_X not in {v.rule for v in result.violations}

    def test_value_on_the_mean_breaks_the_streak(self) -> None:
        past = history([201.0, 202.0, 203.0, 201.0, 200.0, 201.0, 203.0, 202.0, 201.0])
        result = ENGINE.evaluate([obs("c", chol(202.0), run=50)], past)
        assert WestgardRule.R_10_X not in {v.rule for v in result.violations}

    def test_across_levels_five_runs_of_two(self) -> None:
        past = [
            obs(f"p{run}-{level}", -0.4, run=run, level=level)
            for run in range(4)
            for level in (1, 2)
        ]
        current = [obs("c1", -0.3, run=5, level=1), obs("c2", -0.6, run=5, level=2)]
        result = ENGINE.evaluate(current, past)
        violation = next(v for v in result.violations if v.rule is WestgardRule.R_10_X)
        assert violation.scope is RuleScope.ACROSS_LEVELS_AND_RUNS
        assert violation.direction == "low"
        # Each level alone has only five points: no within-level 10_x.
        assert all(
            v.scope is not RuleScope.ACROSS_RUNS
            for v in result.violations
            if v.rule is WestgardRule.R_10_X
        )


class TestEngineConfiguration:
    def test_rule_subset_is_respected(self) -> None:
        engine = WestgardEngine([WestgardRule.R_1_3S])
        past = history([209.0])
        result = engine.evaluate([obs("c", chol(210.0), run=50)], past)
        assert result.outcome is RunOutcome.ACCEPT
        assert engine.rules == (WestgardRule.R_1_3S,)

    def test_rules_are_reported_in_canonical_order(self) -> None:
        engine = WestgardEngine([WestgardRule.R_10_X, WestgardRule.R_1_2S])
        assert engine.rules == (WestgardRule.R_1_2S, WestgardRule.R_10_X)

    def test_empty_run_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one observation"):
            ENGINE.evaluate([])

    def test_no_rules_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one rule"):
            WestgardEngine([])

    def test_combined_failure_reports_every_rule(self) -> None:
        # 213 (1_3s, 1_2s) after 209 (2_2s) after 205, 206 (4_1s with 209, 213).
        past = history([205.0, 206.0, 209.0])
        result = ENGINE.evaluate([obs("c", chol(213.0), run=50)], past)
        assert {v.rule for v in result.violations} == {
            WestgardRule.R_1_2S,
            WestgardRule.R_1_3S,
            WestgardRule.R_2_2S,
            WestgardRule.R_4_1S,
        }
