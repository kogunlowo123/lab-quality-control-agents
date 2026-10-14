"""The QC agents and the workflow that chains them.

    QC event (rejected run)
      -> Investigation agent: gathers instrument events, lot changes and
         cross-analyte failures; ranks root causes (deterministic) and asks a
         model for a narrative (optional, guarded)
      -> Corrective action agent: proposes a CAPA from the leading cause;
         the CAPA waits for a supervisor's electronic signature
      -> (human) supervisor approves; staff perform and sign the action
      -> Verification agent: applies the verification run rules to QC runs
         performed after the action
      -> (human) supervisor closes the CAPA, which lifts the result hold

Agents act under their own identity (an agent-identity-fabric credential
when the fabric is configured) and every step is authorized by policy and
written to the signed audit chain. Agents can never approve, reject, close or
release: policy returns require_approval for those actions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification
from agentic_core.domain.identifiers import new_id
from agentic_core.observability import correlation_id, get_logger
from agentic_core.policy import Resource

from lab_quality_control_agents.application.narrative import NarrativeWriter
from lab_quality_control_agents.domain import capa as capa_rules
from lab_quality_control_agents.domain.catalog import AnalyteCatalog
from lab_quality_control_agents.domain.errors import ConflictError, NotFoundError, QcError
from lab_quality_control_agents.domain.investigation import InvestigationInput, analyse
from lab_quality_control_agents.domain.models import (
    FULL_MULTIRULE,
    Capa,
    CapaStatus,
    InstrumentEventKind,
    Investigation,
    RunEvaluation,
    RunOutcome,
    WestgardRule,
)
from lab_quality_control_agents.domain.ports import Clock, Repositories
from lab_quality_control_agents.domain.verification import VerificationPolicy, verify
from lab_quality_control_agents.integrations.identity_fabric import AgentCredentials
from lab_quality_control_agents.observability.metrics import QcMetrics
from lab_quality_control_agents.security.authn import AGENT_ROLE, Principal, PrincipalKind
from lab_quality_control_agents.security.authz import Authorizer

log = get_logger("lab_quality_control_agents.agents")

INVESTIGATION_AGENT = "investigation-agent"
CORRECTIVE_ACTION_AGENT = "corrective-action-agent"
VERIFICATION_AGENT = "verification-agent"


class AgentIdentity:
    """The principal the internal agents act as."""

    def __init__(
        self,
        agent_id: str,
        *,
        scopes: frozenset[str],
        credentials: AgentCredentials | None = None,
        fabric_issuer: str | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._scopes = scopes
        self._credentials = credentials
        self._fabric_issuer = fabric_issuer or "identity-fabric"

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def principal(self, role: str) -> Principal:
        if self._credentials is not None:
            issued = await self._credentials.credential()
            return Principal(
                subject=self._agent_id,
                kind=PrincipalKind.AGENT,
                name=role,
                scopes=frozenset(issued.scope.split()) or self._scopes,
                issuer=self._fabric_issuer,
                via_fabric=True,
                token=issued.access_token,
            )
        return Principal(
            subject=self._agent_id,
            kind=PrincipalKind.AGENT,
            name=role,
            roles=frozenset({AGENT_ROLE}),
            scopes=self._scopes,
            issuer="local",
        )


@dataclass
class AgentContext:
    repos: Repositories
    authorizer: Authorizer
    audit: AuditLog
    clock: Clock
    metrics: QcMetrics
    catalog: AnalyteCatalog

    async def record(
        self, actor: str, action: str, resource: str, outcome: str, details: dict[str, object]
    ) -> None:
        await self.audit.record(
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            correlation_id=correlation_id(),
            details=dict(details),
        )


def capa_resource(capa_id: str, analyte: str, instrument_id: str) -> Resource:
    return Resource(
        type="capa",
        id=capa_id,
        classification=DataClassification.CONFIDENTIAL,
        attributes={"analyte": analyte, "instrument_id": instrument_id},
    )


class InvestigationAgent:
    def __init__(
        self,
        ctx: AgentContext,
        narrative: NarrativeWriter,
        *,
        lookback: timedelta,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._ctx = ctx
        self._narrative = narrative
        self._lookback = lookback
        self._new_id = id_factory

    async def investigate(self, evaluation: RunEvaluation, principal: Principal) -> Investigation:
        ctx = self._ctx
        await ctx.authorizer.require(
            principal,
            "investigation:run",
            Resource(
                type="qc-run",
                id=evaluation.evaluation_id,
                attributes={"analyte": evaluation.analyte},
            ),
        )
        if evaluation.outcome is not RunOutcome.REJECT:
            raise ConflictError(f"run {evaluation.run_id} was not rejected; nothing to investigate")
        repos = ctx.repos
        run_at = evaluation.run_observed_at
        last_accepted = await repos.evaluations.last_accepted_before(
            evaluation.analyte, evaluation.instrument_id, run_at
        )
        last_accepted_at = last_accepted.run_observed_at if last_accepted else None
        window_start = run_at - self._lookback
        if last_accepted_at is not None and last_accepted_at < window_start:
            window_start = last_accepted_at
        run_results = await repos.results.for_run(
            evaluation.analyte, evaluation.instrument_id, evaluation.run_id
        )
        previous = [
            r
            for r in await repos.results.query(
                analyte=evaluation.analyte,
                instrument_id=evaluation.instrument_id,
                until=last_accepted_at or window_start,
                limit=50,
            )
            if r.run_id != evaluation.run_id
        ]
        events = await repos.events.between(evaluation.instrument_id, window_start, run_at)
        others = [
            e
            for e in await repos.evaluations.list(
                outcome=RunOutcome.REJECT,
                instrument_id=evaluation.instrument_id,
                since=window_start,
                until=run_at,
                limit=200,
            )
            if e.analyte != evaluation.analyte
        ]
        calibration = await repos.events.latest(
            evaluation.instrument_id, InstrumentEventKind.CALIBRATION, evaluation.analyte, run_at
        )
        definition = ctx.catalog.get(evaluation.analyte)
        hypotheses = analyse(
            InvestigationInput(
                evaluation=evaluation,
                run_results=run_results,
                previous_results=previous,
                events=events,
                other_rejections=others,
                last_accepted_at=last_accepted_at,
                last_calibration_at=calibration.occurred_at if calibration else None,
                calibration_interval=(
                    definition.calibration_interval if definition else timedelta(days=30)
                ),
            )
        )
        investigation = Investigation(
            investigation_id=self._new_id(),
            evaluation_id=evaluation.evaluation_id,
            analyte=evaluation.analyte,
            instrument_id=evaluation.instrument_id,
            run_id=evaluation.run_id,
            error_types=tuple(sorted(evaluation.error_types)),
            hypotheses=hypotheses,
            other_analytes_rejected=tuple(sorted({e.analyte for e in others})),
            impact_window_start=last_accepted_at,
            impact_window_end=run_at,
            created_at=ctx.clock.now(),
            created_by=principal.subject,
        )
        relevant = [e for e in events if e.analyte in (None, evaluation.analyte)]
        text, provider, outcome = await self._narrative.write(investigation, evaluation, relevant)
        ctx.metrics.narratives.labels(outcome.value).inc()
        if text is not None:
            investigation = investigation.model_copy(
                update={"narrative": text, "narrative_provider": provider}
            )
        await repos.investigations.add(investigation)
        ctx.metrics.investigations.labels(investigation.leading.category.value).inc()
        await ctx.record(
            principal.subject,
            "investigation.create",
            f"investigation:{investigation.investigation_id}",
            "success",
            {
                "evaluation_id": evaluation.evaluation_id,
                "leading_cause": investigation.leading.category.value,
                "probability": investigation.leading.probability,
                "narrative": outcome.value,
            },
        )
        return investigation


class CorrectiveActionAgent:
    def __init__(self, ctx: AgentContext, *, id_factory: Callable[[], str] = new_id) -> None:
        self._ctx = ctx
        self._new_id = id_factory

    async def propose(
        self,
        investigation: Investigation,
        principal: Principal,
        *,
        hypothesis_id: str | None = None,
        corrective_actions: tuple[str, ...] | None = None,
    ) -> Capa:
        ctx = self._ctx
        capa_id = self._new_id()
        await ctx.authorizer.require(
            principal,
            "capa:propose",
            capa_resource(capa_id, investigation.analyte, investigation.instrument_id),
        )
        existing = await ctx.repos.capas.open_for(
            investigation.analyte, investigation.instrument_id
        )
        if existing is not None:
            raise ConflictError(
                f"CAPA {existing.capa_id} is already open for {investigation.analyte} "
                f"on {investigation.instrument_id}"
            )
        if hypothesis_id is None:
            hypothesis = investigation.leading
        else:
            found = [h for h in investigation.hypotheses if h.hypothesis_id == hypothesis_id]
            if not found:
                raise NotFoundError(f"hypothesis {hypothesis_id} is not part of the investigation")
            hypothesis = found[0]
        capa = capa_rules.propose(
            capa_id=capa_id,
            investigation=investigation,
            hypothesis=hypothesis,
            proposed_by=principal.subject,
            now=ctx.clock.now(),
            corrective_actions=corrective_actions,
        )
        await ctx.repos.capas.add(capa)
        ctx.metrics.capa_transitions.labels(capa.status.value).inc()
        await ctx.record(
            principal.subject,
            "capa.propose",
            f"capa:{capa.capa_id}",
            "success",
            {
                "investigation_id": investigation.investigation_id,
                "root_cause": capa.root_cause.value,
                "requires": "supervisor approval",
            },
        )
        return capa


class VerificationAgent:
    def __init__(self, ctx: AgentContext, policy: VerificationPolicy) -> None:
        self._ctx = ctx
        self._policy = policy

    async def verify(self, capa: Capa, principal: Principal, *, record_insufficient: bool) -> Capa:
        ctx = self._ctx
        await ctx.authorizer.require(
            principal, "capa:verify", capa_resource(capa.capa_id, capa.analyte, capa.instrument_id)
        )
        if capa.status is not CapaStatus.ACTION_COMPLETED or capa.action_completed_at is None:
            raise ConflictError(f"CAPA {capa.capa_id} is {capa.status.value}; nothing to verify")
        targets = await ctx.repos.targets.list(
            analyte=capa.analyte, instrument_id=capa.instrument_id
        )
        rules: set[WestgardRule] = set()
        for target in targets:
            rules.update(target.rules)
        results = await ctx.repos.results.query(
            analyte=capa.analyte,
            instrument_id=capa.instrument_id,
            since=capa.action_completed_at,
            limit=200,
        )
        outcome = verify(
            results=results,
            required_levels=frozenset(t.level for t in targets),
            error_types=capa.error_types,
            rules=tuple(r for r in FULL_MULTIRULE if r in rules) or FULL_MULTIRULE,
            policy=self._policy,
            now=ctx.clock.now(),
        )
        if not outcome.sufficient_data and not record_insufficient:
            return capa
        updated = capa_rules.record_verification(capa, outcome, ctx.clock.now())
        await ctx.repos.capas.update(updated, expected_version=capa.version)
        ctx.metrics.capa_transitions.labels(updated.status.value).inc()
        await ctx.record(
            principal.subject,
            "capa.verify",
            f"capa:{capa.capa_id}",
            "passed" if outcome.passed else "failed",
            {"runs": list(outcome.run_ids), "reasons": list(outcome.reasons)},
        )
        return updated


@dataclass
class WorkflowReport:
    investigation_id: str | None = None
    capa_id: str | None = None
    verified_capa_id: str | None = None
    errors: list[str] = field(default_factory=list)


class QcWorkflow:
    """Runs the agents for a QC event. Failures are recorded, never hidden.

    The run outcome and the result hold are decided before the agents run,
    so an agent failure (identity fabric down, model provider down) delays
    the investigation but can never release results.
    """

    def __init__(
        self,
        ctx: AgentContext,
        identity: AgentIdentity,
        investigation: InvestigationAgent,
        corrective: CorrectiveActionAgent,
        verification: VerificationAgent,
    ) -> None:
        self._ctx = ctx
        self._identity = identity
        self.investigation = investigation
        self.corrective = corrective
        self.verification = verification

    async def _step_failed(self, step: str, subject: str, exc: Exception) -> str:
        message = f"{step}: {exc}"
        log.warning("workflow_step_failed", step=step, error=str(exc))
        await self._ctx.record(
            self._identity.agent_id, f"workflow.{step}", subject, "failure", {"error": str(exc)}
        )
        return message

    async def on_rejected(self, evaluation: RunEvaluation) -> WorkflowReport:
        report = WorkflowReport()
        subject = f"qc-run:{evaluation.evaluation_id}"
        try:
            investigator = await self._identity.principal(INVESTIGATION_AGENT)
            investigation = await self.investigation.investigate(evaluation, investigator)
        except QcError as exc:
            report.errors.append(await self._step_failed("investigate", subject, exc))
            return report
        report.investigation_id = investigation.investigation_id
        open_capa = await self._ctx.repos.capas.open_for(
            evaluation.analyte, evaluation.instrument_id
        )
        if open_capa is not None:
            return report
        try:
            proposer = await self._identity.principal(CORRECTIVE_ACTION_AGENT)
            capa = await self.corrective.propose(investigation, proposer)
        except QcError as exc:
            report.errors.append(await self._step_failed("propose_capa", subject, exc))
            return report
        report.capa_id = capa.capa_id
        return report

    async def on_run(self, analyte: str, instrument_id: str) -> WorkflowReport:
        """After any run: try to verify an open CAPA whose action is complete."""
        report = WorkflowReport()
        capa = await self._ctx.repos.capas.open_for(analyte, instrument_id)
        if capa is None or capa.status is not CapaStatus.ACTION_COMPLETED:
            return report
        try:
            verifier = await self._identity.principal(VERIFICATION_AGENT)
            updated = await self.verification.verify(capa, verifier, record_insufficient=False)
        except QcError as exc:
            report.errors.append(await self._step_failed("verify", f"capa:{capa.capa_id}", exc))
            return report
        if updated.status is CapaStatus.VERIFIED:
            report.verified_capa_id = updated.capa_id
        return report
