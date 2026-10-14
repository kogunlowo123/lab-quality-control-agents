# Compliance control mapping

The evidence column names the artifact an inspector or auditor can examine.
Mapping a control does not by itself make a laboratory compliant; it shows
which requirement each feature supports.

## Laboratory quality (CLIA, CAP, ISO 15189)

| Requirement | Reference | How the service supports it | Evidence |
|---|---|---|---|
| Control procedures that detect immediate errors and changes in performance | CLIA 42 CFR 493.1256(b) | Westgard multirule including trend rules, sigma-based rule selection | Run evaluations, `qc-methods.md`, Westgard tests |
| At least two levels of control each day for quantitative tests | 493.1256(d)(3)(i) | Run completeness by level; verification requires all levels | Evaluations `levels`, verification results |
| Control results meet acceptability criteria before patient results are reported | 493.1256(f) | Rejected run places a hold read by the LIS | `qc_holds`, `/v1/holds`, `qc.hold.place` audit events |
| Corrective action when control results are unacceptable, including review of patient results since the last acceptable run | 493.1282(b)(2) | CAPA workflow; investigation records the impact window from the last accepted run | Investigation `impact_window_start/end`, CAPA records |
| Record retention for QC records (at least 2 years) | 493.1105(a)(3) | PostgreSQL with backups; immutable audit storage (7 years) | Terraform retention settings, DR runbook |
| Establishing mean and SD for new control lots | CAP Chemistry checklist QC requirements; CLSI C24 | Target establishment from ≥ 20 values; supervisor signature | `qc.target.set` audit events, targets |
| Internal quality control and ensuring validity of results | ISO 15189:2022 clause 7.3.7 | Same as above; Levey-Jennings and sigma views | Charts, sigma assessments |
| Nonconforming work and corrective action | ISO 15189:2022 clauses 7.5 and 8.7 | Hold, investigation, CAPA with verification of effectiveness | CAPA verification results |

## Electronic records and signatures (21 CFR Part 11), FDA CSA

| Requirement | Reference | Implementation | Evidence |
|---|---|---|---|
| Secure, computer-generated, time-stamped audit trails | 11.10(e) | agentic-core hash-chained, Ed25519-signed audit log per replica | Audit chains, `verify_chain` |
| Authority checks | 11.10(g) | Policy per action; supervisor-only signatures | `policies/lab-qc.yaml`, decision audit events |
| Operational system checks (enforced sequencing) | 11.10(f) | CAPA state machine; closure only after verification | `domain/capa.py`, tests |
| Signature manifestations: printed name, date/time, meaning | 11.50 | `ElectronicSignature` fields | CAPA `signatures` |
| Signature/record linking | 11.70 | SHA-256 digest of the signed CAPA content in each signature | `record_digest` |
| Unique signatures; components | 11.100, 11.200 | Enterprise IdP accounts (unique subject); IdP MFA policy | IdP configuration |
| Risk-based assurance (CSA) | FDA Computer Software Assurance guidance | High-risk functions (hold, signatures, Westgard) have scripted tests; CI run of the deployed tag as objective evidence | CI runs, test suites |

## Security and AI governance

| Control objective | HIPAA Security Rule | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | NIST SP 800-53 r5 | NIST AI RMF 1.0 | Evidence |
|---|---|---|---|---|---|---|
| Unique identity for people, services and agents | 164.312(a)(2)(i) | CC6.1 | 5.16 | IA-2, IA-9 | GOVERN 1.4 | IdP and fabric identities in audit events |
| Least privilege and separation of duties | 164.308(a)(4) | CC6.3 | 5.15, 5.3 | AC-5, AC-6 | MANAGE 2.4 | Policy rules, SoD tests |
| Human oversight of AI actions | — | CC8.1 | 5.37 | CA-7 | GOVERN 3.2, MANAGE 2.4 | require_approval decisions, signatures |
| Audit integrity | 164.312(b) | CC7.2 | 8.15 | AU-9, AU-10 | MEASURE 2.8 | Signed chains in immutable storage |
| Data minimisation (no PHI) | 164.502(b) | P4.1 | 5.34 | SI-12 | MAP 4.1 | Patient-data guard tests |
| Input validation | 164.312(c)(1) | CC8.1 | 8.28 | SI-10 | MEASURE 2.7 | Parsers, schemas, contract tests |
| AI output validity and transparency | — | PI1.3 | 5.37 | SA-11 | MEASURE 2.5, MEASURE 2.9 | Grounding check, narrative outcome metrics |
| Third-party model risk | 164.308(b) | CC9.2 | 5.19 | SR-3 | GOVERN 6.1 | Provider config (region, classification ceiling) |
| Monitoring and incident response | 164.308(a)(6) | CC7.3 | 5.24 | IR-4, SI-4 | MANAGE 4.1 | Alert rules, runbooks |
| Availability and recovery | 164.308(a)(7) | A1.2 | 8.14 | CP-9, CP-10 | — | HA database, DR runbook |
| Change control | 164.308(a)(8) | CC8.1 | 8.32 | CM-3 | GOVERN 1.4 | Git history, CODEOWNERS, signed releases |

HIPAA is listed because the service runs inside a covered entity's network;
by design it stores no PHI. HITRUST CSF inherits from the ISO 27001 and
NIST 800-53 rows.
