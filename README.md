# Lab Quality Control Agents

Agentic quality control for clinical laboratories. Control results arrive
from the LIS or instrument middleware (HL7 v2 ORU^R01, ASTM E1394, or JSON),
are scored against their targets and evaluated with the Westgard multirule
procedure. A rejected run places a hold on patient-result release for that
analyte and instrument, and a set of agents investigates the failure,
ranks root causes from instrument events and lot changes, and proposes a
CAPA. A supervisor signs the CAPA, staff perform the corrective action, the
verification agent applies verification run rules, and only a supervisor's
closing signature lifts the hold. No agent releases results on its own.

```
QC event ─▶ Westgard evaluation ─▶ Hold ─▶ Investigation agent ─▶ Root cause ranking
        ─▶ Corrective action agent (CAPA proposed) ─▶ Supervisor approval (e-signature)
        ─▶ Action performed (e-signature) ─▶ Verification agent ─▶ Supervisor closure ─▶ Hold lifted
```

## Capabilities

| Capability | Implementation |
|---|---|
| Westgard multirule | 1_2s (warning), 1_3s, 2_2s, R_4s, 4_1s, 10_x within runs, across runs per level and across levels and runs; every violation includes a current-run result; per-target rule selection |
| Levey-Jennings | Control limits (±1/2/3 SD), z-scored points with rule flags, observed mean/SD/CV and bias |
| Target establishment | Laboratory mean and SD from ≥20 baseline values (CLSI C24), sample SD |
| Sigma metrics | σ = (TEa% − \|bias%\|) / CV%, TEa from the analyte catalogue (greater of % and absolute), Westgard Sigma Rules recommendation (rules, N, R) |
| HL7 v2 | Full delimiter, repetition, component, subcomponent and escape handling; ORU^R01 QC profile (SPM-11 `Q`, INV control/reagent lots, OBX-18 instrument); HL7 ACK (AA/AE/AR) |
| ASTM | E1381 frames (checksum, frame sequence, ETB continuation) and E1394 H/P/O/R/C/L records; QC by H-12 `Q` or O-12 `Q` |
| Patient-data guard | Messages carrying patient identifiers (PID, ASTM P) or non-control specimens are refused; policy denies RESTRICTED data |
| Investigation agent | Deterministic, explainable root-cause scoring over calibration, maintenance, reagent/control lot changes, faults, cross-analyte failures and calibration age; patient-impact window from the last accepted run |
| Narrative | Optional model summary via the agentic-core `ModelRouter`, `ContentGuard` on input and output, citation grounding check; never on the decision path |
| CAPA | Proposed → approved → action completed → verified → closed (or rejected); 21 CFR Part 11 style signatures with record digest; separation of duties |
| Verification rules | Complete runs at all levels after the action, all results within ±2 SD, no rejection rule across verification runs, residual bias check for systematic errors |
| Identity | agent-identity-fabric credentials (introspection, `/v1/authorize`, jwt-bearer for the service's own agents) or enterprise OIDC; local ABAC policy otherwise, never allow-all |
| MCP | Official `mcp` SDK `MCPServer`, streamable HTTP at `/mcp/`, typed tool input and output schemas |
| A2A | Agent card at `/.well-known/agent-card.json`, JSON-RPC `message/send` at `/a2a` |
| Audit | agentic-core hash-chained, Ed25519-signed audit log for every decision and state change |
| Observability | Prometheus metrics, OpenTelemetry traces, JSON logs with correlation IDs, alert rules, Grafana dashboard |

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed.

```bash
docker compose up --build
curl -s localhost:8080/.well-known/agent-card.json | jq .skills[].id
```

The compose stack runs PostgreSQL, an OIDC provider standing in for the
enterprise IdP, and an OpenTelemetry collector.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs LABQC_TEST_DATABASE_URL)
```

Test suites: `tests/unit` (Westgard worked examples, statistics, HL7, ASTM,
investigation, CAPA, verification), `tests/security` (threat-model cases),
`tests/chaos` (dependency failures), `tests/performance` (latency budgets;
`locustfile.py` for load), `tests/e2e` (HTTP, MCP and A2A lifecycles and a
property-based OpenAPI contract test), `tests/integration` (PostgreSQL).

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/qc/hl7`, `/v1/qc/astm`, `/v1/qc/runs` | Interface engine, technologist, agent | Ingest and evaluate QC runs |
| `GET /v1/qc/runs`, `/v1/qc/runs/{id}` | Staff, auditors, agents | Run evaluations |
| `GET/PUT /v1/qc/targets`, `POST /v1/qc/targets/establish` | Supervisor (write) | Control targets |
| `GET /v1/qc/levey-jennings`, `/v1/qc/sigma` | Staff, agents | Statistics |
| `POST /v1/instrument-events` | Interface engine, technologist | Calibration, maintenance, lots, faults |
| `GET /v1/holds` | LIS, staff | Patient-result release holds |
| `POST/GET /v1/investigations` | Staff, agents | Root cause investigations |
| `POST/GET /v1/capas`, `/v1/capas/{id}/approve\|reject\|complete-action\|verify\|close` | Staff, supervisor | CAPA workflow |
| `/mcp/` | Agent runtimes | MCP tools |
| `/.well-known/agent-card.json`, `POST /a2a` | Other agents | A2A |
| `/healthz`, `/readyz`, `/metrics` | Platform | Operations |

Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/lab-quality-control-agents` (StatefulSet for per-replica audit chains, PDB, HPA, NetworkPolicy, ServiceMonitor) |
| Namespace and secret sync | `deployments/kubernetes` (restricted Pod Security, External Secrets) |
| Cloud infrastructure | `deployments/terraform/{azure,aws,gcp}`: managed PostgreSQL, secret store, WORM audit storage, workload identity. Azure is the reference deployment. |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md), [agent specification](docs/architecture/agent-specification.md), [ADRs](docs/architecture/adr/)
- [QC method reference](docs/architecture/qc-methods.md) (rules, statistics, interface profiles, worked examples)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [QC failure handling](docs/runbooks/qc-failure.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md)
- [Configuration reference](docs/operations/configuration.md)

Shared controls (policy engine, audit chain, model routing, content guard,
logging, telemetry) come from [agentic-core](https://github.com/kogunlowo123/agentic-core).

## License

Apache-2.0
