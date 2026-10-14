# ADR-0001: Deterministic QC decisions; the model only narrates

- Status: Accepted
- Date: 2026-10-14

## Context

Run acceptance, result holds and root cause ranking affect which patient
results reach clinicians. Regulators expect the laboratory to explain why a
run was rejected and why a corrective action was chosen. Language models can
help a supervisor read an investigation quickly but are not reproducible and
can state causes that the evidence does not support.

## Decision

- Westgard evaluation, holds, root cause scoring, verification and CAPA
  state are deterministic code with unit-tested worked examples.
- A model may write a short narrative of an investigation. The request goes
  through the agentic-core `ModelRouter` (CONFIDENTIAL classification, only
  approved providers) and `ContentGuard` (staff notes isolated as untrusted
  data, injection blocking, secret blocking, PII redaction).
- The narrative must cite hypotheses by ID and may cite only IDs that exist;
  otherwise it is discarded. Provider failure leaves it empty.
- Narratives are disabled unless `LABQC_PROVIDERS_FILE` is set.

## Consequences

- Identical inputs produce identical decisions; tests pin them.
- The service runs with no model provider at all; the narrative is a
  convenience, never on the decision path.
- Ranking quality depends on recorded instrument events; how often the
  supervisor keeps the leading hypothesis is tracked as a KPI to tune weights.
