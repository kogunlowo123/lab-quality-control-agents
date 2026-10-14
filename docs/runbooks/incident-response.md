# Runbook: incident response

## Elevated 5xx

Alert `LabQcErrorBudgetBurnFast`.

1. `kubectl -n lab-qc logs statefulset/labqc-lab-quality-control-agents --since=15m | jq 'select(.level=="error")'`
2. `/readyz` failing on all pods: database. Check the Flexible Server status
   and failover events; the service recovers automatically when it returns.
3. Audit write errors (`OSError` in logs): the audit volume is full or
   read-only. Requests fail by design until evidence can be written; expand
   the PVC or ship and rotate the chain.
4. Middleware retries are safe: messages are idempotent.

## Replicas down

Alert `LabQcNotReady`. Check node pool, PDB and image pull. Middleware and
the LIS must queue during the outage; holds already placed stay in the
database.

## Suspected misuse

Indicators: `forbidden`/`approval_required` spikes in
`labqc_agent_interface_calls_total`, unexpected signers in CAPA records, or a
refused message carrying patient data.

1. Suspend the agent at agent-identity-fabric (`/v1/suspend`); introspection
   makes it take effect on the next call. For enterprise identities, disable
   the account or remove the app role in Entra ID.
2. Export and verify the audit chains for the window
   (`agentic_core.audit.verify_chain`), then search by actor and correlation ID.
3. Review CAPA signatures (`record_digest` must match the CAPA content) and
   any hold released in the window; re-place holds if a release is in doubt.
4. Record the incident and its CAPA in the laboratory quality system.

## Patient-data exposure

If any patient data reached the service (it should be refused), treat it as
a privacy incident: preserve the evidence, stop the feed, notify the privacy
officer, and purge under the laboratory's HIPAA incident procedure.
