# ADR-0006: One signed audit chain per replica

- Status: Accepted
- Date: 2026-10-14

## Context

The agentic-core audit chain is append-serialised per process. Sharing one
chain across replicas would need a distributed lock on every request, and
losing audit evidence on a pod restart is not acceptable for QC records.

## Decision

Run as a StatefulSet. Each replica writes its own chain to its own
persistent volume (`<pod>.jsonl`), signed with the audit key from Key
Vault. A log shipper copies chains to immutable storage provisioned by
Terraform (Azure immutable blobs, S3 Object Lock, GCS retention lock).
Events carry correlation IDs so a request can be followed across chains.
Application state lives in PostgreSQL, so replicas are otherwise
interchangeable.

## Consequences

- No cross-replica coordination on the hot path.
- Auditors verify N chains with `verify_chain`.
