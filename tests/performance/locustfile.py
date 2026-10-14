"""Load profile for a deployed service.

Run against a staging deployment, never production. Tokens come from the
staging identity provider (an interface-engine token with role qc-interface
and a technologist token with role qc-technologist):

    LABQC_LOAD_INTERFACE_TOKEN=<token> LABQC_LOAD_READER_TOKEN=<token> \
    locust -f tests/performance/locustfile.py --host https://<staging-host> \
           --users 100 --spawn-rate 10 --run-time 10m --headless

The mix mirrors a core laboratory: every analyser posts QC runs a few times
per shift, while LIS checks of the hold list and dashboard reads dominate.
Run IDs are unique per request so every submission is evaluated.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime

from locust import HttpUser, between, task

INTERFACE = os.environ.get("LABQC_LOAD_INTERFACE_TOKEN", "")
READER = os.environ.get("LABQC_LOAD_READER_TOKEN", "")
INSTRUMENTS = [f"CHEM-{n:02d}" for n in range(1, 9)]


class LaboratoryTraffic(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(2)
    def submit_run(self) -> None:
        now = datetime.now(UTC).isoformat()
        z1, z2 = random.gauss(0, 1), random.gauss(0, 1)
        self.client.post(
            "/v1/qc/runs",
            json={
                "analyte": "GLU",
                "instrument_id": random.choice(INSTRUMENTS),
                "run_id": f"load-{uuid.uuid4().hex[:16]}",
                "results": [
                    {
                        "material_id": "GLU-L1",
                        "value": 100 + 2.5 * z1,
                        "units": "mg/dL",
                        "observed_at": now,
                    },
                    {
                        "material_id": "GLU-L2",
                        "value": 300 + 6 * z2,
                        "units": "mg/dL",
                        "observed_at": now,
                    },
                ],
            },
            headers={"Authorization": f"Bearer {INTERFACE}"},
            name="/v1/qc/runs",
        )

    @task(10)
    def holds(self) -> None:
        self.client.get(
            "/v1/holds", headers={"Authorization": f"Bearer {READER}"}, name="/v1/holds"
        )

    @task(3)
    def rejected_runs(self) -> None:
        self.client.get(
            "/v1/qc/runs?outcome=reject&limit=50",
            headers={"Authorization": f"Bearer {READER}"},
            name="/v1/qc/runs",
        )
