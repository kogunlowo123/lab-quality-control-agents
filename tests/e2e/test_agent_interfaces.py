"""MCP (streamable HTTP) and A2A (JSON-RPC) end to end."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import INSTRUMENT, START, FakeIdp, bearer, value
from tests.e2e.test_http_lifecycle import hl7_run, setup_targets

pytestmark = pytest.mark.e2e

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


def mcp(
    client: TestClient, method: str, params: dict[str, Any] | None = None, **headers: str
) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp/", headers={**MCP_HEADERS, **headers}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def run_args(run_id: str, z1: float) -> dict[str, Any]:
    return {
        "analyte": "GLU",
        "instrument_id": INSTRUMENT,
        "run_id": run_id,
        "results": [
            {
                "material_id": "GLU-L1",
                "value": value(1, z1),
                "units": "mg/dL",
                "observed_at": START.isoformat(),
            },
            {
                "material_id": "GLU-L2",
                "value": value(2, 0.1),
                "units": "mg/dL",
                "observed_at": START.isoformat(),
            },
        ],
    }


class TestMcp:
    def test_initialize_and_list_tools(self, client: TestClient) -> None:
        init = mcp(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "lab-quality-control"
        tools = {t["name"]: t for t in mcp(client, "tools/list")["result"]["tools"]}
        assert set(tools) == {
            "evaluate_qc_run",
            "levey_jennings",
            "sigma_metric",
            "get_investigation",
            "get_capa",
            "list_holds",
        }
        assert not any("approve" in name or "close" in name for name in tools)
        for tool in tools.values():
            assert tool["inputSchema"]["type"] == "object"
            assert tool["outputSchema"]["type"] == "object"
        assert tools["evaluate_qc_run"]["inputSchema"]["required"] == [
            "analyte",
            "instrument_id",
            "run_id",
            "results",
        ]

    def test_tool_calls_are_authenticated_and_authorized(
        self, client: TestClient, idp: FakeIdp
    ) -> None:
        setup_targets(client, idp)
        agent = bearer(idp, "lims-agent", "qc-agent", scp="qc:ingest qc:read capa:read")
        rejected = mcp(
            client,
            "tools/call",
            {"name": "evaluate_qc_run", "arguments": run_args("m1", 3.4)},
            **agent,
        )["result"]
        assert rejected["isError"] is False
        report = rejected["structuredContent"]
        assert report["evaluations"][0]["outcome"] == "reject"

        holds = mcp(client, "tools/call", {"name": "list_holds", "arguments": {}}, **agent)[
            "result"
        ]
        assert holds["structuredContent"]["items"][0]["analyte"] == "GLU"
        capa = mcp(
            client,
            "tools/call",
            {"name": "get_capa", "arguments": {"capa_id": report["capas_proposed"][0]}},
            **agent,
        )["result"]
        assert capa["structuredContent"]["status"] == "proposed"
        investigation = mcp(
            client,
            "tools/call",
            {
                "name": "get_investigation",
                "arguments": {"investigation_id": report["investigations"][0]},
            },
            **agent,
        )["result"]
        assert investigation["structuredContent"]["hypotheses"]
        chart = mcp(
            client,
            "tools/call",
            {
                "name": "levey_jennings",
                "arguments": {
                    "analyte": "GLU",
                    "instrument_id": INSTRUMENT,
                    "material_id": "GLU-L1",
                },
            },
            **agent,
        )["result"]
        assert len(chart["structuredContent"]["points"]) == 1
        sigma = mcp(
            client,
            "tools/call",
            {
                "name": "sigma_metric",
                "arguments": {
                    "analyte": "GLU",
                    "instrument_id": INSTRUMENT,
                    "material_id": "GLU-L1",
                },
            },
            **agent,
        )["result"]
        assert sigma["isError"] is True  # not enough data yet

        anonymous = mcp(client, "tools/call", {"name": "list_holds", "arguments": {}})["result"]
        assert anonymous["isError"] is True
        out_of_scope = bearer(idp, "narrow-agent", "qc-agent", scp="capa:read")
        denied = mcp(
            client,
            "tools/call",
            {"name": "evaluate_qc_run", "arguments": run_args("m2", 0.0)},
            **out_of_scope,
        )["result"]
        assert denied["isError"] is True
        metrics = client.get("/metrics").text
        assert (
            'labqc_agent_interface_calls_total{interface="mcp",'
            'operation="list_holds",outcome="ok"} 1.0' in metrics
        )
        assert 'outcome="unauthorized"' in metrics


def a2a(client: TestClient, payload: Any, headers: dict[str, str] | None = None) -> Any:
    return client.post("/a2a", headers=headers or {}, json=payload)


def send(parts: list[dict[str, Any]], request_id: int = 7) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "m1",
                "contextId": "ctx-1",
                "parts": parts,
            }
        },
    }


class TestA2a:
    def test_agent_card(self, client: TestClient) -> None:
        card = client.get("/.well-known/agent-card.json").json()
        for field in (
            "name",
            "description",
            "url",
            "version",
            "capabilities",
            "skills",
            "securitySchemes",
        ):
            assert field in card
        assert card["url"] == "https://lab-qc.lab.test/a2a"
        assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
        assert {s["id"] for s in card["skills"]} >= {"qc.evaluate_run", "qc.ingest_hl7", "qc.holds"}

    def test_message_send_routes_to_skills(self, client: TestClient, idp: FakeIdp) -> None:
        setup_targets(client, idp)
        agent = bearer(idp, "lims-agent", "qc-agent", scp="qc:ingest qc:read")
        data = a2a(
            client,
            send([{"kind": "data", "data": {"skill": "qc.evaluate_run", **run_args("a1", 0.2)}}]),
            agent,
        ).json()
        task = data["result"]
        assert data["id"] == 7
        assert task["kind"] == "task"
        assert task["contextId"] == "ctx-1"
        assert task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0]["data"]["evaluations"][0]["outcome"] == "accept"

        hl7_text = hl7_run("A2", 1, 3.5, 0.0)["message"]
        task = a2a(client, send([{"kind": "text", "text": hl7_text}]), agent).json()["result"]
        report = task["artifacts"][0]["parts"][0]["data"]
        assert report["evaluations"][0]["outcome"] == "reject"
        investigation = a2a(
            client,
            send(
                [
                    {
                        "kind": "data",
                        "data": {
                            "skill": "qc.investigation",
                            "investigation_id": report["investigations"][0],
                        },
                    }
                ]
            ),
            agent,
        ).json()["result"]
        assert investigation["status"]["state"] == "completed"
        holds = a2a(client, send([{"kind": "data", "data": {"skill": "qc.holds"}}]), agent).json()[
            "result"
        ]
        assert holds["artifacts"][0]["parts"][0]["data"]["items"][0]["analyte"] == "GLU"
        astm_bare = "H|\\^&|R9||CHEM-01|||||||Q\rL|1|N"
        failed = a2a(client, send([{"kind": "text", "text": astm_bare}]), agent).json()["result"]
        assert failed["status"]["state"] == "failed"
        framed = a2a(client, send([{"kind": "text", "text": "\x02garbage"}]), agent).json()[
            "result"
        ]
        assert framed["status"]["state"] == "failed"

    def test_policy_rejection_is_a_rejected_task(self, client: TestClient, idp: FakeIdp) -> None:
        auditor = bearer(idp, "ari@lab.test", "qc-auditor")
        task = a2a(
            client,
            send([{"kind": "data", "data": {"skill": "qc.evaluate_run", **run_args("a3", 0.0)}}]),
            auditor,
        ).json()["result"]
        assert task["status"]["state"] == "rejected"
        assert "denied" in task["status"]["message"]["parts"][0]["text"]

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ([1, 2], -32600),
            ({"jsonrpc": "1.0", "method": "x"}, -32600),
            ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "x"}, -32600),
            ({"jsonrpc": "2.0", "id": 1}, -32600),
            ({"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel"}, -32601),
            ({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "t"}}, -32001),
            ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}, -32602),
            (send([{"kind": "text", "text": "hello"}]), -32602),
            (send([{"kind": "data", "data": {"skill": "qc.unknown"}}]), -32602),
            (
                send([{"kind": "data", "data": {"skill": "qc.evaluate_run", "analyte": "GLU"}}]),
                -32602,
            ),
            (send([{"kind": "data", "data": {"skill": "qc.investigation"}}]), -32602),
        ],
    )
    def test_json_rpc_errors(
        self, client: TestClient, idp: FakeIdp, payload: Any, code: int
    ) -> None:
        response = a2a(
            client, payload, bearer(idp, "lims-agent", "qc-agent", scp="qc:read qc:ingest")
        )
        assert response.status_code == 200
        assert response.json()["error"]["code"] == code

    def test_parse_error_and_authentication(self, client: TestClient) -> None:
        parse = client.post(
            "/a2a", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert parse.json()["error"]["code"] == -32700
        unauthenticated = a2a(client, send([{"kind": "data", "data": {"skill": "qc.holds"}}]))
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"] == "Bearer"
