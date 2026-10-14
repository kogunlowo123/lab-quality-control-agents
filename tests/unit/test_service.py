"""Use cases end to end at the service layer (SQLite, in-process agents)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lab_quality_control_agents.application.commands import (
    ActionCompletion,
    AstmSubmission,
    EstablishTarget,
    Hl7Submission,
    InstrumentEventInput,
    ProposeCapa,
    RunResultInput,
    RunSubmission,
    SignatureInput,
)
from lab_quality_control_agents.application.service import QualityControlService
from lab_quality_control_agents.domain.errors import (
    ConflictError,
    ForbiddenError,
    InsufficientDataError,
    InvalidStateError,
    MessageFormatError,
    NotFoundError,
    SeparationOfDutiesError,
)
from lab_quality_control_agents.domain.models import (
    CapaStatus,
    CauseCategory,
    InstrumentEventKind,
    RunOutcome,
    WestgardRule,
)
from lab_quality_control_agents.integrations import astm
from lab_quality_control_agents.runtime.container import Container
from tests.conftest import (
    AGENT_ID,
    AUDITOR,
    INSTRUMENT,
    INTERFACE,
    START,
    SUPERVISOR,
    SUPERVISOR2,
    TECH,
    TECH2,
    FakeClock,
    run_submission,
    seed_targets,
    target_input,
    value,
)
from tests.unit.test_hl7 import group, message

SIGN = SignatureInput(statement="Reviewed the investigation and evidence; signing.")
BASELINE = [(0.3, -0.2), (-0.5, 0.4), (0.1, 0.6), (-0.2, -0.4)]


async def baseline(service: QualityControlService) -> None:
    await seed_targets(service)
    for hour, (z1, z2) in enumerate(BASELINE):
        report = await service.submit_run(INTERFACE, run_submission(f"base-{hour}", hour, z1, z2))
        assert report.evaluations[0].outcome is RunOutcome.ACCEPT


async def reject_shift(service: QualityControlService) -> str:
    """A 2_2s within-run shift right after a reagent lot change; returns the CAPA id."""
    report = await service.submit_run(
        INTERFACE, run_submission("shift-1", 5, 2.4, 2.3, reagent_lot="R-200")
    )
    (evaluation,) = report.evaluations
    assert evaluation.outcome is RunOutcome.REJECT
    assert report.holds_placed
    assert report.workflow_errors == ()
    (capa_id,) = report.capas_proposed
    return capa_id


async def test_full_capa_cycle_lifts_the_hold(
    service: QualityControlService, clock: FakeClock
) -> None:
    await baseline(service)
    capa_id = await reject_shift(service)

    (hold,) = await service.list_holds(TECH, active_only=True)
    assert (hold.analyte, hold.instrument_id, hold.capa_id) == ("GLU", INSTRUMENT, capa_id)

    capa = await service.get_capa(TECH, capa_id)
    assert capa.status is CapaStatus.PROPOSED
    assert capa.proposed_by == AGENT_ID
    assert capa.root_cause is CauseCategory.REAGENT_LOT
    investigation = await service.get_investigation(TECH, capa.investigation_id)
    assert investigation.leading.category is CauseCategory.REAGENT_LOT
    assert investigation.impact_window_start == START + timedelta(hours=3)
    assert investigation.narrative is None

    approved = await service.approve_capa(SUPERVISOR, capa_id, SIGN)
    assert approved.status is CapaStatus.APPROVED
    clock.current = START + timedelta(hours=5, minutes=30)
    done = await service.complete_action(
        SUPERVISOR2, capa_id, ActionCompletion(note="Recalibrated GLU with new reagent lot R-200.")
    )
    assert done.status is CapaStatus.ACTION_COMPLETED

    first = await service.submit_run(
        INTERFACE, run_submission("ver-1", 6, 0.2, -0.3, reagent_lot="R-200")
    )
    assert first.evaluations[0].history_points == 0  # history restarts after the action
    assert first.capas_verified == ()
    assert (await service.get_capa(TECH, capa_id)).verifications == ()
    second = await service.submit_run(
        INTERFACE, run_submission("ver-2", 7, -0.1, 0.4, reagent_lot="R-200")
    )
    assert second.capas_verified == (capa_id,)

    with pytest.raises(ForbiddenError):
        await service.close_capa(TECH, capa_id, SIGN)  # technologists cannot sign closure
    with pytest.raises(SeparationOfDutiesError):
        await service.close_capa(SUPERVISOR2, capa_id, SIGN)  # performed the action
    closed = await service.close_capa(SUPERVISOR, capa_id, SIGN)
    assert closed.status is CapaStatus.CLOSED
    assert await service.list_holds(TECH, active_only=True) == []
    (released,) = await service.list_holds(TECH, active_only=False)
    assert released.released_by == SUPERVISOR.subject
    assert await service.history_reset("GLU", INSTRUMENT) == clock.current


async def test_duplicates_are_recognised(service: QualityControlService) -> None:
    await seed_targets(service)
    submission = run_submission("dup-1", 0, 0.1, 0.1)
    first = await service.submit_run(INTERFACE, submission)
    again = await service.submit_run(INTERFACE, submission)
    assert (first.stored, again.stored, again.duplicates) == (2, 0, 2)
    assert again.evaluations == ()


async def test_unmatched_observations_are_reported(service: QualityControlService) -> None:
    await seed_targets(service)
    when = START
    run = RunSubmission(
        analyte="GLU",
        instrument_id=INSTRUMENT,
        run_id="mixed-1",
        results=(
            RunResultInput(material_id="GLU-L9", value=1, units="mg/dL", observed_at=when),
            RunResultInput(
                material_id="GLU-L1", control_lot="OTHER", value=1, units="mg/dL", observed_at=when
            ),
            RunResultInput(material_id="GLU-L2", value=300, units="mmol/L", observed_at=when),
            RunResultInput(
                material_id="GLU-L1", value=value(1, 0.5), units="mg/dL", observed_at=when
            ),
        ),
    )
    report = await service.submit_run(INTERFACE, run)
    reasons = sorted(u.reason.split(":")[0] for u in report.unmatched)
    assert reasons == ["lot mismatch", "no target", "unit mismatch"]
    assert report.stored == 1
    unknown = await service.submit_run(
        INTERFACE, run.model_copy(update={"analyte": "XYZ", "run_id": "mixed-2"})
    )
    assert {u.reason.split(":")[0] for u in unknown.unmatched} == {"unknown analyte"}


async def test_hl7_ingestion_and_acknowledgements(service: QualityControlService) -> None:
    await seed_targets(service)
    text = message(
        group("GLU-L1", "L1-2026A", "101.0", set_id="1", when="202610010815"),
        group("GLU-L2", "L2-2026A", "303.0", set_id="2", when="202610010815"),
    )
    report = await service.ingest_hl7(INTERFACE, Hl7Submission(message=text))
    assert report.stored == 2
    assert report.evaluations[0].outcome is RunOutcome.ACCEPT
    assert report.ack is not None
    assert "MSA|AA|MSG0001" in report.ack

    partial = message(group("GLU-L9", "X", "1.0"), control_id="MSG0002")
    partial_report = await service.ingest_hl7(INTERFACE, Hl7Submission(message=partial))
    assert partial_report.ack is not None
    assert "MSA|AE|MSG0002" in partial_report.ack

    with pytest.raises(MessageFormatError) as refused:
        await service.ingest_hl7(INTERFACE, Hl7Submission(message=text.replace("|Q", "|P")))
    assert refused.value.ack is not None
    assert "MSA|AR|MSG0001" in refused.value.ack


async def test_astm_ingestion(service: QualityControlService) -> None:
    await seed_targets(service)
    records = [
        "H|\\^&|RUN-7||CHEM-01|||||||Q|LIS2-A2",
        "O|1|GLU-L1^L1-2026A||^^^GLUC|R||||||Q",
        f"R|1|^^^GLUC|{value(1, 3.5)}|mg/dL||H||F||||20261001081500|CHEM-01",
        "O|2|GLU-L2^L2-2026A||^^^GLUC|R||||||Q",
        f"R|1|^^^GLUC|{value(2, 0.2)}|mg/dL||N||F||||20261001081500|CHEM-01",
        "L|1|N",
    ]
    report = await service.ingest_astm(INTERFACE, AstmSubmission(data=astm.build_frames(records)))
    (evaluation,) = report.evaluations
    assert evaluation.outcome is RunOutcome.REJECT
    assert {v.rule for v in evaluation.violations} >= {WestgardRule.R_1_3S}
    with pytest.raises(MessageFormatError):
        await service.ingest_astm(INTERFACE, AstmSubmission(data="H|\\^&\rL|1", framed=False))


async def test_policy_is_enforced(service: QualityControlService) -> None:
    await seed_targets(service)
    with pytest.raises(ForbiddenError):
        await service.submit_run(AUDITOR, run_submission("x", 0, 0, 0))
    with pytest.raises(ForbiddenError):
        await service.put_target(TECH, target_input(1))


async def test_separation_of_duties_on_human_proposals(
    service: QualityControlService, container: Container
) -> None:
    await baseline(service)
    capa_id = await reject_shift(service)
    rejected = await service.reject_capa(SUPERVISOR, capa_id, SIGN)
    assert rejected.status is CapaStatus.REJECTED
    assert await service.list_holds(TECH, active_only=True)  # hold stays

    capa = await service.propose_capa(
        SUPERVISOR,
        ProposeCapa(
            investigation_id=rejected.investigation_id,
            hypothesis_id="H2",
            corrective_actions=("Recalibrate and repeat QC at both levels",),
        ),
    )
    assert capa.proposed_by == SUPERVISOR.subject
    assert capa.corrective_actions == ("Recalibrate and repeat QC at both levels",)
    with pytest.raises(SeparationOfDutiesError):
        await service.approve_capa(SUPERVISOR, capa.capa_id, SIGN)
    with pytest.raises(ConflictError, match="nothing to verify"):
        await service.verify_capa(TECH, capa.capa_id)
    with pytest.raises(ConflictError, match="already open"):
        await service.propose_capa(TECH, ProposeCapa(investigation_id=rejected.investigation_id))
    approved = await service.approve_capa(SUPERVISOR2, capa.capa_id, SIGN)
    with pytest.raises(InvalidStateError, match="verification must pass"):
        await service.close_capa(SUPERVISOR2, approved.capa_id, SIGN)
    (hold,) = await container.service.list_holds(TECH, active_only=True)
    assert hold.capa_id == capa_id  # first CAPA stays linked


async def test_manual_verification_records_insufficient_attempts(
    service: QualityControlService, clock: FakeClock
) -> None:
    await baseline(service)
    capa_id = await reject_shift(service)
    await service.approve_capa(SUPERVISOR, capa_id, SIGN)
    clock.current = START + timedelta(hours=5, minutes=30)
    await service.complete_action(TECH2, capa_id, ActionCompletion(note="Replaced reagent pack."))
    capa = await service.verify_capa(TECH, capa_id)
    assert capa.status is CapaStatus.ACTION_COMPLETED
    assert capa.verifications[0].sufficient_data is False
    with pytest.raises(InvalidStateError):
        await service.approve_capa(SUPERVISOR, capa_id, SIGN)


async def test_lookup_errors(service: QualityControlService) -> None:
    for call in (
        service.get_evaluation(TECH, "missing"),
        service.get_investigation(TECH, "missing"),
        service.get_capa(TECH, "missing"),
        service.investigate(TECH, "missing"),
        service.propose_capa(TECH, ProposeCapa(investigation_id="missing")),
        service.verify_capa(TECH, "missing"),
        service.approve_capa(SUPERVISOR, "missing", SIGN),
        service.levey_jennings(TECH, "GLU", INSTRUMENT, "GLU-L1", limit=10),
        service.sigma(TECH, "GLU", INSTRUMENT, "GLU-L1"),
        service.put_target(SUPERVISOR, target_input(1, analyte="XYZ")),
    ):
        with pytest.raises(NotFoundError):
            await call


async def test_investigating_an_accepted_run_conflicts(service: QualityControlService) -> None:
    await seed_targets(service)
    report = await service.submit_run(INTERFACE, run_submission("ok-1", 0, 0.1, 0.1))
    with pytest.raises(ConflictError, match="not rejected"):
        await service.investigate(TECH, report.evaluations[0].evaluation_id)
    assert (
        await service.get_evaluation(TECH, report.evaluations[0].evaluation_id)
    ).run_id == "ok-1"


async def test_rejected_runs_are_excluded_from_history(service: QualityControlService) -> None:
    await baseline(service)
    await reject_shift(service)
    follow = await service.submit_run(INTERFACE, run_submission("after", 6, 2.2, 0.1))
    # 2.2 after the rejected 2.4 would be 2_2s if the rejected run were history.
    assert follow.evaluations[0].outcome is RunOutcome.WARNING
    assert len(follow.investigations) == 0


async def test_new_rejection_while_capa_open_investigates_without_new_capa(
    service: QualityControlService,
) -> None:
    await baseline(service)
    await reject_shift(service)
    again = await service.submit_run(INTERFACE, run_submission("again", 6, 3.4, 0.0))
    assert again.evaluations[0].outcome is RunOutcome.REJECT
    assert len(again.investigations) == 1
    assert again.capas_proposed == ()
    assert again.holds_placed == ()


async def test_events_feed_investigations(service: QualityControlService) -> None:
    await baseline(service)
    event = await service.record_event(
        TECH,
        InstrumentEventInput(
            instrument_id=INSTRUMENT,
            kind=InstrumentEventKind.CALIBRATION,
            occurred_at=START + timedelta(hours=4, minutes=30),
            analyte="GLU",
            lot="CAL-9",
            detail="Two-point calibration after reagent pack change",
        ),
    )
    report = await service.submit_run(INTERFACE, run_submission("cal-shift", 5, 2.4, 2.5))
    investigation = await service.get_investigation(TECH, report.investigations[0])
    assert investigation.leading.category is CauseCategory.CALIBRATION
    references = {e.reference for h in investigation.hypotheses for e in h.evidence}
    assert event.event_id in references
    again = await service.investigate(SUPERVISOR, report.evaluations[0].evaluation_id)
    assert again.created_by == SUPERVISOR.subject
    assert len(await service.list_investigations(TECH, limit=10, offset=0)) == 2


async def test_statistics_views(service: QualityControlService) -> None:
    await seed_targets(service)
    zs = [0.4, -0.6, 1.1, -0.9, 0.2, -0.1, 0.8, -1.2, 0.5, -0.3] * 2
    for i, z in enumerate(zs):
        await service.submit_run(INTERFACE, run_submission(f"s-{i}", i, z, -z))
    chart = await service.levey_jennings(TECH, "GLU", INSTRUMENT, "GLU-L1", limit=100)
    assert len(chart.points) == 20
    assert chart.limits.plus_2sd == 105.0
    assert chart.observed is not None
    assert chart.bias_percent == pytest.approx(0.0, abs=0.05)
    sigma = await service.sigma(TECH, "GLU", INSTRUMENT, "GLU-L1")
    assert sigma.n == 20
    assert sigma.tea_percent == pytest.approx(8.0)
    assert sigma.sigma > 3

    await service.submit_run(INTERFACE, run_submission("flagged", 30, 3.6, 0.0))
    chart = await service.levey_jennings(TECH, "GLU", INSTRUMENT, "GLU-L1", limit=100)
    assert WestgardRule.R_1_3S in chart.points[-1].rules_violated
    await service.put_target(SUPERVISOR, target_input(1, instrument="CHEM-02"))
    with pytest.raises(InsufficientDataError, match="at least 20"):
        await service.sigma(TECH, "GLU", "CHEM-02", "GLU-L1")


async def test_establish_target_from_baseline(service: QualityControlService) -> None:
    values = tuple(100.0 + (i % 5 - 2) * 0.8 for i in range(20))
    target = await service.establish_target(
        SUPERVISOR,
        EstablishTarget(
            analyte="GLU",
            instrument_id="CHEM-02",
            material_id="GLU-L1",
            control_lot="L1-2026B",
            level=1,
            units="mg/dL",
            values=values,
        ),
    )
    assert target.mean == pytest.approx(100.0)
    assert target.established_n == 20
    (listed,) = await service.list_targets(TECH, analyte="GLU", instrument_id="CHEM-02")
    assert listed == target
    with pytest.raises(InsufficientDataError):
        await service.establish_target(
            SUPERVISOR,
            EstablishTarget(
                analyte="GLU",
                instrument_id="CHEM-02",
                material_id="GLU-L1",
                control_lot="L1-2026B",
                level=1,
                units="mg/dL",
                values=values[:5],
            ),
        )
    with pytest.raises(NotFoundError):
        await service.establish_target(
            SUPERVISOR,
            EstablishTarget(
                analyte="NOPE",
                instrument_id="CHEM-02",
                material_id="GLU-L1",
                control_lot="L1",
                level=1,
                units="mg/dL",
                values=values,
            ),
        )


async def test_capa_listing(service: QualityControlService) -> None:
    await baseline(service)
    capa_id = await reject_shift(service)
    assert [
        c.capa_id for c in await service.list_capas(AUDITOR, status=None, limit=10, offset=0)
    ] == [capa_id]
    assert await service.list_capas(AUDITOR, status=CapaStatus.CLOSED, limit=10, offset=0) == []
    runs = await service.list_evaluations(
        AUDITOR,
        outcome=RunOutcome.REJECT,
        analyte="GLU",
        instrument_id=INSTRUMENT,
        limit=10,
        offset=0,
    )
    assert [r.run_id for r in runs] == ["shift-1"]
