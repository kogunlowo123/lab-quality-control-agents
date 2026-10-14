# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Westgard multirule engine (1_2s warning, 1_3s, 2_2s, R_4s, 4_1s, 10_x)
  within runs, across runs per level and across levels and runs.
- Levey-Jennings charts, laboratory target establishment (CLSI C24), sigma
  metrics with Westgard Sigma Rules recommendations; analyte catalogue with
  LOINC mapping and CLIA allowable error.
- HL7 v2 ORU^R01 QC profile with acknowledgements; ASTM E1381 framing and
  E1394 records; patient-data guard on both.
- Result holds placed on rejection and released only by closing a verified
  CAPA.
- Investigation, corrective action and verification agents; deterministic
  root cause ranking; optional model narrative behind the agentic-core
  router and content guard with a grounding check.
- CAPA workflow with electronic signatures (signer, printed name, meaning,
  time, record digest) and separation of duties; verification run rules.
- agent-identity-fabric client (jwt-bearer, introspection, authorization);
  local ABAC policy; MCP server (streamable HTTP); A2A agent card and
  JSON-RPC `message/send`.
- Signed per-replica audit chains, Prometheus metrics, OpenTelemetry traces,
  alert rules and dashboard.
- Helm chart, Kubernetes namespace and secret sync, Terraform for Azure
  (reference), AWS and GCP.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos,
  performance, e2e, PostgreSQL integration), Helm/kubeconform, Terraform
  validate, Trivy, Syft, Grype, Semgrep, read-only container smoke test.
