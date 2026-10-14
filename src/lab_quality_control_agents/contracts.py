"""Export published contracts: JSON Schemas for inputs/outputs and the OpenAPI document.

python -m lab_quality_control_agents.contracts            # rewrite schemas/ and docs/api/
python -m lab_quality_control_agents.contracts --check    # exit 1 if committed files are stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from lab_quality_control_agents.api.app import create_app
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
)
from lab_quality_control_agents.domain.models import (
    Capa,
    InstrumentEvent,
    Investigation,
    QcHold,
    QcResult,
    QcTarget,
    RunEvaluation,
)
from lab_quality_control_agents.domain.statistics import LeveyJenningsChart, SigmaAssessment

BASE_ID = "https://github.com/kogunlowo123/lab-quality-control-agents"

MODELS: dict[str, type[BaseModel]] = {
    "run-submission.request": RunSubmission,
    "hl7-submission.request": Hl7Submission,
    "astm-submission.request": AstmSubmission,
    "qc-target.request": TargetInput,
    "establish-target.request": EstablishTarget,
    "instrument-event.request": InstrumentEventInput,
    "propose-capa.request": ProposeCapa,
    "signature.request": SignatureInput,
    "action-completion.request": ActionCompletion,
    "ingest-report.response": IngestReport,
    "qc-result.record": QcResult,
    "qc-target.record": QcTarget,
    "run-evaluation.record": RunEvaluation,
    "instrument-event.record": InstrumentEvent,
    "investigation.record": Investigation,
    "capa.record": Capa,
    "qc-hold.record": QcHold,
    "levey-jennings.response": LeveyJenningsChart,
    "sigma-assessment.response": SigmaAssessment,
}


async def _no_container() -> Any:  # pragma: no cover - never started
    raise RuntimeError("contract export does not start the application")


def documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        f"schemas/{name}.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE_ID}/schemas/{name}.schema.json",
            **model.model_json_schema(mode="validation"),
        }
        for name, model in MODELS.items()
    }
    app: FastAPI = create_app(_no_container)
    docs["docs/api/openapi.json"] = app.openapi()
    return docs


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = root or Path.cwd()
    stale: list[str] = []
    for relative, document in documents().items():
        path = base / relative
        content = render(document)
        if "--check" in args:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(
            "stale contracts (run python -m lab_quality_control_agents.contracts): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
