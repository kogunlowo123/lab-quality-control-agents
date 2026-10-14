# ADR-0003: Result holds, electronic signatures and separation of duties

- Status: Accepted
- Date: 2026-10-14

## Context

Laboratory agents must never release diagnostic outcomes on model or agent
confidence. QC failures require corrective action documented and reviewed
by qualified staff (CLIA 42 CFR 493.1282), and laboratories that operate
under 21 CFR Part 11 need attributable electronic signatures.

## Decision

- A rejected run places a hold for the analyte on the instrument before any
  agent runs. The LIS reads `/v1/holds` to stop autoverification.
- Only closing a CAPA releases a hold, and closing requires status
  `verified`.
- Approve, reject, close and complete-action are electronic signatures:
  signer, printed name from the IdP, time, meaning and a SHA-256 digest of
  the CAPA content. Policy returns `require_approval` for any non-human
  principal and denies signatures exercised through a delegation chain.
- Separation of duties: the proposer cannot approve; the person who
  performed the action cannot close.

## Consequences

- No code path lets an agent release results; tests assert it for REST,
  MCP (no signing tools exist) and A2A.
- The full cycle needs at least two qualified people, which matches common
  QC review practice.
