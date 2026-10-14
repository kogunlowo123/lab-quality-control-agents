"""Investigation narrative written by a foundation model, under strict limits.

The deterministic root cause ranking is the record. The model only turns it
into a short summary a supervisor can read quickly:

* requests go through the agentic-core ``ModelRouter`` with classification
  CONFIDENTIAL, so only providers approved for that level are eligible;
* the agentic-core ``ContentGuard`` inspects the request (free-text event
  notes typed by staff are marked EXTERNAL and isolated as data) and the
  output (credentials blocked, PII redacted);
* the output must cite hypotheses by ID (``[H1]``) and may cite only IDs that
  exist; otherwise it is discarded as ungrounded;
* any provider failure leaves the narrative empty. QC decisions never wait
  on, or depend on, a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from enum import StrEnum

from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.domain.errors import ProviderUnavailableError
from agentic_core.models.router import ModelRouter
from agentic_core.models.types import Message, ModelRequest, Role
from agentic_core.observability import get_logger
from agentic_core.security import ContentGuard

from lab_quality_control_agents.domain.models import InstrumentEvent, Investigation, RunEvaluation

log = get_logger("lab_quality_control_agents.narrative")

SYSTEM_PROMPT = (
    "You summarise laboratory quality control investigations for a QC supervisor. "
    "Use only the facts supplied. Refer to each candidate cause by its ID in square "
    "brackets, for example [H1]. Do not introduce causes that are not listed. Do not "
    "state that patient results may be released; release decisions belong to the "
    "supervisor after verification. Write at most 150 words in plain sentences."
)

_CITATION = re.compile(r"\[(H\d+)\]")


class NarrativeOutcome(StrEnum):
    GENERATED = "generated"
    DISABLED = "disabled"
    GUARDED = "guarded"
    UNGROUNDED = "ungrounded"
    UNAVAILABLE = "unavailable"


class NarrativeWriter:
    def __init__(
        self, router: ModelRouter | None, guard: ContentGuard, *, max_tokens: int = 400
    ) -> None:
        self._router = router
        self._guard = guard
        self._max_tokens = max_tokens

    @property
    def enabled(self) -> bool:
        return self._router is not None

    def build_request(
        self,
        investigation: Investigation,
        evaluation: RunEvaluation,
        events: Sequence[InstrumentEvent],
    ) -> ModelRequest:
        facts = {
            "analyte": investigation.analyte,
            "instrument": investigation.instrument_id,
            "run": investigation.run_id,
            "outcome": evaluation.outcome.value,
            "violations": [
                {"rule": v.rule.value, "scope": v.scope.value, "levels": list(v.levels)}
                for v in evaluation.violations
            ],
            "hypotheses": [
                {
                    "id": h.hypothesis_id,
                    "cause": h.category.value,
                    "probability": h.probability,
                    "evidence": [e.description for e in h.evidence if e.kind != "rule"],
                }
                for h in investigation.hypotheses
            ],
            "other_analytes_rejected": list(investigation.other_analytes_rejected),
        }
        messages = [
            Message(role=Role.SYSTEM, content=SYSTEM_PROMPT, trust=TrustLevel.SYSTEM),
            Message(
                role=Role.USER,
                content="Investigation facts (JSON):\n" + json.dumps(facts, sort_keys=True),
                trust=TrustLevel.VERIFIED,
            ),
        ]
        notes = [f"{e.kind.value}: {e.detail}" for e in events if e.detail.strip()]
        if notes:
            messages.append(
                Message(
                    role=Role.USER,
                    content="Staff notes recorded with instrument events:\n" + "\n".join(notes),
                    trust=TrustLevel.EXTERNAL,
                )
            )
        return ModelRequest(
            messages=tuple(messages),
            max_tokens=self._max_tokens,
            temperature=0.0,
            classification=DataClassification.CONFIDENTIAL,
            task="summarize",
        )

    async def write(
        self,
        investigation: Investigation,
        evaluation: RunEvaluation,
        events: Sequence[InstrumentEvent],
    ) -> tuple[str | None, str | None, NarrativeOutcome]:
        if self._router is None:
            return None, None, NarrativeOutcome.DISABLED
        request = self.build_request(investigation, evaluation, events)
        inbound = self._guard.inspect_request(request)
        if inbound.blocked or inbound.request is None:
            log.warning("narrative_input_blocked", reasons=list(inbound.reasons))
            return None, None, NarrativeOutcome.GUARDED
        try:
            routed = await self._router.generate(inbound.request)
        except ProviderUnavailableError as exc:
            log.warning("narrative_provider_unavailable", attempts=exc.attempts)
            return None, None, NarrativeOutcome.UNAVAILABLE
        outbound = self._guard.inspect_output(routed.response.text)
        if outbound.blocked or outbound.text is None:
            log.warning("narrative_output_blocked", reasons=list(outbound.reasons))
            return None, None, NarrativeOutcome.GUARDED
        text = outbound.text.strip()
        cited = set(_CITATION.findall(text))
        known = {h.hypothesis_id for h in investigation.hypotheses}
        if not cited or not cited <= known:
            log.warning("narrative_ungrounded", cited=sorted(cited), known=sorted(known))
            return None, None, NarrativeOutcome.UNGROUNDED
        return text, routed.response.provider, NarrativeOutcome.GENERATED
