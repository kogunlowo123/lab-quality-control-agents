"""CAPA lifecycle, electronic signatures and verification run rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab_quality_control_agents.domain import capa as capa_rules
from lab_quality_control_agents.domain.catalog import AnalyteCatalog
from lab_quality_control_agents.domain.errors import InvalidStateError
from lab_quality_control_agents.domain.models import (
    CapaStatus,
    CauseCategory,
    ErrorType,
    Evidence,
    Hypothesis,
    Investigation,
    QcResult,
    ResultSource,
    SignatureMeaning,
    VerificationResult,
    WestgardRule,
)
from lab_quality_control_agents.domain.verification import VerificationPolicy, verify
from tests.conftest import ROOT

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

HYPOTHESIS = Hypothesis(
    hypothesis_id="H1",
    category=CauseCategory.CALIBRATION,
    probability=0.7,
    score=1.2,
    evidence=(Evidence(kind="event", description="calibration 2 h before", weight=0.8),),
    corrective_actions=("Recalibrate", "Repeat QC"),
    preventive_actions=("Review calibration frequency",),
)
INVESTIGATION = Investigation(
    investigation_id="inv-1",
    evaluation_id="ev-1",
    analyte="GLU",
    instrument_id="CHEM-01",
    run_id="run-9",
    error_types=(ErrorType.SYSTEMATIC,),
    hypotheses=(HYPOTHESIS,),
    other_analytes_rejected=(),
    impact_window_start=NOW - timedelta(hours=8),
    impact_window_end=NOW,
    created_at=NOW,
    created_by="agent",
)


def proposed() -> capa_rules.Capa:
    return capa_rules.propose(
        capa_id="capa-1",
        investigation=INVESTIGATION,
        hypothesis=HYPOTHESIS,
        proposed_by="agent",
        now=NOW,
    )


def signature(
    capa: capa_rules.Capa, meaning: SignatureMeaning, signer: str = "sam"
) -> capa_rules.ElectronicSignature:
    return capa_rules.sign(
        capa,
        signer=signer,
        signer_name=signer.title(),
        meaning=meaning,
        statement="reviewed and signed",
        now=NOW,
    )


def passed(ok: bool = True) -> VerificationResult:
    return VerificationResult(
        passed=ok,
        sufficient_data=True,
        run_ids=("v1",),
        reasons=("ok",),
        evaluated_at=NOW,
        mean_z_by_level={1: 0.1},
    )


class TestCapaLifecycle:
    def test_full_path(self) -> None:
        capa = proposed()
        assert capa.status is CapaStatus.PROPOSED
        assert capa.corrective_actions == ("Recalibrate", "Repeat QC")
        approved = capa_rules.approve(capa, signature(capa, SignatureMeaning.APPROVAL), NOW)
        assert approved.status is CapaStatus.APPROVED
        assert approved.version == 2
        done = capa_rules.complete_action(
            approved, signature(approved, SignatureMeaning.PERFORMED, "tess"), "Recalibrated", NOW
        )
        assert done.action_completed_at == NOW
        failed = capa_rules.record_verification(done, passed(False), NOW)
        assert failed.status is CapaStatus.ACTION_COMPLETED
        verified = capa_rules.record_verification(failed, passed(True), NOW)
        assert verified.status is CapaStatus.VERIFIED
        assert len(verified.verifications) == 2
        closed = capa_rules.close(verified, signature(verified, SignatureMeaning.CLOSURE), NOW)
        assert closed.status is CapaStatus.CLOSED
        assert closed.signed(SignatureMeaning.PERFORMED) is not None
        assert closed.signed(SignatureMeaning.REJECTION) is None
        assert [s.meaning for s in closed.signatures] == [
            SignatureMeaning.APPROVAL,
            SignatureMeaning.PERFORMED,
            SignatureMeaning.CLOSURE,
        ]

    def test_rejection_is_terminal(self) -> None:
        capa = proposed()
        rejected = capa_rules.reject(capa, signature(capa, SignatureMeaning.REJECTION), NOW)
        assert rejected.status is CapaStatus.REJECTED
        with pytest.raises(InvalidStateError):
            capa_rules.approve(rejected, signature(rejected, SignatureMeaning.APPROVAL), NOW)

    def test_cannot_skip_approval(self) -> None:
        capa = proposed()
        with pytest.raises(InvalidStateError, match="must be approved"):
            capa_rules.complete_action(capa, signature(capa, SignatureMeaning.PERFORMED), "x", NOW)
        with pytest.raises(InvalidStateError, match="no completed action"):
            capa_rules.record_verification(capa, passed(), NOW)
        with pytest.raises(InvalidStateError, match="cannot become"):
            capa_rules.close(capa, signature(capa, SignatureMeaning.CLOSURE), NOW)

    def test_signature_meaning_must_match(self) -> None:
        capa = proposed()
        with pytest.raises(InvalidStateError, match="meaning"):
            capa_rules.approve(capa, signature(capa, SignatureMeaning.CLOSURE), NOW)

    def test_digest_binds_content_not_status(self) -> None:
        capa = proposed()
        approved = capa_rules.approve(capa, signature(capa, SignatureMeaning.APPROVAL), NOW)
        assert capa_rules.content_digest(capa) == capa_rules.content_digest(approved)
        altered = capa.model_copy(update={"corrective_actions": ("Do nothing",)})
        assert capa_rules.content_digest(altered) != capa_rules.content_digest(capa)
        assert approved.signatures[0].record_digest.startswith("sha256:")


def results(run_id: str, hours: float, z1: float, z2: float | None) -> list[QcResult]:
    out = []
    for level, z in ((1, z1), (2, z2)):
        if z is None:
            continue
        out.append(
            QcResult(
                result_id=f"{run_id}-{level}",
                source_key=f"{run_id}-{level}",
                source=ResultSource.API,
                analyte="GLU",
                instrument_id="CHEM-01",
                material_id=f"GLU-L{level}",
                control_lot="L",
                level=level,
                run_id=run_id,
                value=100 + z,
                units="mg/dL",
                z=z,
                observed_at=NOW + timedelta(hours=hours),
                received_at=NOW,
            )
        )
    return out


def check(found: list[QcResult], errors: tuple[ErrorType, ...]) -> VerificationResult:
    return verify(
        results=found,
        required_levels=frozenset({1, 2}),
        error_types=errors,
        rules=tuple(WestgardRule),
        policy=VerificationPolicy(),
        now=NOW,
    )


class TestVerificationRules:
    def test_random_error_needs_one_clean_run(self) -> None:
        outcome = check(results("v1", 1, 0.4, -0.6), (ErrorType.RANDOM,))
        assert outcome.passed
        assert outcome.sufficient_data
        assert outcome.run_ids == ("v1",)
        assert outcome.mean_z_by_level == {1: 0.4, 2: -0.6}

    def test_incomplete_runs_do_not_count(self) -> None:
        outcome = check(results("v1", 1, 0.4, None), (ErrorType.RANDOM,))
        assert not outcome.passed
        assert not outcome.sufficient_data
        assert "0 complete verification run(s); 1 required" in outcome.reasons[0]

    def test_systematic_error_needs_two_runs(self) -> None:
        one = check(results("v1", 1, 0.2, 0.1), (ErrorType.SYSTEMATIC,))
        assert not one.sufficient_data
        two = check(
            results("v1", 1, 0.2, 0.1) + results("v2", 2, -0.3, 0.4), (ErrorType.SYSTEMATIC,)
        )
        assert two.passed
        assert two.run_ids == ("v1", "v2")

    def test_result_beyond_2sd_fails(self) -> None:
        outcome = check(results("v1", 1, 2.3, 0.0), (ErrorType.RANDOM,))
        assert not outcome.passed
        assert any("outside +/-2 SD" in r for r in outcome.reasons)

    def test_residual_bias_fails_systematic_verification(self) -> None:
        found = results("v1", 1, 1.5, 0.2) + results("v2", 2, 1.4, 0.3)
        outcome = check(found, (ErrorType.SYSTEMATIC,))
        assert not outcome.passed
        assert any("still biased" in r for r in outcome.reasons)
        # The same values would pass for a random error: bias is not checked.
        assert check(found[:2], (ErrorType.RANDOM,)).passed

    def test_rejection_rule_across_verification_runs_fails(self) -> None:
        # Within limits individually (|z| <= 2) except one; 4_1s across both runs.
        found = results("v1", 1, 1.6, 1.2) + results("v2", 2, 1.3, 1.1)
        outcome = check(found, (ErrorType.SYSTEMATIC,))
        assert not outcome.passed
        assert any("4_1s" in r for r in outcome.reasons)

    def test_only_needed_runs_are_used(self) -> None:
        found = results("v1", 1, 0.1, 0.1) + results("v2", 2, 3.5, 3.5)
        assert check(found, (ErrorType.RANDOM,)).passed

    def test_policy_with_only_warning_rule_still_checks_1_3s(self) -> None:
        outcome = verify(
            results=results("v1", 1, 0.1, 0.1),
            required_levels=frozenset({1, 2}),
            error_types=(ErrorType.RANDOM,),
            rules=(WestgardRule.R_1_2S,),
            policy=VerificationPolicy(),
            now=NOW,
        )
        assert outcome.passed


class TestCatalog:
    def test_repository_catalog_loads_and_resolves(self) -> None:
        catalog = AnalyteCatalog.from_file(ROOT / "configs" / "analytes.yaml")
        glucose = catalog.resolve("2345-7", "LN")
        assert glucose is not None
        assert glucose.code == "GLU"
        assert catalog.resolve("gluc") is glucose
        assert catalog.resolve("GLU") is glucose
        assert catalog.resolve("2345-7") is glucose
        assert catalog.resolve("9999-9", "LN") is None
        assert catalog.resolve("NOPE") is None
        assert catalog.get("K") is not None
        assert catalog.get("ZZ") is None
        assert glucose.calibration_interval == timedelta(days=14)

    def test_ambiguous_identifiers_are_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "analytes.yaml"
        path.write_text(
            "analytes:\n"
            "  - {code: GLU, name: Glucose, units: mg/dL, aliases: [X1],\n"
            "     tea: {percent: 8, source: CLIA}}\n"
            "  - {code: K, name: Potassium, units: mmol/L, aliases: [X1],\n"
            "     tea: {absolute: 0.3, source: CLIA}}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="maps to both"):
            AnalyteCatalog.from_file(path)
