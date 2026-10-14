"""Westgard multirule quality control.

Rules are evaluated on z-scores, z = (x - target mean) / target SD, for the
current analytical run against the history of earlier runs for the same
analyte on the same instrument (all control levels).

* 1_2s  one result beyond +/-2 SD. Warning only. Evaluated on each current result.
* 1_3s  one result beyond +/-3 SD. Random error. Each current result.
* 2_2s  two consecutive results beyond 2 SD on the same side. Systematic error.
        Within the run (across levels) and across runs for each level.
* R_4s  one result beyond +2 SD and another beyond -2 SD in the same run.
        Random error. Within the run.
* 4_1s  four consecutive results beyond 1 SD on the same side. Systematic error.
        Across runs per level, and across levels and runs.
* 10_x  ten consecutive results on the same side of the mean. Systematic error.
        Across runs per level, and across levels and runs.

"Beyond" is strict: a result exactly at 2.0 SD does not exceed 2 SD. A z of
exactly zero breaks a 10_x streak. Every reported violation includes at
least one result from the current run, so an old pattern is never
re-reported against a later run. All rules are evaluated (1_2s is reported as
a warning, not used as a gate), which follows current practice of applying
the rejection rules directly rather than only after a 1_2s trigger.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from lab_quality_control_agents.domain.models import (
    FULL_MULTIRULE,
    ErrorType,
    RuleScope,
    RuleViolation,
    RunOutcome,
    Severity,
    WestgardRule,
)


@dataclass(frozen=True, slots=True)
class Observation:
    result_id: str
    level: int
    z: float
    observed_at: datetime
    run_id: str


def side(z: float, limit: float) -> int:
    """+1 when z exceeds +limit, -1 when below -limit, else 0."""
    if z > limit:
        return 1
    if z < -limit:
        return -1
    return 0


def _ordered(observations: Iterable[Observation]) -> list[Observation]:
    return sorted(observations, key=lambda o: (o.observed_at, o.run_id, o.level, o.result_id))


@dataclass(frozen=True)
class Evaluation:
    outcome: RunOutcome
    violations: tuple[RuleViolation, ...]


class WestgardEngine:
    """Applies a configured subset of the multirule procedure."""

    def __init__(self, rules: Iterable[WestgardRule] = FULL_MULTIRULE) -> None:
        self._rules = frozenset(rules)
        if not self._rules:
            raise ValueError("at least one rule is required")

    @property
    def rules(self) -> tuple[WestgardRule, ...]:
        return tuple(r for r in WestgardRule if r in self._rules)

    def evaluate(
        self, current: Sequence[Observation], history: Sequence[Observation] = ()
    ) -> Evaluation:
        if not current:
            raise ValueError("a run needs at least one observation")
        run = sorted(current, key=lambda o: (o.level, o.result_id))
        past = _ordered(history)
        found: list[RuleViolation] = []
        if WestgardRule.R_1_3S in self._rules:
            found.extend(self._single(run, WestgardRule.R_1_3S, 3.0, Severity.REJECT))
        if WestgardRule.R_1_2S in self._rules:
            found.extend(self._single(run, WestgardRule.R_1_2S, 2.0, Severity.WARNING))
        if WestgardRule.R_2_2S in self._rules:
            found.extend(self._two_two_s(run, past))
        if WestgardRule.R_R_4S in self._rules:
            found.extend(self._range_four_s(run))
        if WestgardRule.R_4_1S in self._rules:
            found.extend(self._consecutive(run, past, WestgardRule.R_4_1S, 4, 1.0))
        if WestgardRule.R_10_X in self._rules:
            found.extend(self._consecutive(run, past, WestgardRule.R_10_X, 10, 0.0))

        if any(v.severity is Severity.REJECT for v in found):
            outcome = RunOutcome.REJECT
        elif found:
            outcome = RunOutcome.WARNING
        else:
            outcome = RunOutcome.ACCEPT
        return Evaluation(outcome=outcome, violations=tuple(found))

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _single(
        run: Sequence[Observation], rule: WestgardRule, limit: float, severity: Severity
    ) -> list[RuleViolation]:
        violations = []
        for obs in run:
            sign = side(obs.z, limit)
            if sign:
                violations.append(
                    RuleViolation(
                        rule=rule,
                        severity=severity,
                        error_type=ErrorType.RANDOM,
                        scope=RuleScope.SINGLE,
                        direction="high" if sign > 0 else "low",
                        levels=(obs.level,),
                        result_ids=(obs.result_id,),
                        detail=f"level {obs.level} z={obs.z:+.2f} exceeds {limit:g} SD",
                    )
                )
        return violations

    @staticmethod
    def _two_two_s(run: Sequence[Observation], past: Sequence[Observation]) -> list[RuleViolation]:
        violations: list[RuleViolation] = []
        for sign in (1, -1):
            same = [o for o in run if side(o.z, 2.0) == sign]
            if len(same) >= 2:
                pair = same[:2]
                violations.append(
                    RuleViolation(
                        rule=WestgardRule.R_2_2S,
                        severity=Severity.REJECT,
                        error_type=ErrorType.SYSTEMATIC,
                        scope=RuleScope.WITHIN_RUN,
                        direction="high" if sign > 0 else "low",
                        levels=tuple(o.level for o in pair),
                        result_ids=tuple(o.result_id for o in pair),
                        detail="two results in the run beyond 2 SD on the same side",
                    )
                )
        for level in sorted({o.level for o in run}):
            sequence = [o for o in past if o.level == level] + [o for o in run if o.level == level]
            current_ids = {o.result_id for o in run}
            for index in range(1, len(sequence)):
                previous, latest = sequence[index - 1], sequence[index]
                if latest.result_id not in current_ids or previous.result_id in current_ids:
                    continue
                sign = side(latest.z, 2.0)
                if sign and side(previous.z, 2.0) == sign:
                    violations.append(
                        RuleViolation(
                            rule=WestgardRule.R_2_2S,
                            severity=Severity.REJECT,
                            error_type=ErrorType.SYSTEMATIC,
                            scope=RuleScope.ACROSS_RUNS,
                            direction="high" if sign > 0 else "low",
                            levels=(level,),
                            result_ids=(previous.result_id, latest.result_id),
                            detail=f"level {level} beyond 2 SD, same side, consecutive runs",
                        )
                    )
                    break
        return violations

    @staticmethod
    def _range_four_s(run: Sequence[Observation]) -> list[RuleViolation]:
        highs = [o for o in run if side(o.z, 2.0) == 1]
        lows = [o for o in run if side(o.z, 2.0) == -1]
        if not highs or not lows:
            return []
        high = max(highs, key=lambda o: o.z)
        low = min(lows, key=lambda o: o.z)
        return [
            RuleViolation(
                rule=WestgardRule.R_R_4S,
                severity=Severity.REJECT,
                error_type=ErrorType.RANDOM,
                scope=RuleScope.WITHIN_RUN,
                direction="both",
                levels=tuple(sorted({high.level, low.level})),
                result_ids=(high.result_id, low.result_id),
                detail=f"range {high.z - low.z:.2f} SD: one result above +2 SD, one below -2 SD",
            )
        ]

    @staticmethod
    def _consecutive(
        run: Sequence[Observation],
        past: Sequence[Observation],
        rule: WestgardRule,
        count: int,
        limit: float,
    ) -> list[RuleViolation]:
        current_ids = {o.result_id for o in run}
        violations: list[RuleViolation] = []
        within_level_windows: set[tuple[str, ...]] = set()
        for level in sorted({o.level for o in run}):
            sequence = [o for o in past if o.level == level] + [o for o in run if o.level == level]
            window = _streak(sequence, count, limit, current_ids)
            if window is not None:
                within_level_windows.add(tuple(o.result_id for o in window))
                violations.append(_streak_violation(rule, RuleScope.ACROSS_RUNS, window, limit))
        combined = list(past) + list(run)
        window = _streak(combined, count, limit, current_ids, require_multiple_levels=True)
        if window is not None and tuple(o.result_id for o in window) not in within_level_windows:
            violations.append(
                _streak_violation(rule, RuleScope.ACROSS_LEVELS_AND_RUNS, window, limit)
            )
        return violations


def _streak(
    sequence: Sequence[Observation],
    count: int,
    limit: float,
    current_ids: set[str],
    *,
    require_multiple_levels: bool = False,
) -> list[Observation] | None:
    for end in range(count - 1, len(sequence)):
        if sequence[end].result_id not in current_ids:
            continue
        window = list(sequence[end - count + 1 : end + 1])
        sides = {side(o.z, limit) for o in window}
        if len(sides) != 1 or 0 in sides:
            continue
        if require_multiple_levels and len({o.level for o in window}) < 2:
            continue
        return window
    return None


def _streak_violation(
    rule: WestgardRule, scope: RuleScope, window: Sequence[Observation], limit: float
) -> RuleViolation:
    sign = side(window[0].z, limit)
    where = "the mean" if limit == 0 else f"{limit:g} SD"
    return RuleViolation(
        rule=rule,
        severity=Severity.REJECT,
        error_type=ErrorType.SYSTEMATIC,
        scope=scope,
        direction="high" if sign > 0 else "low",
        levels=tuple(sorted({o.level for o in window})),
        result_ids=tuple(o.result_id for o in window),
        detail=(
            f"{len(window)} consecutive results {'above' if sign > 0 else 'below'} {where} "
            f"({scope.value.replace('_', ' ')})"
        ),
    )
