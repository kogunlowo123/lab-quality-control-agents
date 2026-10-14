# Runbook: QC failure handling

## Normal cycle

1. A run is rejected. The ingest report lists the evaluation, the hold, the
   investigation and the proposed CAPA. The LIS stops releasing results for
   the analyte on that instrument.
2. The technologist opens the investigation (`GET /v1/investigations/{id}`):
   ranked hypotheses with evidence and the patient-impact window (from the
   last accepted run to the rejected run). Patient results in that window
   are reviewed in the LIS under the laboratory's lookback procedure.
3. The supervisor approves the proposed CAPA, or rejects it and proposes
   another hypothesis (`POST /v1/capas` with `hypothesis_id`); a different
   supervisor then approves that proposal.
4. Staff perform the corrective action and sign it
   (`POST /v1/capas/{id}/complete-action`).
5. Staff run controls at all levels. The verification agent checks each run;
   the CAPA becomes `verified` once the rules pass (one complete run for
   random error, two for systematic error).
6. A supervisor who did not perform the action closes the CAPA; the hold is
   released.

## Investigation missing

Alert `LabQcRejectedRunsWithoutInvestigation`. The ingest reports carry
`workflow_errors`. Causes: identity fabric unavailable or the agent
suspended (`workflow.investigate` failure audit events), policy change
removing `investigation:run`. The hold is in place regardless. Fix the cause,
then run `POST /v1/investigations` with the evaluation ID as a technologist.

## Long holds

Alert `LabQcHoldsOpenTooLong`. List open CAPAs (`GET /v1/capas?status=approved`,
`action_completed`). Common causes: verification runs missing a level,
verification failing (see `verifications[].reasons`), or the CAPA rejected
without a new proposal.

## Rejection rate

Alert `LabQcRejectionRateHigh`. Check the dashboard panel "Westgard
violations by rule": a burst of 1_3s across analytes on one instrument
points at the instrument; 10_x/4_1s on one analyte points at drift or
calibration. Review the sigma metric (`/v1/qc/sigma`) of methods with
frequent 1_2s warnings.

## Messages refused

Alert `LabQcMessagesRefused`. The response bodies (and HL7 AR
acknowledgements) state the reason. A message refused for patient
identifiers indicates a middleware routing error: stop the feed and correct
the routing; never relax the guard.

## Unmatched results

Alert `LabQcUnmatchedResults` by reason: `no target` or `lot mismatch` means
a new control lot is in use without targets (set them before reporting);
`unknown analyte` means a catalogue gap; `unit mismatch` means a middleware
mapping error.
