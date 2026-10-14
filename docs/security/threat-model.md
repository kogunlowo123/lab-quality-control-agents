# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Result holds | Patient results released from a failing method |
| CAPA records and signatures | Unqualified or self-approved corrective actions; inspection findings |
| QC targets and rule selection | Failing runs accepted (targets widened, rules removed) |
| QC results and evaluations | Falsified QC history hides a failing method |
| Audit chains | Loss of accountability for every decision above |
| Audit signing key, database credentials | Forged evidence; data tampering |
| Model provider credentials | Spend, data egress |

## Trust boundaries

1. Instrument middleware / LIS → ingestion API (machine-generated, partially trusted content).
2. Staff, agents and services → REST, MCP and A2A (authenticated, authorized per action).
3. Service → identity fabric and Entra ID JWKS.
4. Service → model providers (prompt content leaves the trust boundary).
5. Service → PostgreSQL, Key Vault, audit storage.

## STRIDE

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| T1 | Patient-identifiable data sent to the QC service | Information disclosure | PID/ASTM P identifiers and non-control specimens refused with HL7 AR; RESTRICTED data denied by policy | `tests/security::test_t1`, `test_t5` |
| T2 | Agent or interface approves, closes or releases | Elevation of privilege | Signatures need a human (`require_approval`); no MCP signing tools; hold release only via closure | `test_t2` |
| T3 | Self-approval of a CAPA | Elevation / repudiation | Proposer ≠ approver, performer ≠ closer | `test_t3`, `tests/unit/test_service.py` |
| T4 | Forged, `alg: none`, expired or misdirected tokens | Spoofing | JWKS signature, single algorithm per token, issuer, audience, expiry | `test_t4` |
| T5 | Scope creep by an agent | Elevation | Local policy requires the action in the credential scopes; fabric decision cannot widen it | `test_t7`, `tests/unit/test_identity_and_access.py` |
| T6 | Signature through a delegation chain | Repudiation | `deny-delegated-signatures` rule | `test_t6` |
| T7 | Prompt injection through staff notes or messages | Tampering | Notes sent as EXTERNAL trust, isolated, injection-scored and blocked; narrative grounding check; model never decides | `tests/unit/test_narrative.py` |
| T8 | Audit tampering or credentials in audit | Repudiation / disclosure | Hash-chained, signed events; no tokens recorded | `test_t8` |
| T9 | Metrics cardinality attack via attacker-chosen labels | Denial of service | Only fixed label values (known skills, fixed outcomes) | `test_t9` |
| T10 | Oversized, extra-field or non-finite input | Tampering / DoS | 1 MB message cap, ≤ 12 results, `extra="forbid"`, finite numbers | `test_t10` |
| T11 | Identifier injection (path traversal, log forging) | Tampering | Strict code patterns on analyte, instrument, material, run IDs | `test_t11` |
| T12 | Malformed escapes or frames crash the parser | DoS | Strict HL7 escape and ASTM checksum/sequence validation, typed errors (no 5xx) | `test_t12`, `tests/unit/test_astm.py`, schemathesis |
| T13 | Replayed or concurrently re-delivered messages double-count | Tampering | Idempotency key per source/instrument/message/sequence; unique constraints | `tests/chaos::test_concurrent_redelivery_is_idempotent` |
| T14 | Audit write fails silently | Repudiation | Audit failure fails the request | `tests/chaos::test_audit_sink_failure_blocks_the_action` |
| T15 | Identity fabric, JWKS or model outage | DoS | Fail closed; holds and run outcomes do not depend on them | `tests/chaos` |
| T16 | DNS rebinding against the MCP endpoint | Spoofing | Host header allow-list | configuration, `LABQC_MCP_ALLOWED_HOSTS` |

## MITRE ATT&CK (Enterprise) techniques addressed

| Technique | Relevance | Control |
|---|---|---|
| T1078 Valid Accounts | Stolen staff token used to sign | Short-lived Entra ID tokens, role checks, SoD, signed audit |
| T1528 Steal Application Access Token | Agent credential theft | Fabric credentials introspected per call; suspension takes effect immediately |
| T1565.001 Stored Data Manipulation | Altering QC history | Append-only results, unique constraints, audit chain |
| T1070 Indicator Removal | Deleting audit evidence | Immutable storage, per-replica signed chains |
| T1499 Endpoint Denial of Service | Flooding ingestion | Input caps, HPA, ingress rate limits |
| T1190 Exploit Public-Facing Application | Parser abuse | Strict parsers, property-based contract test, SAST |

## MITRE ATLAS

| Technique | Control |
|---|---|
| AML.T0051 LLM Prompt Injection (indirect, via staff notes) | Untrusted-content isolation, injection blocking, model output not used for decisions |
| AML.T0048 External Harms (wrong corrective action) | Deterministic ranking; supervisor signature required |
| AML.T0057 LLM Data Leakage | No PHI in scope; CONFIDENTIAL routing only to approved providers; output secret blocking and PII redaction |
| AML.T0029 Denial of ML Service | Narrative optional; provider failure leaves it empty |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt Injection | ContentGuard on input (trust-aware), isolation of notes |
| LLM02 Sensitive Information Disclosure | No patient data; secret blocking and PII redaction on output |
| LLM03 Supply Chain | Pinned dependencies (`uv.lock`), SBOM, Grype, pip-audit |
| LLM04 Data and Model Poisoning | No training or fine-tuning; prompts built from structured facts |
| LLM05 Improper Output Handling | Output stored as text only; citations validated; never executed |
| LLM06 Excessive Agency | Agents limited to investigate/propose/verify; signatures and release are human |
| LLM07 System Prompt Leakage | System prompt holds no secrets |
| LLM08 Vector and Embedding Weaknesses | Not applicable (no retrieval store) |
| LLM09 Misinformation | Grounding check against hypothesis IDs; deterministic ranking is the record |
| LLM10 Unbounded Consumption | One request per rejected run, 400 output tokens, retries bounded |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 Broken Object Level Authorization | Every read and write authorized by policy with the resource identified |
| API2 Broken Authentication | JWKS verification, fabric introspection, no API keys |
| API3 Broken Object Property Level Authorization | `extra="forbid"`; signatures and statuses only set by the service |
| API4 Unrestricted Resource Consumption | Size caps, paging limits (≤ 500), HPA |
| API5 Broken Function Level Authorization | Distinct actions per function (`capa:approve` vs `capa:propose`) |
| API6 Unrestricted Access to Sensitive Business Flows | Release only via verified closure with SoD |
| API7 Server Side Request Forgery | No user-controlled outbound URLs |
| API8 Security Misconfiguration | Production refuses ephemeral keys; Helm `required`; restricted Pod Security; read-only root filesystem |
| API9 Improper Inventory Management | OpenAPI and JSON Schemas generated and checked in CI |
| API10 Unsafe Consumption of APIs | Fabric, JWKS and model responses validated; fail closed |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | LIS ignores the hold list and autoverifies | Medium | High | LIS team | Interface validation at go-live; `LabQcHoldsOpenTooLong` alert; documented in startup runbook | Medium |
| R2 | Wrong target values entered | Medium | High | QC supervisor | Supervisor-only, signed, audited target changes; establishment from ≥ 20 points | Low |
| R3 | Leading hypothesis wrong | Medium | Medium | QC supervisor | Supervisor selects any hypothesis; verification rules catch ineffective actions | Low |
| R4 | Instrument events not recorded | High | Medium | Laboratory operations | Middleware event feed; RCA still ranks by error pattern | Medium |
| R5 | PostgreSQL outage | Low | High | Platform | Zone-redundant HA, readiness probe, PITR | Low |
| R6 | Model provider sends data out of region | Low | Medium | AI governance | Region pinned in provider config; CONFIDENTIAL routing; narratives optional | Low |
| R7 | Analyte catalogue TEa outdated | Medium | Low | QC supervisor | Source cited per analyte; review annually against the current eCFR | Low |
