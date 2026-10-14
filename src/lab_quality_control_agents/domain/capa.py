"""CAPA lifecycle and electronic signatures.

    proposed --approve (supervisor)--> approved --complete (staff)--> action_completed
        |                                                                |   ^
        +--reject (supervisor)--> rejected            verification passed|   |failed
                                                                         v   |
                                     closed <--close (supervisor)-- verified

Approval, rejection and closure are electronic signatures (21 CFR 11.50,
11.70): signer identity, printed name, time, meaning and a digest of the
record content that was signed. The proposer may not approve their own CAPA.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from agentic_core.domain.provenance import canonical_json

from lab_quality_control_agents.domain.errors import InvalidStateError
from lab_quality_control_agents.domain.models import (
    Capa,
    CapaStatus,
    ElectronicSignature,
    Hypothesis,
    Investigation,
    SignatureMeaning,
    VerificationResult,
)

TRANSITIONS: dict[CapaStatus, frozenset[CapaStatus]] = {
    CapaStatus.PROPOSED: frozenset({CapaStatus.APPROVED, CapaStatus.REJECTED}),
    CapaStatus.APPROVED: frozenset({CapaStatus.ACTION_COMPLETED}),
    CapaStatus.ACTION_COMPLETED: frozenset({CapaStatus.ACTION_COMPLETED, CapaStatus.VERIFIED}),
    CapaStatus.VERIFIED: frozenset({CapaStatus.CLOSED}),
    CapaStatus.REJECTED: frozenset(),
    CapaStatus.CLOSED: frozenset(),
}

OPEN_STATUSES = frozenset(
    {CapaStatus.PROPOSED, CapaStatus.APPROVED, CapaStatus.ACTION_COMPLETED, CapaStatus.VERIFIED}
)


def content_digest(capa: Capa) -> str:
    """Digest of what a signer attests to: the record minus its signature and bookkeeping."""
    body = capa.model_dump(mode="json", exclude={"signatures", "updated_at", "version", "status"})
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


def propose(
    *,
    capa_id: str,
    investigation: Investigation,
    hypothesis: Hypothesis,
    proposed_by: str,
    now: datetime,
    corrective_actions: tuple[str, ...] | None = None,
) -> Capa:
    return Capa(
        capa_id=capa_id,
        investigation_id=investigation.investigation_id,
        evaluation_id=investigation.evaluation_id,
        analyte=investigation.analyte,
        instrument_id=investigation.instrument_id,
        status=CapaStatus.PROPOSED,
        root_cause=hypothesis.category,
        hypothesis_id=hypothesis.hypothesis_id,
        error_types=investigation.error_types,
        corrective_actions=corrective_actions or hypothesis.corrective_actions,
        preventive_actions=hypothesis.preventive_actions,
        proposed_by=proposed_by,
        created_at=now,
        updated_at=now,
    )


def _move(capa: Capa, target: CapaStatus, now: datetime, **changes: object) -> Capa:
    if target not in TRANSITIONS[capa.status]:
        raise InvalidStateError(
            f"CAPA {capa.capa_id} is {capa.status.value}; cannot become {target}"
        )
    return capa.model_copy(
        update={"status": target, "updated_at": now, "version": capa.version + 1, **changes}
    )


def sign(
    capa: Capa,
    *,
    signer: str,
    signer_name: str,
    meaning: SignatureMeaning,
    statement: str,
    now: datetime,
) -> ElectronicSignature:
    return ElectronicSignature(
        signer=signer,
        signer_name=signer_name,
        meaning=meaning,
        signed_at=now,
        record_digest=content_digest(capa),
        statement=statement,
    )


def approve(capa: Capa, signature: ElectronicSignature, now: datetime) -> Capa:
    _require(signature, SignatureMeaning.APPROVAL)
    return _move(capa, CapaStatus.APPROVED, now, signatures=(*capa.signatures, signature))


def reject(capa: Capa, signature: ElectronicSignature, now: datetime) -> Capa:
    _require(signature, SignatureMeaning.REJECTION)
    return _move(capa, CapaStatus.REJECTED, now, signatures=(*capa.signatures, signature))


def complete_action(capa: Capa, signature: ElectronicSignature, note: str, now: datetime) -> Capa:
    _require(signature, SignatureMeaning.PERFORMED)
    if capa.status is not CapaStatus.APPROVED:
        raise InvalidStateError(f"CAPA {capa.capa_id} must be approved before action is recorded")
    return _move(
        capa,
        CapaStatus.ACTION_COMPLETED,
        now,
        signatures=(*capa.signatures, signature),
        action_completed_at=now,
        action_note=note,
    )


def record_verification(capa: Capa, result: VerificationResult, now: datetime) -> Capa:
    if capa.status is not CapaStatus.ACTION_COMPLETED:
        raise InvalidStateError(f"CAPA {capa.capa_id} has no completed action to verify")
    target = CapaStatus.VERIFIED if result.passed else CapaStatus.ACTION_COMPLETED
    return _move(capa, target, now, verifications=(*capa.verifications, result))


def close(capa: Capa, signature: ElectronicSignature, now: datetime) -> Capa:
    _require(signature, SignatureMeaning.CLOSURE)
    return _move(capa, CapaStatus.CLOSED, now, signatures=(*capa.signatures, signature))


def _require(signature: ElectronicSignature, meaning: SignatureMeaning) -> None:
    if signature.meaning is not meaning:
        raise InvalidStateError(
            f"signature meaning {signature.meaning} where {meaning} is required"
        )
