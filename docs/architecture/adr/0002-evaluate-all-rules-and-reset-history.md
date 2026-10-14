# ADR-0002: Evaluate every rule; reset history at corrective actions

- Status: Accepted
- Date: 2026-10-14

## Context

The original Westgard multirule procedure uses 1_2s as a gate: rejection
rules are checked only when a result exceeds 2 SD. That misses 4_1s and
10_x patterns built from results inside 2 SD. Current practice applies the
rejection rules directly and keeps 1_2s as a warning. Separately, a rule
history that spans a corrective action keeps re-triggering on the old
shifted results, and rejected runs (whose results were acted upon) distort
later evaluations.

## Decision

- Evaluate every configured rule on every run; 1_2s alone yields `warning`.
- Rule history for an analyte on an instrument starts at the later of the
  last hold release (CAPA closure) and the completion time of the open
  CAPA's corrective action. Results from rejected runs are excluded.
- Rules are selected per target (`rules`), so a supervisor can apply the
  sigma-based recommendation; the union of the rules of the materials in a
  run is applied.

## Consequences

- Trend rules detect drift without waiting for a 2 SD excursion.
- Verification runs are judged on their own, not against pre-failure data.
- Removing rules for a high-sigma method is an audited, signed target change
  (`qc-target:write`, supervisors only).
