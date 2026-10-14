"""Full HTTP lifecycle: HL7 in, rejection, hold, investigation, CAPA, signatures, release."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import INSTRUMENT, START, FakeClock, FakeIdp, bearer, value
from tests.unit.test_hl7 import group, message

pytestmark = pytest.mark.e2e

SIGN = {"statement": "Reviewed the investigation evidence and approve."}


def hl7_run(
    control_id: str, hours: float, z1: float, z2: float, reagent: str = "R-100"
) -> dict[str, str]:
    stamp = (START + timedelta(hours=hours)).strftime("%Y%m%d%H%M")
    return {
        "message": message(
            group("GLU-L1", "L1-2026A", str(value(1, z1)), set_id="1", when=stamp, reagent=reagent),
            group("GLU-L2", "L2-2026A", str(value(2, z2)), set_id="2", when=stamp, reagent=reagent),
            control_id=control_id,
        )
    }


def setup_targets(client: TestClient, idp: FakeIdp) -> None:
    supervisor = bearer(idp, "sam@lab.test", "qc-supervisor")
    for level, (material, lot, mean, sd) in {
        1: ("GLU-L1", "L1-2026A", 100.0, 2.5),
        2: ("GLU-L2", "L2-2026A", 300.0, 6.0),
    }.items():
        response = client.put(
            "/v1/qc/targets",
            headers=supervisor,
            json={
                "analyte": "GLU",
                "instrument_id": INSTRUMENT,
                "material_id": material,
                "control_lot": lot,
                "level": level,
                "mean": mean,
                "sd": sd,
                "units": "mg/dL",
            },
        )
        assert response.status_code == 200, response.text


def test_qc_failure_to_release(client: TestClient, idp: FakeIdp, clock: FakeClock) -> None:
    interface = bearer(idp, "lis-interface", "qc-interface")
    tech = bearer(idp, "tess@lab.test", "qc-technologist")
    supervisor = bearer(idp, "sam@lab.test", "qc-supervisor")
    reviewer = bearer(idp, "rae@lab.test", "qc-supervisor")
    setup_targets(client, idp)

    for i, (z1, z2) in enumerate([(0.2, -0.4), (-0.6, 0.3), (0.5, 0.1)]):
        ok = client.post("/v1/qc/hl7", headers=interface, json=hl7_run(f"B{i}", i, z1, z2))
        assert ok.status_code == 200, ok.text
        assert ok.json()["evaluations"][0]["outcome"] == "accept"

    failed = client.post(
        "/v1/qc/hl7", headers=interface, json=hl7_run("F1", 4, 2.6, 2.3, reagent="R-200")
    ).json()
    evaluation = failed["evaluations"][0]
    assert evaluation["outcome"] == "reject"
    assert {v["rule"] for v in evaluation["violations"]} >= {"2_2s", "1_2s"}
    assert "MSA|AA|F1" in failed["ack"]
    capa_id = failed["capas_proposed"][0]

    holds = client.get("/v1/holds", headers=tech).json()["items"]
    assert [(h["analyte"], h["active"]) for h in holds] == [("GLU", True)]
    investigation = client.get(
        f"/v1/investigations/{failed['investigations'][0]}", headers=tech
    ).json()
    assert investigation["hypotheses"][0]["category"] == "reagent_lot"

    # Agents never sign; technologists cannot approve.
    assert client.post(f"/v1/capas/{capa_id}/approve", headers=tech, json=SIGN).status_code == 403
    approved = client.post(f"/v1/capas/{capa_id}/approve", headers=supervisor, json=SIGN)
    assert approved.json()["status"] == "approved"
    assert approved.json()["signatures"][0]["signer_name"] == "Sam"

    clock.current = START + timedelta(hours=4, minutes=30)
    done = client.post(
        f"/v1/capas/{capa_id}/complete-action",
        headers=tech,
        json={"note": "Recalibrated glucose on CHEM-01 with reagent lot R-200."},
    )
    assert done.json()["status"] == "action_completed"

    for i, hours in enumerate((5, 6)):
        response = client.post(
            "/v1/qc/hl7",
            headers=interface,
            json=hl7_run(f"V{i}", hours, 0.3, -0.2, reagent="R-200"),
        )
        assert response.status_code == 200
    capa = client.get(f"/v1/capas/{capa_id}", headers=tech).json()
    assert capa["status"] == "verified"

    closed = client.post(f"/v1/capas/{capa_id}/close", headers=reviewer, json=SIGN)
    assert closed.status_code == 200, closed.text
    assert client.get("/v1/holds", headers=tech).json()["items"] == []

    runs = client.get("/v1/qc/runs", headers=tech, params={"outcome": "reject"}).json()
    assert [r["run_id"] for r in runs["items"]] == ["F1"]
    assert (
        client.get(f"/v1/qc/runs/{runs['items'][0]['evaluation_id']}", headers=tech).status_code
        == 200
    )
    chart = client.get(
        "/v1/qc/levey-jennings",
        headers=tech,
        params={"analyte": "GLU", "instrument_id": INSTRUMENT, "material_id": "GLU-L1"},
    ).json()
    assert len(chart["points"]) == 6
    assert (
        client.get("/v1/capas", headers=tech, params={"status": "closed"}).json()["items"][0][
            "capa_id"
        ]
        == capa_id
    )
    assert len(client.get("/v1/investigations", headers=tech).json()["items"]) == 1
    assert (
        len(client.get("/v1/qc/targets", headers=tech, params={"analyte": "GLU"}).json()["items"])
        == 2
    )

    metrics = client.get("/metrics").text
    assert 'labqc_run_outcomes_total{outcome="reject"} 1.0' in metrics
    assert 'labqc_capa_transitions_total{status="closed"} 1.0' in metrics
    assert "labqc_active_holds 0.0" in metrics


def test_other_routes(client: TestClient, idp: FakeIdp) -> None:
    supervisor = bearer(idp, "sam@lab.test", "qc-supervisor")
    tech = bearer(idp, "tess@lab.test", "qc-technologist")
    setup_targets(client, idp)
    established = client.post(
        "/v1/qc/targets/establish",
        headers=supervisor,
        json={
            "analyte": "K",
            "instrument_id": INSTRUMENT,
            "material_id": "K-L1",
            "control_lot": "K1-2026A",
            "level": 1,
            "units": "mmol/L",
            "values": [4.0 + (i % 4 - 1.5) * 0.04 for i in range(20)],
        },
    )
    assert established.status_code == 201, established.text
    event = client.post(
        "/v1/instrument-events",
        headers=tech,
        json={"instrument_id": INSTRUMENT, "kind": "maintenance", "occurred_at": START.isoformat()},
    )
    assert event.status_code == 201
    run = client.post(
        "/v1/qc/runs",
        headers=tech,
        json={
            "analyte": "GLU",
            "instrument_id": INSTRUMENT,
            "run_id": "api-1",
            "results": [
                {
                    "material_id": "GLU-L1",
                    "value": 108.5,
                    "units": "mg/dL",
                    "observed_at": START.isoformat(),
                },
                {
                    "material_id": "GLU-L2",
                    "value": 300.0,
                    "units": "mg/dL",
                    "observed_at": START.isoformat(),
                },
            ],
        },
    ).json()
    evaluation_id = run["evaluations"][0]["evaluation_id"]
    redo = client.post("/v1/investigations", headers=tech, json={"evaluation_id": evaluation_id})
    assert redo.status_code == 201
    assert redo.json()["hypotheses"][0]["category"] == "instrument"
    proposal = client.post(
        "/v1/capas", headers=tech, json={"investigation_id": redo.json()["investigation_id"]}
    )
    assert proposal.status_code == 409  # the agents already opened one
    capa_id = run["capas_proposed"][0]
    assert (
        client.post(f"/v1/capas/{capa_id}/reject", headers=supervisor, json=SIGN).json()["status"]
        == "rejected"
    )
    assert client.post(f"/v1/capas/{capa_id}/verify", headers=tech).status_code == 409
    sigma = client.get(
        "/v1/qc/sigma",
        headers=tech,
        params={"analyte": "GLU", "instrument_id": INSTRUMENT, "material_id": "GLU-L1"},
    )
    assert sigma.status_code == 422
    assert sigma.json()["error"] == "insufficient_data"
    astm_error = client.post("/v1/qc/astm", headers=tech, json={"data": "garbage", "framed": True})
    assert astm_error.status_code == 400
    assert "ack" not in astm_error.json()


def test_errors_are_oauth_shaped(client: TestClient, idp: FakeIdp) -> None:
    missing = client.get("/v1/holds")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json() == {"error": "unauthorized", "error_description": "bearer token required"}
    invalid = client.post(
        "/v1/qc/runs", headers=bearer(idp, "t", "qc-technologist"), json={"bad": 1}
    )
    assert invalid.status_code == 422
    body: dict[str, Any] = invalid.json()
    assert body["error"] == "invalid_request"
    assert body["details"]
    refused = client.post(
        "/v1/qc/hl7",
        headers=bearer(idp, "lis", "qc-interface"),
        json={"message": "MSH|^~\\&|A|B|C|D|20261001||ADT^A01|9|P|2.5.1"},
    )
    assert refused.status_code == 400
    assert "MSA|AR|9" in refused.json()["ack"]


def test_operations_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz", headers={"X-Correlation-ID": "trace-123"})
    assert ready.json() == {"status": "ready"}
    assert ready.headers["x-correlation-id"] == "trace-123"
    assert ready.headers["x-content-type-options"] == "nosniff"
    long_id = client.get("/healthz", headers={"X-Correlation-ID": "x" * 100})
    assert long_id.headers["x-correlation-id"] != "x" * 100
    assert "labqc_http_request_duration_seconds" in client.get("/metrics").text
