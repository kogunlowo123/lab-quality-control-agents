"""Service metrics exposed on /metrics in Prometheus text format."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class QcMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "labqc_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self.results_ingested = Counter(
            "labqc_results_ingested_total",
            "QC results stored, by source",
            ["source"],
            registry=self.registry,
        )
        self.results_rejected = Counter(
            "labqc_results_unmatched_total",
            "QC observations not stored (no target, unit mismatch, unknown analyte)",
            ["source", "reason"],
            registry=self.registry,
        )
        self.messages_failed = Counter(
            "labqc_messages_failed_total",
            "Inbound HL7/ASTM messages refused as malformed or non-QC",
            ["source"],
            registry=self.registry,
        )
        self.run_outcomes = Counter(
            "labqc_run_outcomes_total",
            "Westgard run evaluations by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.violations = Counter(
            "labqc_rule_violations_total",
            "Westgard rule violations by rule",
            ["rule"],
            registry=self.registry,
        )
        self.investigations = Counter(
            "labqc_investigations_total",
            "Root cause investigations by leading cause",
            ["cause"],
            registry=self.registry,
        )
        self.capa_transitions = Counter(
            "labqc_capa_transitions_total",
            "CAPA state transitions",
            ["status"],
            registry=self.registry,
        )
        self.narratives = Counter(
            "labqc_narratives_total",
            "Investigation narratives by outcome (generated, guarded, ungrounded, unavailable)",
            ["outcome"],
            registry=self.registry,
        )
        self.active_holds = Gauge(
            "labqc_active_holds",
            "Analyte/instrument pairs with patient-result release on hold",
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "labqc_agent_interface_calls_total",
            "MCP tool and A2A calls by interface, operation and outcome",
            ["interface", "operation", "outcome"],
            registry=self.registry,
        )
