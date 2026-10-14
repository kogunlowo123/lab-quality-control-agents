"""Adversarial tests. Each maps to a threat ID in docs/security/threat-model.md."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
import pytest
from agentic_core.audit import AuditEvent, verify_chain
from fastapi.testclient import TestClient

from lab_quality_control_agents.domain.errors import (
    ApprovalRequiredError,
    ForbiddenError,
    MessageFormatError,
)
from lab_quality_control_agents.security.authn import Principal, PrincipalKind
from tests.conftest import AGENT_ID, AUDIENCE, INSTRUMENT, OIDC_ISSUER, SUPERVISOR, FakeIdp, bearer
from tests.e2e.test_http_lifecycle import SIGN, hl7_run, setup_targets
from tests.unit.test_hl7 import group, message

pytestmark = pytest.mark.security

JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ")


def test_t1_patient_results_and_identifiers_are_refused(client: TestClient, idp: FakeIdp) -> None:
    interface = bearer(idp, "lis", "qc-interface")
    with_pid = message(group("GLU-L1", "L", "100"), pid="PID|1||MRN555^^^HOSP^MR||ROE^RICHARD")
    patient_specimen = message(group("GLU-L1", "L", "100", role="P"))
    for text in (with_pid, patient_specimen):
        response = client.post("/v1/qc/hl7", headers=interface, json={"message": text})
        assert response.status_code == 400
        assert "MSA|AR" in response.json()["ack"]
    astm_patient = "H|\\^&|R1||CHEM-01|||||||Q\rP|1||MRN555\rO|1|GLU-L1||^^^GLU\rL|1|N"
    response = client.post(
        "/v1/qc/astm", headers=interface, json={"data": astm_patient, "framed": False}
    )
    assert response.status_code == 400
    assert "patient" in response.json()["error_description"]


def test_t2_agents_cannot_sign_or_release(client: TestClient, idp: FakeIdp) -> None:
    setup_targets(client, idp)
    interface = bearer(idp, "lis", "qc-interface")
    failed = client.post("/v1/qc/hl7", headers=interface, json=hl7_run("X1", 1, 3.5, 0.0)).json()
    capa_id = failed["capas_proposed"][0]
    agent = bearer(idp, "rogue-agent", "qc-agent", scp="capa:* qc:*")
    for action in ("approve", "reject", "close"):
        response = client.post(f"/v1/capas/{capa_id}/{action}", headers=agent, json=SIGN)
        assert response.status_code == 403
        assert response.json()["error"] == "approval_required"
    note = {"note": "Agent claims the corrective action was done."}
    assert (
        client.post(f"/v1/capas/{capa_id}/complete-action", headers=agent, json=note).status_code
        == 403
    )
    for service_action in ("approve", "close"):
        response = client.post(
            f"/v1/capas/{capa_id}/{service_action}", headers=interface, json=SIGN
        )
        assert response.json()["error"] == "approval_required"
    assert client.get("/v1/holds", headers=bearer(idp, "t", "qc-technologist")).json()["items"]


def test_t3_self_approval_is_refused(client: TestClient, idp: FakeIdp) -> None:
    setup_targets(client, idp)
    supervisor = bearer(idp, "sam@lab.test", "qc-supervisor")
    failed = client.post(
        "/v1/qc/hl7", headers=bearer(idp, "lis", "qc-interface"), json=hl7_run("X2", 1, 3.5, 0.0)
    ).json()
    client.post(f"/v1/capas/{failed['capas_proposed'][0]}/reject", headers=supervisor, json=SIGN)
    own = client.post(
        "/v1/capas", headers=supervisor, json={"investigation_id": failed["investigations"][0]}
    ).json()
    response = client.post(f"/v1/capas/{own['capa_id']}/approve", headers=supervisor, json=SIGN)
    assert response.status_code == 403
    assert response.json()["error"] == "separation_of_duties"


@pytest.mark.parametrize(
    "variant", ["foreign_key", "alg_none", "wrong_audience", "expired", "wrong_issuer"]
)
def test_t4_forged_or_misdirected_tokens(client: TestClient, idp: FakeIdp, variant: str) -> None:
    now = int(time.time())
    claims = {
        "iss": OIDC_ISSUER,
        "sub": "mallory",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "roles": ["qc-supervisor"],
    }
    if variant == "foreign_key":
        token = FakeIdp(OIDC_ISSUER).mint(sub="mallory", roles=["qc-supervisor"])
    elif variant == "alg_none":
        token = jwt.encode(claims, key="", algorithm="none", headers={"kid": "k1"})
    elif variant == "wrong_audience":
        token = idp.mint(sub="mallory", aud="another-service", roles=["qc-supervisor"])
    elif variant == "expired":
        token = idp.mint(sub="mallory", ttl=-120, roles=["qc-supervisor"])
    else:
        token = FakeIdp("https://evil.test", kid="k1").mint(sub="mallory", roles=["qc-supervisor"])
    response = client.get("/v1/holds", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_t5_restricted_data_is_refused_even_for_supervisors(container: object) -> None:
    from agentic_core.domain.classification import DataClassification
    from agentic_core.policy import Resource

    outcome = await container.authorizer.decide(  # type: ignore[attr-defined]
        SUPERVISOR,
        "qc:read",
        Resource(type="patient-result", classification=DataClassification.RESTRICTED),
    )
    assert outcome.decision.value == "deny"


async def test_t6_delegated_credentials_cannot_sign(container: object) -> None:
    from agentic_core.policy import Resource

    delegated = Principal(
        subject="sam@lab.test",
        kind=PrincipalKind.HUMAN,
        name="Sam",
        roles=frozenset({"qc-supervisor"}),
        issuer=OIDC_ISSUER,
        delegation_chain=("spiffe://lab.test/agent/lims/orchestrator",),
    )
    with pytest.raises(ForbiddenError, match="denied"):
        await container.authorizer.require(delegated, "capa:approve", Resource(type="capa"))  # type: ignore[attr-defined]


async def test_t7_agent_identity_cannot_exceed_scopes(container: object) -> None:
    from agentic_core.policy import Resource

    agent = Principal(
        subject=AGENT_ID,
        kind=PrincipalKind.AGENT,
        name="a",
        scopes=frozenset({"qc:read"}),
        issuer="local",
    )
    with pytest.raises(ForbiddenError):
        await container.authorizer.require(agent, "capa:propose", Resource(type="capa"))  # type: ignore[attr-defined]
    with pytest.raises(ApprovalRequiredError):
        await container.authorizer.require(agent, "qc-target:write", Resource(type="qc-target"))  # type: ignore[attr-defined]


def test_t8_audit_chain_is_intact_and_free_of_credentials(
    client: TestClient, idp: FakeIdp, settings: object
) -> None:
    setup_targets(client, idp)
    headers = bearer(idp, "lis", "qc-interface")
    client.post("/v1/qc/hl7", headers=headers, json=hl7_run("X3", 1, 3.5, 0.0))
    container = client.app.state.container  # type: ignore[attr-defined]
    path: Path = settings.audit_log_path  # type: ignore[attr-defined]
    raw = path.read_text(encoding="utf-8")
    events = [AuditEvent.model_validate_json(line) for line in raw.splitlines()]
    public = container.audit._signer.public_key()
    assert verify_chain(events, {container.audit._key_id: public}) == len(events)
    actions = {e.action for e in events}
    assert {"qc.run.evaluate", "qc.hold.place", "investigation.create", "capa.propose"} <= actions
    assert headers["Authorization"].split()[1] not in raw
    assert JWT_SHAPE.search(raw) is None  # no JWTs of any kind


def test_t9_untrusted_identifiers_do_not_reach_metric_labels(
    client: TestClient, idp: FakeIdp
) -> None:
    agent = bearer(idp, "lims-agent", "qc-agent", scp="qc:read")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {"parts": [{"kind": "data", "data": {"skill": "attacker-chosen-label"}}]}
        },
    }
    client.post("/a2a", headers=agent, json=body)
    client.post("/a2a", json={**body, "method": "attacker-method"})
    metrics = client.get("/metrics").text
    assert "attacker" not in metrics


def test_t10_oversized_and_malformed_input_is_rejected(client: TestClient, idp: FakeIdp) -> None:
    headers = bearer(idp, "lis", "qc-interface")
    huge = {"message": "MSH|" + "x" * 1_000_001}
    assert client.post("/v1/qc/hl7", headers=headers, json=huge).status_code == 422
    too_many = {
        "analyte": "GLU",
        "instrument_id": INSTRUMENT,
        "run_id": "r",
        "results": [
            {
                "material_id": "GLU-L1",
                "value": 1,
                "units": "mg/dL",
                "observed_at": "2026-10-01T08:00:00Z",
            }
        ]
        * 13,
    }
    assert client.post("/v1/qc/runs", headers=headers, json=too_many).status_code == 422
    sneaky = dict(too_many, results=too_many["results"][:1], approved=True)
    assert client.post("/v1/qc/runs", headers=headers, json=sneaky).status_code == 422
    nan = json.dumps(dict(too_many, results=[dict(too_many["results"][0], value=float("nan"))]))
    response = client.post(
        "/v1/qc/runs", headers={**headers, "Content-Type": "application/json"}, content=nan
    )
    assert response.status_code == 422


async def test_t11_path_injection_in_identifiers(container: object) -> None:
    from lab_quality_control_agents.application.commands import RunSubmission

    with pytest.raises(ValueError, match=r"(?i)string should match pattern"):
        RunSubmission.model_validate(
            {"analyte": "../etc", "instrument_id": "X", "run_id": "r", "results": []}
        )


def test_t12_hl7_escape_abuse_is_rejected() -> None:
    from lab_quality_control_agents.integrations import hl7

    text = message(group("GLU-L1", "L", "1\\Zbad\\"))
    with pytest.raises(MessageFormatError):
        hl7.extract_qc(text, default_tz=ZoneInfo("UTC"))
