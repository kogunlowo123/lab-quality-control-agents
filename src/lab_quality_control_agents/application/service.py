"""Laboratory QC use cases.

Ingestion path, for every message or run submitted:

1. parse (HL7 ORU^R01, ASTM E1394 or JSON) and refuse anything that is not a
   control specimen or that carries patient identifiers;
2. map interface codes to catalogue analytes, match each observation to its
   QC target (material, lot, units) and score it as a z-value;
3. store results idempotently (re-delivered messages are recognised);
4. evaluate each new run with the Westgard multirule engine against the
   history since the last CAPA reset, excluding rejected runs;
5. on rejection place a patient-result hold and hand the run to the agents
   (investigation, corrective action); on any run, let the verification
   agent check an open CAPA whose action is complete.

Supervisor actions (approve, reject, close) are electronic signatures and
enforce separation of duties. Closing a verified CAPA lifts the hold.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, tzinfo

from agentic_core.domain.classification import DataClassification
from agentic_core.domain.identifiers import new_id
from agentic_core.policy import Resource

from lab_quality_control_agents.application.commands import (
    ActionCompletion,
    AstmSubmission,
    EstablishTarget,
    Hl7Submission,
    IngestReport,
    InstrumentEventInput,
    ProposeCapa,
    RunSubmission,
    SignatureInput,
    TargetInput,
    UnmatchedObservation,
)
from lab_quality_control_agents.domain import capa as capa_rules
from lab_quality_control_agents.domain import statistics
from lab_quality_control_agents.domain.errors import (
    ConflictError,
    InvalidStateError,
    MessageFormatError,
    NotFoundError,
    SeparationOfDutiesError,
)
from lab_quality_control_agents.domain.models import (
    FULL_MULTIRULE,
    Capa,
    CapaStatus,
    InstrumentEvent,
    Investigation,
    QcHold,
    QcResult,
    QcTarget,
    ResultSource,
    RunEvaluation,
    RunOutcome,
    SignatureMeaning,
    TargetSource,
    WestgardRule,
)
from lab_quality_control_agents.domain.westgard import Observation, WestgardEngine
from lab_quality_control_agents.integrations import astm, hl7
from lab_quality_control_agents.integrations.hl7 import QcMessage, QcObservation
from lab_quality_control_agents.orchestration.agents import (
    AgentContext,
    QcWorkflow,
    capa_resource,
)
from lab_quality_control_agents.security.authn import Principal

QC_DATA = DataClassification.INTERNAL
HISTORY_LIMIT = 200


def _resource(kind: str, ident: str = "*", **attributes: str) -> Resource:
    return Resource(type=kind, id=ident, classification=QC_DATA, attributes=dict(attributes))


def _observation(result: QcResult) -> Observation:
    return Observation(
        result_id=result.result_id,
        level=result.level,
        z=result.z,
        observed_at=result.observed_at,
        run_id=result.run_id,
    )


class QualityControlService:
    def __init__(
        self,
        ctx: AgentContext,
        workflow: QcWorkflow,
        *,
        site_timezone: tzinfo,
        target_min_points: int = 20,
        sigma_min_points: int = 20,
    ) -> None:
        self._ctx = ctx
        self._repos = ctx.repos
        self._workflow = workflow
        self._tz = site_timezone
        self._target_min = target_min_points
        self._sigma_min = sigma_min_points

    @property
    def workflow(self) -> QcWorkflow:
        return self._workflow

    # ------------------------------------------------------------------ ingestion

    async def ingest_hl7(self, principal: Principal, body: Hl7Submission) -> IngestReport:
        await self._ctx.authorizer.require(principal, "qc:ingest", _resource("qc-result"))
        try:
            message = hl7.extract_qc(body.message, default_tz=self._tz)
        except MessageFormatError as exc:
            await self._refused(principal, ResultSource.HL7)
            exc.ack = hl7.build_ack(
                body.message,
                code="AR",
                note=exc.message,
                control_id=new_id()[:20],
                now=self._ctx.clock.now(),
            )
            raise
        report = await self._ingest(principal, ResultSource.HL7, message)
        code = "AA" if not report.unmatched else "AE"
        note = "accepted" if code == "AA" else f"{len(report.unmatched)} observation(s) unmatched"
        ack = hl7.build_ack(
            body.message,
            code=code,
            note=note,
            control_id=new_id()[:20],
            now=self._ctx.clock.now(),
        )
        return report.model_copy(update={"ack": ack})

    async def ingest_astm(self, principal: Principal, body: AstmSubmission) -> IngestReport:
        await self._ctx.authorizer.require(principal, "qc:ingest", _resource("qc-result"))
        try:
            message = astm.extract_qc(body.data, framed=body.framed, default_tz=self._tz)
        except MessageFormatError:
            await self._refused(principal, ResultSource.ASTM)
            raise
        return await self._ingest(principal, ResultSource.ASTM, message)

    async def submit_run(self, principal: Principal, run: RunSubmission) -> IngestReport:
        await self._ctx.authorizer.require(
            principal, "qc:ingest", _resource("qc-result", analyte=run.analyte)
        )
        message = QcMessage(
            control_id=run.run_id,
            sending_application="api",
            sending_facility="",
            version="api",
            observations=tuple(
                QcObservation(
                    set_id=r.material_id,
                    analyte_code=run.analyte,
                    coding_system=None,
                    value=r.value,
                    units=r.units,
                    observed_at=r.observed_at,
                    instrument_id=run.instrument_id,
                    material_id=r.material_id,
                    control_lot=r.control_lot,
                    reagent_lot=r.reagent_lot,
                )
                for r in run.results
            ),
        )
        return await self._ingest(principal, ResultSource.API, message)

    async def _refused(self, principal: Principal, source: ResultSource) -> None:
        self._ctx.metrics.messages_failed.labels(source.value).inc()
        await self._ctx.record(principal.subject, "qc.message.refuse", source.value, "failure", {})

    async def _match(
        self, source: ResultSource, message: QcMessage
    ) -> tuple[list[QcResult], list[UnmatchedObservation], dict[str, QcTarget]]:
        now = self._ctx.clock.now()
        matched: list[QcResult] = []
        unmatched: list[UnmatchedObservation] = []
        targets: dict[str, QcTarget] = {}
        for obs in message.observations:

            def miss(reason: str, obs: QcObservation = obs) -> None:
                unmatched.append(
                    UnmatchedObservation(
                        reference=obs.set_id,
                        analyte_code=obs.analyte_code,
                        instrument_id=obs.instrument_id,
                        material_id=obs.material_id,
                        reason=reason,
                    )
                )
                category = reason.split(":", maxsplit=1)[0]
                self._ctx.metrics.results_rejected.labels(source.value, category).inc()

            analyte = self._ctx.catalog.resolve(obs.analyte_code, obs.coding_system)
            if analyte is None:
                miss("unknown analyte: not in the analyte catalogue")
                continue
            target = await self._repos.targets.get(analyte.code, obs.instrument_id, obs.material_id)
            if target is None:
                miss("no target: no QC target for this material on this instrument")
                continue
            if obs.control_lot and obs.control_lot != target.control_lot:
                miss(f"lot mismatch: control lot {obs.control_lot} has no target")
                continue
            if obs.units.casefold() != target.units.casefold():
                miss(f"unit mismatch: {obs.units} differs from target units {target.units}")
                continue
            source_key = f"{source.value}:{obs.instrument_id}:{message.control_id}:{obs.set_id}"
            result_id = hashlib.sha256(source_key.encode()).hexdigest()[:32]
            targets[result_id] = target
            matched.append(
                QcResult(
                    result_id=result_id,
                    source_key=source_key,
                    source=source,
                    analyte=analyte.code,
                    instrument_id=obs.instrument_id,
                    material_id=obs.material_id,
                    control_lot=target.control_lot,
                    level=target.level,
                    run_id=message.control_id,
                    value=obs.value,
                    units=target.units,
                    z=round(target.z(obs.value), 6),
                    observed_at=obs.observed_at,
                    received_at=now,
                    reagent_lot=obs.reagent_lot,
                )
            )
        return matched, unmatched, targets

    async def _ingest(
        self, principal: Principal, source: ResultSource, message: QcMessage
    ) -> IngestReport:
        matched, unmatched, targets = await self._match(source, message)
        stored = await self._repos.results.add_new(matched)
        self._ctx.metrics.results_ingested.labels(source.value).inc(len(stored))

        runs: dict[tuple[str, str, str], list[QcResult]] = {}
        for result in stored:
            runs.setdefault((result.analyte, result.instrument_id, result.run_id), []).append(
                result
            )

        evaluations: list[RunEvaluation] = []
        holds: list[str] = []
        investigations: list[str] = []
        proposed: list[str] = []
        verified: list[str] = []
        errors: list[str] = []
        for (analyte, instrument_id, run_id), results in sorted(runs.items()):
            rules = {rule for r in results for rule in targets[r.result_id].rules}
            evaluation = await self._evaluate(
                principal, analyte, instrument_id, run_id, results, rules=rules
            )
            if evaluation is None:
                continue
            evaluations.append(evaluation)
            if evaluation.outcome is RunOutcome.REJECT:
                hold = await self._place_hold(principal, evaluation)
                if hold is not None:
                    holds.append(hold.hold_id)
                outcome = await self._workflow.on_rejected(evaluation)
                errors.extend(outcome.errors)
                if outcome.investigation_id:
                    investigations.append(outcome.investigation_id)
                if outcome.capa_id:
                    proposed.append(outcome.capa_id)
                    await self._link_hold(analyte, instrument_id, outcome.capa_id)
            check = await self._workflow.on_run(analyte, instrument_id)
            errors.extend(check.errors)
            if check.verified_capa_id:
                verified.append(check.verified_capa_id)

        await self._ctx.record(
            principal.subject,
            "qc.ingest",
            f"{source.value}:{message.control_id}",
            "success",
            {
                "stored": len(stored),
                "duplicates": len(matched) - len(stored),
                "unmatched": len(unmatched),
                "runs": [e.evaluation_id for e in evaluations],
            },
        )
        return IngestReport(
            source=source,
            message_id=message.control_id,
            stored=len(stored),
            duplicates=len(matched) - len(stored),
            unmatched=tuple(unmatched),
            skipped=message.skipped,
            evaluations=tuple(evaluations),
            holds_placed=tuple(holds),
            investigations=tuple(investigations),
            capas_proposed=tuple(proposed),
            capas_verified=tuple(verified),
            workflow_errors=tuple(errors),
        )

    async def history_reset(self, analyte: str, instrument_id: str) -> datetime | None:
        """Rule history restarts after a closed CAPA or a completed corrective action."""
        points: list[datetime] = []
        released = await self._repos.holds.last_released(analyte, instrument_id)
        if released is not None and released.released_at is not None:
            points.append(released.released_at)
        capa = await self._repos.capas.open_for(analyte, instrument_id)
        if capa is not None and capa.action_completed_at is not None:
            points.append(capa.action_completed_at)
        return max(points) if points else None

    async def _evaluate(
        self,
        principal: Principal,
        analyte: str,
        instrument_id: str,
        run_id: str,
        results: Sequence[QcResult],
        *,
        rules: set[WestgardRule],
    ) -> RunEvaluation | None:
        repos = self._repos
        if await repos.evaluations.find_run(analyte, instrument_id, run_id) is not None:
            return None
        ordered_rules = tuple(r for r in FULL_MULTIRULE if r in rules) or FULL_MULTIRULE
        run_start = min(r.observed_at for r in results)
        run_end = max(r.observed_at for r in results)
        reset = await self.history_reset(analyte, instrument_id)
        rejected_runs = {
            e.run_id
            for e in await repos.evaluations.list(
                outcome=RunOutcome.REJECT,
                analyte=analyte,
                instrument_id=instrument_id,
                since=reset,
                limit=HISTORY_LIMIT,
            )
        }
        history = [
            _observation(r)
            for r in await repos.results.query(
                analyte=analyte, instrument_id=instrument_id, since=reset, limit=HISTORY_LIMIT
            )
            if r.run_id != run_id and r.run_id not in rejected_runs and r.observed_at < run_start
        ]
        outcome = WestgardEngine(ordered_rules).evaluate(
            [_observation(r) for r in results], history
        )
        evaluation = RunEvaluation(
            evaluation_id=new_id(),
            analyte=analyte,
            instrument_id=instrument_id,
            run_id=run_id,
            outcome=outcome.outcome,
            violations=outcome.violations,
            rules_applied=ordered_rules,
            result_ids=tuple(r.result_id for r in sorted(results, key=lambda r: r.level)),
            levels=tuple(sorted({r.level for r in results})),
            run_observed_at=run_end,
            evaluated_at=self._ctx.clock.now(),
            history_points=len(history),
        )
        try:
            await repos.evaluations.add(evaluation)
        except ConflictError:
            return None
        metrics = self._ctx.metrics
        metrics.run_outcomes.labels(evaluation.outcome.value).inc()
        for violation in evaluation.violations:
            metrics.violations.labels(violation.rule.value).inc()
        await self._ctx.record(
            principal.subject,
            "qc.run.evaluate",
            f"qc-run:{evaluation.evaluation_id}",
            evaluation.outcome.value,
            {
                "analyte": analyte,
                "instrument_id": instrument_id,
                "run_id": run_id,
                "violations": [f"{v.rule.value}/{v.scope.value}" for v in evaluation.violations],
            },
        )
        return evaluation

    async def _place_hold(self, principal: Principal, evaluation: RunEvaluation) -> QcHold | None:
        if await self._repos.holds.active(evaluation.analyte, evaluation.instrument_id):
            return None
        hold = QcHold(
            hold_id=new_id(),
            analyte=evaluation.analyte,
            instrument_id=evaluation.instrument_id,
            evaluation_id=evaluation.evaluation_id,
            placed_at=self._ctx.clock.now(),
        )
        await self._repos.holds.add(hold)
        await self._ctx.record(
            principal.subject,
            "qc.hold.place",
            f"hold:{evaluation.analyte}@{evaluation.instrument_id}",
            "success",
            {"evaluation_id": evaluation.evaluation_id},
        )
        await self._refresh_hold_gauge()
        return hold

    async def _link_hold(self, analyte: str, instrument_id: str, capa_id: str) -> None:
        hold = await self._repos.holds.active(analyte, instrument_id)
        if hold is not None and hold.capa_id is None:
            await self._repos.holds.update(hold.model_copy(update={"capa_id": capa_id}))

    async def _refresh_hold_gauge(self) -> None:
        self._ctx.metrics.active_holds.set(len(await self._repos.holds.list(active_only=True)))

    # -------------------------------------------------------------------- targets

    async def put_target(self, principal: Principal, body: TargetInput) -> QcTarget:
        await self._ctx.authorizer.require(
            principal, "qc-target:write", _resource("qc-target", body.material_id)
        )
        if self._ctx.catalog.get(body.analyte) is None:
            raise NotFoundError(f"analyte {body.analyte} is not in the analyte catalogue")
        target = QcTarget(**body.model_dump(), effective_from=self._ctx.clock.now())
        await self._store_target(principal, target)
        return target

    async def establish_target(self, principal: Principal, body: EstablishTarget) -> QcTarget:
        await self._ctx.authorizer.require(
            principal, "qc-target:write", _resource("qc-target", body.material_id)
        )
        if self._ctx.catalog.get(body.analyte) is None:
            raise NotFoundError(f"analyte {body.analyte} is not in the analyte catalogue")
        stats = statistics.establish(body.values, min_n=self._target_min)
        target = QcTarget(
            analyte=body.analyte,
            instrument_id=body.instrument_id,
            material_id=body.material_id,
            control_lot=body.control_lot,
            level=body.level,
            mean=round(stats.mean, 6),
            sd=round(stats.sd, 6),
            units=body.units,
            source=TargetSource.LABORATORY,
            established_n=stats.n,
            rules=body.rules,
            effective_from=self._ctx.clock.now(),
        )
        await self._store_target(principal, target)
        return target

    async def _store_target(self, principal: Principal, target: QcTarget) -> None:
        await self._repos.targets.put(target)
        await self._ctx.record(
            principal.subject,
            "qc.target.set",
            f"qc-target:{target.analyte}/{target.instrument_id}/{target.material_id}",
            "success",
            {
                "lot": target.control_lot,
                "mean": target.mean,
                "sd": target.sd,
                "source": target.source.value,
                "rules": [r.value for r in target.rules],
            },
        )

    async def list_targets(
        self, principal: Principal, *, analyte: str | None, instrument_id: str | None
    ) -> Sequence[QcTarget]:
        await self._ctx.authorizer.require(principal, "qc:read", _resource("qc-target"))
        return await self._repos.targets.list(analyte=analyte, instrument_id=instrument_id)

    # --------------------------------------------------------------------- events

    async def record_event(
        self, principal: Principal, body: InstrumentEventInput
    ) -> InstrumentEvent:
        await self._ctx.authorizer.require(
            principal, "instrument-event:write", _resource("instrument", body.instrument_id)
        )
        now = self._ctx.clock.now()
        event = InstrumentEvent(
            event_id=new_id(), recorded_by=principal.subject, recorded_at=now, **body.model_dump()
        )
        await self._repos.events.add(event)
        await self._ctx.record(
            principal.subject,
            "instrument.event.record",
            f"instrument:{event.instrument_id}",
            "success",
            {"event_id": event.event_id, "kind": event.kind.value, "analyte": event.analyte},
        )
        return event

    # ---------------------------------------------------------------------- reads

    async def list_evaluations(
        self,
        principal: Principal,
        *,
        outcome: RunOutcome | None,
        analyte: str | None,
        instrument_id: str | None,
        limit: int,
        offset: int,
    ) -> Sequence[RunEvaluation]:
        await self._ctx.authorizer.require(principal, "qc:read", _resource("qc-run"))
        return await self._repos.evaluations.list(
            outcome=outcome,
            analyte=analyte,
            instrument_id=instrument_id,
            limit=limit,
            offset=offset,
        )

    async def get_evaluation(self, principal: Principal, evaluation_id: str) -> RunEvaluation:
        await self._ctx.authorizer.require(principal, "qc:read", _resource("qc-run", evaluation_id))
        evaluation = await self._repos.evaluations.get(evaluation_id)
        if evaluation is None:
            raise NotFoundError(f"evaluation {evaluation_id} not found")
        return evaluation

    async def levey_jennings(
        self,
        principal: Principal,
        analyte: str,
        instrument_id: str,
        material_id: str,
        *,
        limit: int,
    ) -> statistics.LeveyJenningsChart:
        await self._ctx.authorizer.require(
            principal, "qc:read", _resource("qc-target", material_id)
        )
        target = await self._target(analyte, instrument_id, material_id)
        results = await self._repos.results.query(
            analyte=analyte, instrument_id=instrument_id, material_id=material_id, limit=limit
        )
        flags: dict[str, list[WestgardRule]] = {}
        if results:
            for evaluation in await self._repos.evaluations.list(
                analyte=analyte,
                instrument_id=instrument_id,
                since=results[0].observed_at,
                limit=max(limit, 1) * 2,
            ):
                for violation in evaluation.violations:
                    for rid in violation.result_ids:
                        flags.setdefault(rid, []).append(violation.rule)
        current_lot = [r for r in results if r.control_lot == target.control_lot]
        values = [r.value for r in current_lot]
        observed = statistics.describe(values) if len(values) >= 2 else None
        return statistics.LeveyJenningsChart(
            analyte=analyte,
            instrument_id=instrument_id,
            material_id=material_id,
            control_lot=target.control_lot,
            level=target.level,
            units=target.units,
            limits=statistics.ControlLimits.of(target),
            points=tuple(
                statistics.LeveyJenningsPoint(
                    result_id=r.result_id,
                    run_id=r.run_id,
                    observed_at=r.observed_at,
                    value=r.value,
                    z=r.z,
                    rules_violated=tuple(dict.fromkeys(flags.get(r.result_id, []))),
                )
                for r in current_lot
            ),
            observed=observed,
            bias_percent=(
                statistics.bias_percent(observed.mean, target.mean)
                if observed is not None and target.mean != 0
                else None
            ),
        )

    async def sigma(
        self, principal: Principal, analyte: str, instrument_id: str, material_id: str
    ) -> statistics.SigmaAssessment:
        await self._ctx.authorizer.require(
            principal, "qc:read", _resource("qc-target", material_id)
        )
        target = await self._target(analyte, instrument_id, material_id)
        definition = self._ctx.catalog.get(analyte)
        if definition is None:
            raise NotFoundError(f"analyte {analyte} is not in the analyte catalogue")
        rejected = {
            e.run_id
            for e in await self._repos.evaluations.list(
                outcome=RunOutcome.REJECT,
                analyte=analyte,
                instrument_id=instrument_id,
                limit=1000,
            )
        }
        results = await self._repos.results.query(
            analyte=analyte, instrument_id=instrument_id, material_id=material_id, limit=1000
        )
        values = [
            r.value
            for r in results
            if r.control_lot == target.control_lot and r.run_id not in rejected
        ]
        return statistics.assess_sigma(target, values, definition.tea, min_n=self._sigma_min)

    async def _target(self, analyte: str, instrument_id: str, material_id: str) -> QcTarget:
        target = await self._repos.targets.get(analyte, instrument_id, material_id)
        if target is None:
            raise NotFoundError(f"no QC target for {analyte}/{instrument_id}/{material_id}")
        return target

    async def get_investigation(self, principal: Principal, investigation_id: str) -> Investigation:
        await self._ctx.authorizer.require(
            principal, "qc:read", _resource("investigation", investigation_id)
        )
        investigation = await self._repos.investigations.get(investigation_id)
        if investigation is None:
            raise NotFoundError(f"investigation {investigation_id} not found")
        return investigation

    async def list_investigations(
        self, principal: Principal, *, limit: int, offset: int
    ) -> Sequence[Investigation]:
        await self._ctx.authorizer.require(principal, "qc:read", _resource("investigation"))
        return await self._repos.investigations.list(limit=limit, offset=offset)

    async def investigate(self, principal: Principal, evaluation_id: str) -> Investigation:
        evaluation = await self._repos.evaluations.get(evaluation_id)
        if evaluation is None:
            raise NotFoundError(f"evaluation {evaluation_id} not found")
        return await self._workflow.investigation.investigate(evaluation, principal)

    async def list_holds(self, principal: Principal, *, active_only: bool) -> Sequence[QcHold]:
        await self._ctx.authorizer.require(principal, "qc:read", _resource("qc-hold"))
        return await self._repos.holds.list(active_only=active_only)

    # ----------------------------------------------------------------------- CAPA

    async def get_capa(self, principal: Principal, capa_id: str) -> Capa:
        capa = await self._repos.capas.get(capa_id)
        if capa is None:
            await self._ctx.authorizer.require(
                principal, "capa:read", capa_resource(capa_id, "unknown", "unknown")
            )
            raise NotFoundError(f"CAPA {capa_id} not found")
        await self._ctx.authorizer.require(
            principal, "capa:read", capa_resource(capa_id, capa.analyte, capa.instrument_id)
        )
        return capa

    async def list_capas(
        self, principal: Principal, *, status: CapaStatus | None, limit: int, offset: int
    ) -> Sequence[Capa]:
        await self._ctx.authorizer.require(principal, "capa:read", capa_resource("*", "*", "*"))
        return await self._repos.capas.list(status=status, limit=limit, offset=offset)

    async def propose_capa(self, principal: Principal, body: ProposeCapa) -> Capa:
        investigation = await self._repos.investigations.get(body.investigation_id)
        if investigation is None:
            raise NotFoundError(f"investigation {body.investigation_id} not found")
        capa = await self._workflow.corrective.propose(
            investigation,
            principal,
            hypothesis_id=body.hypothesis_id,
            corrective_actions=body.corrective_actions,
        )
        await self._link_hold(capa.analyte, capa.instrument_id, capa.capa_id)
        return capa

    async def _signable(self, principal: Principal, capa_id: str, action: str) -> Capa:
        capa = await self._repos.capas.get(capa_id)
        if capa is None:
            raise NotFoundError(f"CAPA {capa_id} not found")
        await self._ctx.authorizer.require(
            principal, action, capa_resource(capa_id, capa.analyte, capa.instrument_id)
        )
        return capa

    async def _save(self, principal: Principal, before: Capa, after: Capa, action: str) -> Capa:
        await self._repos.capas.update(after, expected_version=before.version)
        self._ctx.metrics.capa_transitions.labels(after.status.value).inc()
        signature = after.signatures[-1] if after.signatures else None
        await self._ctx.record(
            principal.subject,
            action,
            f"capa:{after.capa_id}",
            after.status.value,
            {
                "from": before.status.value,
                "signature_meaning": signature.meaning.value if signature else None,
                "record_digest": signature.record_digest if signature else None,
            },
        )
        return after

    async def approve_capa(self, principal: Principal, capa_id: str, body: SignatureInput) -> Capa:
        capa = await self._signable(principal, capa_id, "capa:approve")
        if capa.proposed_by == principal.subject:
            raise SeparationOfDutiesError("the proposer of a CAPA cannot approve it")
        now = self._ctx.clock.now()
        signature = capa_rules.sign(
            capa,
            signer=principal.subject,
            signer_name=principal.name,
            meaning=SignatureMeaning.APPROVAL,
            statement=body.statement,
            now=now,
        )
        return await self._save(
            principal, capa, capa_rules.approve(capa, signature, now), "capa.approve"
        )

    async def reject_capa(self, principal: Principal, capa_id: str, body: SignatureInput) -> Capa:
        capa = await self._signable(principal, capa_id, "capa:reject")
        now = self._ctx.clock.now()
        signature = capa_rules.sign(
            capa,
            signer=principal.subject,
            signer_name=principal.name,
            meaning=SignatureMeaning.REJECTION,
            statement=body.statement,
            now=now,
        )
        return await self._save(
            principal, capa, capa_rules.reject(capa, signature, now), "capa.reject"
        )

    async def complete_action(
        self, principal: Principal, capa_id: str, body: ActionCompletion
    ) -> Capa:
        capa = await self._signable(principal, capa_id, "capa:complete-action")
        now = self._ctx.clock.now()
        signature = capa_rules.sign(
            capa,
            signer=principal.subject,
            signer_name=principal.name,
            meaning=SignatureMeaning.PERFORMED,
            statement=body.note,
            now=now,
        )
        updated = capa_rules.complete_action(capa, signature, body.note, now)
        return await self._save(principal, capa, updated, "capa.complete_action")

    async def verify_capa(self, principal: Principal, capa_id: str) -> Capa:
        capa = await self._repos.capas.get(capa_id)
        if capa is None:
            raise NotFoundError(f"CAPA {capa_id} not found")
        return await self._workflow.verification.verify(capa, principal, record_insufficient=True)

    async def close_capa(self, principal: Principal, capa_id: str, body: SignatureInput) -> Capa:
        capa = await self._signable(principal, capa_id, "capa:close")
        performed = capa.signed(SignatureMeaning.PERFORMED)
        if performed is not None and performed.signer == principal.subject:
            raise SeparationOfDutiesError(
                "the person who performed the corrective action cannot close the CAPA"
            )
        if capa.status is not CapaStatus.VERIFIED:
            raise InvalidStateError(
                f"CAPA {capa_id} is {capa.status.value}; verification must pass before closure"
            )
        now = self._ctx.clock.now()
        signature = capa_rules.sign(
            capa,
            signer=principal.subject,
            signer_name=principal.name,
            meaning=SignatureMeaning.CLOSURE,
            statement=body.statement,
            now=now,
        )
        closed = await self._save(
            principal, capa, capa_rules.close(capa, signature, now), "capa.close"
        )
        hold = await self._repos.holds.active(capa.analyte, capa.instrument_id)
        if hold is not None:
            released = hold.model_copy(
                update={
                    "active": False,
                    "released_at": now,
                    "released_by": principal.subject,
                    "capa_id": capa.capa_id,
                }
            )
            await self._repos.holds.update(released)
            await self._ctx.record(
                principal.subject,
                "qc.hold.release",
                f"hold:{capa.analyte}@{capa.instrument_id}",
                "success",
                {"capa_id": capa.capa_id, "record_digest": signature.record_digest},
            )
            await self._refresh_hold_gauge()
        return closed
