# QC method reference

## Scoring

Each control result is scored against the target of its control material
lot on that instrument: `z = (x − mean) / SD`. Results whose analyte, target,
lot or units do not match are not stored and are reported as unmatched.

## Westgard multirule

| Rule | Condition (strictly beyond the limit) | Error | Scopes |
|---|---|---|---|
| 1_2s | one result beyond ±2 SD | warning | each current result |
| 1_3s | one result beyond ±3 SD | random | each current result |
| 2_2s | two consecutive results beyond 2 SD, same side | systematic | within run across levels; across runs per level |
| R_4s | one result above +2 SD and another below −2 SD in the same run | random | within run |
| 4_1s | four consecutive results beyond 1 SD, same side | systematic | across runs per level; across levels and runs |
| 10_x | ten consecutive results on the same side of the mean | systematic | across runs per level; across levels and runs |

Outcome: `reject` if any rejection rule fires, `warning` if only 1_2s, else
`accept`. All rules are evaluated (ADR-0002). History is the results for the
analyte on the instrument since the last reset (CAPA closure or completed
corrective action), excluding runs that were rejected, ordered by time and
level. A violation is reported only if it includes a current-run result.

### Worked examples (verified in `tests/unit/test_westgard.py`)

Cholesterol control, mean 200 mg/dL, SD 4 mg/dL (2 SD limits 192/208, 3 SD
limits 188/212):

| Results (oldest → current) | Rules |
|---|---|
| 207 | accept |
| 208 | accept (exactly 2 SD is not beyond) |
| 209 | 1_2s warning (z = +2.25) |
| 213 | 1_3s reject (z = +3.25) and 1_2s |
| 209, 210 | 2_2s across runs |
| 191, 209 | no 2_2s (opposite sides) |
| level 1 209 and level 2 191 in one run | R_4s (range 4.5 SD) |
| 205, 206, 205, 207 | 4_1s across runs |
| 205, 204, 205, 207 | no 4_1s (204 is exactly 1 SD) |
| ten results from 201 to 203 | 10_x |
| nine above the mean, one at 200 | no 10_x |
| 205, 206, 209, 213 | 1_2s, 1_3s, 2_2s, 4_1s |

Two-level examples: level z-scores +2.2/+2.2 in one run give 2_2s within
run; prior run +1.2/+1.5 and current +1.1/+1.3 give 4_1s across levels and
runs (only two points per level); four prior runs at −0.4/−0.4 and current
−0.3/−0.6 give 10_x across levels while each level alone has five points.

## Levey-Jennings and targets

- Limits: mean ± 1, 2, 3 SD of the assigned target.
- Observed statistics: mean, sample SD (n − 1), CV%, bias% against the
  target mean, using results of the current control lot.
- Laboratory targets are established from at least 20 baseline values
  (`LABQC_TARGET_MIN_POINTS`), following CLSI C24; a zero-spread baseline is
  refused.

Worked example: 98, 100, 102, 101, 99 → mean 100, SD √(10/4) = 1.581, CV
1.581 %.

## Sigma metric

`σ = (TEa% − |bias%|) / CV%`. TEa comes from the analyte catalogue
(`configs/analytes.yaml`); when both a percentage and an absolute limit
exist, the greater applies at the target concentration (glucose ±6 mg/dL or
±8 %: 8 % at 100 mg/dL, 12 % at 50 mg/dL). At least 20 non-rejected results
of the current lot are required.

Worked example: TEa 10 %, bias 2 %, CV 2 % → σ = 4.

| σ | Quality | Recommended rules (Westgard Sigma Rules, 2 levels) | N × R |
|---|---|---|---|
| ≥ 6 | world class | 1_3s | 2 × 1 |
| 5 to < 6 | excellent | 1_3s / 2_2s / R_4s | 2 × 1 |
| 4 to < 5 | good | 1_3s / 2_2s / R_4s / 4_1s | 4 × 1 |
| 3 to < 4 | marginal | 1_2s warning + 1_3s / 2_2s / R_4s / 4_1s / 10_x | 4 × 2 |
| < 3 | poor / unacceptable | as marginal; method improvement required | 4 × 2 |

The published Sigma Rules use 8_x at low sigma; this implementation applies
10_x, the trend rule it implements, and documents the difference here.

## Root cause scoring

See `domain/investigation.py`. Priors depend on the error pattern (random,
systematic, one level affected by a systematic rule, trend rules). Evidence
weights: reagent lot change seen in results 1.0; recorded events weighted by
`exp(−hours/24)`, halved when the event preceded the last accepted run;
control lot change for a level 0.9; other analytes rejected on the
instrument 0.4 each (max 1.2); calibration older than the analyte's interval
0.5. Scores are normalised to probabilities and ranked; each hypothesis
carries corrective and preventive actions.

## Verification run rules

After the corrective action is signed as performed, verification uses only
later runs: at least one complete run (all target levels) for random error,
two for systematic error; every result within ±2 SD; no rejection rule
across the verification runs; for systematic error, each level's mean z
within ±1 SD. Too few runs is recorded as insufficient (not a failure) when
the verification is requested explicitly; automatic checks after each run
wait silently until enough runs exist.

## Interface profiles

HL7 v2 ORU^R01 and ASTM E1394 field usage is documented in the module
docstrings of `integrations/hl7.py` and `integrations/astm.py`. Both refuse
any message that carries patient identifiers or a non-control specimen.
Observations are keyed for idempotency by source, instrument, message
control ID and order/result sequence, so a re-delivered message is
recognised and not re-evaluated.
