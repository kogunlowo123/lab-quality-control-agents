# Runbook: rollback

The schema is additive within a minor version (`schema_version` table), so
application rollback within 0.x.y does not require database changes.

```bash
helm -n lab-qc history labqc
helm -n lab-qc rollback labqc <revision> --wait
kubectl -n lab-qc rollout status statefulset/labqc-lab-quality-control-agents
curl -sf "$LABQC/readyz"
```

Policy-only regressions: revert the commit touching `policies/lab-qc.yaml`,
release, and confirm the `policy_digest` recorded in new authorization audit
events matches the reverted file.

Analyte catalogue regressions (`configs/analytes.yaml`): revert and release;
results that were unmatched during the faulty window are listed in the
ingest reports and can be re-sent by the middleware (idempotent).

Every release keeps the previous image tag and chart version in GHCR.
