# ADR-0005: Identity fabric and local policy, most restrictive wins

- Status: Accepted
- Date: 2026-10-14

## Context

Agents calling this service may hold agent-identity-fabric credentials, and
the service's own agents should act under a fabric identity so that a
suspension at the fabric stops them. Not every deployment has the fabric,
and the service must never fall back to allow-all.

## Decision

- Tokens whose issuer is the configured fabric are introspected at the
  fabric on every request; others are verified against the enterprise OIDC
  JWKS.
- For fabric principals both the fabric's `/v1/authorize` and the local
  `policies/lab-qc.yaml` are evaluated; deny beats require_approval beats
  allow. Everyone else is decided by the local policy (default deny).
- The service's agents obtain a fabric credential with the jwt-bearer grant
  from a projected service-account token, cached until one minute before
  expiry; without the fabric they act under `LABQC_AGENT_ID` with the
  configured scopes.
- Fabric unavailability fails closed: the agent step is recorded as failed
  and audited; the run outcome and the hold are unaffected.

## Consequences

- A fabric allow can never widen what the local policy permits.
- Each agent step costs one fabric authorization call; the credential
  exchange happens about once per ten minutes.
