# Runbook: disaster recovery

| Component | Protection | RPO | RTO |
|---|---|---|---|
| PostgreSQL (Azure Flexible Server) | Zone-redundant HA, geo-redundant backups, 14-day PITR | ≤ 5 min (PITR) | < 1 h |
| Audit chains | Per-replica PVC + shipping to immutable blob storage (GZRS) | Shipping interval | Not needed for service restore |
| Secrets | Key Vault with soft delete and purge protection | 0 | Minutes |
| Configuration | Git (chart, policies, catalogue) | 0 | Minutes |

## Region loss

1. Restore the server to the paired region from geo-redundant backup
   (`az postgres flexible-server geo-restore`).
2. Update the Key Vault secret `lab-quality-control-agents-database-url`
   with the new host.
3. Install the chart in the standby cluster (`docs/runbooks/startup.md`
   steps 3-4).
4. Middleware re-sends messages queued since the outage; duplicates are
   recognised.
5. Before reopening autoverification, confirm the hold list was restored:
   holds placed after the recovery point are lost with the data, so review
   QC runs from the outage window in the middleware and re-send them.

## Point-in-time restore after corruption

Restore to a new server at a time before the event, compare
`qc_evaluations` and `capas` with the audit chains for the window (the
chains are the independent record), then switch the database URL.
