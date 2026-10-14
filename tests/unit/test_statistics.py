"""Levey-Jennings statistics, target establishment and sigma metrics against hand calculations."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from lab_quality_control_agents.domain.errors import InsufficientDataError
from lab_quality_control_agents.domain.models import QcTarget, WestgardRule
from lab_quality_control_agents.domain.statistics import (
    AllowableError,
    ControlLimits,
    SigmaQuality,
    assess_sigma,
    bias_percent,
    describe,
    establish,
    recommend,
    sigma_metric,
)

TARGET = QcTarget(
    analyte="GLU",
    instrument_id="CHEM-01",
    material_id="GLU-L1",
    control_lot="L1-2026A",
    level=1,
    mean=100.0,
    sd=2.5,
    units="mg/dL",
    effective_from=datetime(2026, 10, 1, tzinfo=UTC),
)


class TestDescriptive:
    def test_sample_sd_and_cv(self) -> None:
        stats = describe([98.0, 100.0, 102.0, 101.0, 99.0])
        assert stats.n == 5
        assert stats.mean == pytest.approx(100.0)
        # Sum of squared deviations 4+0+4+1+1 = 10, n-1 = 4.
        assert stats.sd == pytest.approx(math.sqrt(10 / 4))
        assert stats.cv_percent == pytest.approx(math.sqrt(2.5))
        assert (stats.minimum, stats.maximum) == (98.0, 102.0)

    def test_zero_mean_has_no_cv(self) -> None:
        assert describe([-1.0, 1.0]).cv_percent is None

    def test_needs_two_finite_values(self) -> None:
        with pytest.raises(InsufficientDataError):
            describe([1.0])
        with pytest.raises(InsufficientDataError, match="finite"):
            describe([1.0, math.nan])

    def test_establish_requires_twenty_points(self) -> None:
        with pytest.raises(InsufficientDataError, match="at least 20"):
            establish([100.0] * 19)
        stats = establish([99.0, 101.0] * 10)
        assert stats.n == 20
        assert stats.mean == pytest.approx(100.0)
        assert stats.sd == pytest.approx(math.sqrt(20 / 19))

    def test_establish_rejects_zero_spread(self) -> None:
        with pytest.raises(InsufficientDataError, match="zero spread"):
            establish([100.0] * 20)


class TestLimitsAndBias:
    def test_control_limits(self) -> None:
        limits = ControlLimits.of(TARGET)
        assert (limits.minus_3sd, limits.minus_2sd, limits.minus_1sd) == (92.5, 95.0, 97.5)
        assert (limits.plus_1sd, limits.plus_2sd, limits.plus_3sd) == (102.5, 105.0, 107.5)

    def test_target_z(self) -> None:
        assert TARGET.z(105.5) == pytest.approx(2.2)

    def test_bias_percent(self) -> None:
        assert bias_percent(102.0, 100.0) == pytest.approx(2.0)
        assert bias_percent(97.0, 100.0) == pytest.approx(-3.0)
        with pytest.raises(InsufficientDataError):
            bias_percent(1.0, 0.0)


class TestAllowableError:
    def test_percent_only(self) -> None:
        assert AllowableError(percent=10, source="CLIA").percent_at(200) == 10

    def test_greater_of_percent_and_absolute(self) -> None:
        glucose = AllowableError(percent=8, absolute=6, source="CLIA")
        assert glucose.percent_at(100) == pytest.approx(8.0)  # 6 mg/dL is 6 %
        assert glucose.percent_at(50) == pytest.approx(12.0)  # 6 mg/dL is 12 %

    def test_absolute_needs_positive_concentration(self) -> None:
        with pytest.raises(InsufficientDataError):
            AllowableError(absolute=0.3, source="CLIA").percent_at(0)

    def test_one_limit_required(self) -> None:
        with pytest.raises(ValueError, match="percent or absolute"):
            AllowableError(source="CLIA")


class TestSigma:
    def test_worked_example(self) -> None:
        # TEa 10 %, bias 2 %, CV 2 %: (10 - 2) / 2 = 4 sigma.
        assert sigma_metric(10, 2, 2) == pytest.approx(4.0)
        assert sigma_metric(10, -2, 2) == pytest.approx(4.0)  # |bias|

    def test_cv_must_be_positive(self) -> None:
        with pytest.raises(InsufficientDataError):
            sigma_metric(10, 0, 0)

    @pytest.mark.parametrize(
        ("sigma", "quality", "rules", "n"),
        [
            (6.0, SigmaQuality.WORLD_CLASS, (WestgardRule.R_1_3S,), 2),
            (
                5.99,
                SigmaQuality.EXCELLENT,
                (WestgardRule.R_1_3S, WestgardRule.R_2_2S, WestgardRule.R_R_4S),
                2,
            ),
            (
                4.0,
                SigmaQuality.GOOD,
                (
                    WestgardRule.R_1_3S,
                    WestgardRule.R_2_2S,
                    WestgardRule.R_R_4S,
                    WestgardRule.R_4_1S,
                ),
                4,
            ),
        ],
    )
    def test_westgard_sigma_rules(
        self, sigma: float, quality: SigmaQuality, rules: tuple[WestgardRule, ...], n: int
    ) -> None:
        design = recommend(sigma)
        assert design.quality is quality
        assert design.rules == rules
        assert design.controls_per_run == n
        assert not design.method_improvement_required

    def test_low_sigma_uses_full_multirule_and_flags_method(self) -> None:
        marginal = recommend(3.5)
        assert marginal.quality is SigmaQuality.MARGINAL
        assert WestgardRule.R_10_X in marginal.rules
        assert (marginal.controls_per_run, marginal.runs) == (4, 2)
        assert not marginal.method_improvement_required
        assert recommend(2.5).quality is SigmaQuality.POOR
        assert recommend(2.5).method_improvement_required
        assert recommend(1.0).quality is SigmaQuality.UNACCEPTABLE

    def test_assess_sigma_from_results(self) -> None:
        # Mean 101, SD sqrt(20/19) ~ 1.026: bias 1 %, CV ~ 1.016 %.
        values = [100.0, 102.0] * 10
        result = assess_sigma(TARGET, values, AllowableError(percent=8, absolute=6, source="CLIA"))
        expected_cv = math.sqrt(20 / 19) / 101 * 100
        assert result.bias_percent == pytest.approx(1.0)
        assert result.cv_percent == pytest.approx(expected_cv)
        assert result.tea_percent == pytest.approx(8.0)
        assert result.sigma == pytest.approx((8 - 1) / expected_cv)
        assert result.design.quality is SigmaQuality.WORLD_CLASS

    def test_assess_sigma_needs_enough_points(self) -> None:
        with pytest.raises(InsufficientDataError, match="at least 20"):
            assess_sigma(TARGET, [100.0, 101.0], AllowableError(percent=8, source="CLIA"))

    def test_assess_sigma_needs_spread(self) -> None:
        with pytest.raises(InsufficientDataError, match="CV is zero"):
            assess_sigma(TARGET, [100.0] * 20, AllowableError(percent=8, source="CLIA"))
