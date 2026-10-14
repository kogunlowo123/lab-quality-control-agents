# Agent specification

Three agents run inside the service under one workload identity (the SPIFFE
ID in `LABQC_AGENT_ID`, registered in agent-identity-fabric). Each step is
authorized separately by action, so the fabric's policy and the local policy
can grant or withdraw each capability independently.

| Agent | Step in the flow | Actions it may request |
|---|---|---|
| Investigation agent | QC event → investigation → root cause analysis | `investigation:run`, `qc:read` |
| Corrective action agent | Root cause → corrective action proposal | `capa:propose`, `capa:read` |
| Verification agent | Corrective action → verification | `capa:verify`, `qc:read` |

## Role and mission

- **Purpose:** shorten the time from a rejected QC run to a documented,
  verified corrective action, without ever releasing results.
- **Business objective:** fewer patient results reported from failing
  methods, faster recovery of the analytical system, inspection-ready
  evidence for each QC failure.
- **Protected resources:** QC records, investigations, CAPA records, result
  holds, audit evidence.
- **Operating boundaries:** read QC data and instrument events, write
  investigations and CAPA proposals, record verification outcomes. Never
  modify targets, sign, close, release a hold, or touch patient results.
- **Delegated responsibility:** analytical judgement remains with qualified
  staff: the laboratory director and the technical supervisor they delegate
  QC review to (CLIA 42 CFR 493.1445 and 493.1451 for high-complexity
  testing); the agents prepare evidence.

## Delegated scope

| Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|
| Investigate any rejected run | Change QC targets or rule selection (`qc-target:write` → require_approval) | Moderate: wrong ranking delays the right fix | Supervisor reviews hypotheses and may choose another (`hypothesis_id`) |
| Propose one CAPA per open analyte/instrument | Approve, reject, close, complete actions (require_approval) | High if it could sign; prevented by policy | Proposal waits for a supervisor signature |
| Record verification outcomes | Release a hold (only closure by a supervisor releases it) | High if it could release; no code path exists | `LabQcHoldsOpenTooLong` alert |
| Write a narrative from ranked facts | Introduce causes or cite unknown hypotheses (discarded) | Low | Narrative omitted and counted (`labqc_narratives_total`) |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of `/v1/*`, `/mcp/`, `/a2a` | 99.9% over 30 days |
| SLO latency | p95 ingestion including evaluation | < 500 ms |
| SLO freshness | Rejected run has an investigation | within 5 minutes (alert `LabQcRejectedRunsWithoutInvestigation`) |
| SLI | `labqc:request_errors:ratio_5m`, latency histograms, outcome counters | Prometheus |
| KPI | Median time from rejection to CAPA closure | tracked from audit events (`qc.hold.place` → `qc.hold.release`) |
| KPI | Share of investigations whose leading hypothesis the supervisor approved unchanged | from CAPA `hypothesis_id` vs investigation `H1` |
| Completion | A rejection is complete when its CAPA is closed after passed verification and the hold is released | Enforced by the CAPA state machine |

## Permissions

| Mechanism | Use |
|---|---|
| RBAC | Entra ID app roles: `qc-technologist`, `qc-supervisor`, `qc-auditor`, `qc-interface`, `qc-agent` |
| ABAC | `policies/lab-qc.yaml` evaluated by the agentic-core PolicyEngine: subject kind, roles, scope match, delegation; resource classification |
| Agent identity | SPIFFE ID in agent-identity-fabric; credential obtained with the RFC 7523 jwt-bearer grant from a projected service-account token |
| OAuth 2.0 / OIDC | Staff and services: Entra ID tokens verified against the tenant JWKS; agents: fabric credentials introspected (RFC 7662) on every call |
| Workload identity | Azure Workload ID (federated credential) for the service's own cloud access; IRSA / GKE Workload Identity on other clouds |
| Separation of duties | Proposer ≠ approver; performer ≠ closer; signatures through a delegation chain are denied |

## Inputs and outputs

| Input | Schema | Validation |
|---|---|---|
| HL7 v2 ORU^R01 | `schemas/hl7-submission.request.schema.json` | Delimiters, escapes, MSH-9/10/12, PID must carry no identifiers, SPM-11 `Q`, OBX-2 `NM`, OBX-11 final, OBX-18 instrument, timestamps |
| ASTM E1381/E1394 | `schemas/astm-submission.request.schema.json` | Frame checksum, sequence, ETB continuation, H-12/O-12 `Q`, P without identifiers, R-13/R-14 |
| JSON run | `schemas/run-submission.request.schema.json` | Code patterns, finite values, ≤ 12 results, aware timestamps, unknown fields rejected |
| Instrument event | `schemas/instrument-event.request.schema.json` | Kind enum, free text ≤ 2000 characters (treated as untrusted in prompts) |

| Output | Schema | Provenance and audit metadata |
|---|---|---|
| Ingest report | `schemas/ingest-report.response.schema.json` | Message ID, idempotency (duplicates), unmatched reasons, evaluations, holds, workflow errors, HL7 ACK |
| Run evaluation | `schemas/run-evaluation.record.schema.json` | Rules applied, violations with scope and result IDs, history size |
| Investigation | `schemas/investigation.record.schema.json` | Hypotheses with evidence references (event IDs, result IDs), impact window, narrative provider, creator |
| CAPA | `schemas/capa.record.schema.json` | Signatures: signer, printed name, meaning, time, record digest; verification attempts |

Every decision (authorization, evaluation, investigation, proposal,
signature, verification, hold) is an audit event with actor, resource,
outcome, correlation ID and policy digest where applicable, hash-chained and
Ed25519-signed (agentic-core `AuditLog`).
