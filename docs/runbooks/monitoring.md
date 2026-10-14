# Runbook: monitoring

| Signal | Source | Normal |
|---|---|---|
| `labqc:request_errors:ratio_5m` | Prometheus recording rule | < 0.1 % |
| p95 ingestion latency | `labqc_http_request_duration_seconds` (`/v1/qc/*`) | < 500 ms |
| `labqc_active_holds` | gauge | 0 most of the time; any value > 0 has an open CAPA |
| `labqc_run_outcomes_total` | counter by outcome | reject rate in line with the method sigma |
| `labqc_results_unmatched_total` | counter by reason | 0 outside lot changes |
| `labqc_narratives_total` | counter by outcome | `generated` when enabled; `unavailable` means provider issues |
| `labqc_agent_interface_calls_total` | counter by interface/operation/outcome | `unauthorized`/`forbidden` near 0 |

Dashboard: `configs/grafana/lab-quality-control-agents.json`. Alerts:
`configs/prometheus/alerts.yaml`.

## Latency

1. Check PostgreSQL CPU and connection count; the service uses SQLAlchemy
   pooling with pre-ping.
2. Check the history size of slow evaluations (`history_points` in run
   evaluations); it is capped at 200.
3. Check HPA status and pod CPU; scale limits are in `values.yaml`.

## Traces

Traces carry the `X-Correlation-ID` of the request. Audit events carry the
same correlation ID, so a trace, a log line and the signed audit evidence
for one request can be joined.
