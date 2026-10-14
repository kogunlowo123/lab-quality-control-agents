"""Runtime settings, read from LABQC_* environment variables."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SPIFFE = r"^spiffe://[a-z0-9.\-_]+/agent/[a-z0-9][a-z0-9\-_.]*/[a-z0-9][a-z0-9\-_.]*$"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LABQC_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="External URL of this service (A2A agent card)")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    audit_log_path: Path = Path("/var/lib/lab-qc/audit/audit.jsonl")
    audit_key_file: Path | None = Field(default=None, description="Ed25519 PEM for audit signing")
    dev_ephemeral_audit_key: bool = Field(
        default=False,
        description="Generate an in-memory audit key at start-up. Refused in production.",
    )

    oidc_issuer: str = Field(description="Enterprise OIDC issuer for staff and service principals")
    oidc_audience: str = Field(description="Audience tokens for this service must carry")
    oidc_jwks_url: str
    roles_claim: str = "roles"
    name_claim: str = "name"

    agent_id: str = Field(pattern=_SPIFFE, description="SPIFFE ID the internal QC agents act under")
    agent_scopes: str = Field(
        default="qc:read investigation:run capa:propose capa:read capa:verify",
        description="Scopes of the internal agents when no identity fabric is configured",
    )
    identity_fabric_url: HttpUrl | None = Field(
        default=None, description="agent-identity-fabric base URL; enables fabric credentials"
    )
    workload_token_file: Path | None = Field(
        default=None, description="Projected service-account token exchanged at the fabric"
    )

    policy_file: Path = Path("policies/lab-qc.yaml")
    analytes_file: Path = Path("configs/analytes.yaml")
    providers_file: Path | None = Field(
        default=None, description="Model providers for investigation narratives; unset disables"
    )

    site_timezone: str = Field(default="UTC", description="IANA zone for timestamps without offset")
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*"],
        description="Host headers accepted by the MCP endpoint (DNS rebinding protection)",
    )
    investigation_lookback_hours: int = Field(default=72, ge=1, le=720)
    target_min_points: int = Field(default=20, ge=2, le=200)
    sigma_min_points: int = Field(default=20, ge=2, le=500)
    verification_min_runs: int = Field(default=1, ge=1, le=10)
    verification_min_runs_systematic: int = Field(default=2, ge=1, le=10)

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @field_validator("site_timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Settings:
        if self.dev_ephemeral_audit_key:
            if self.environment == "production":
                raise ValueError("dev_ephemeral_audit_key cannot be used in production")
        elif self.audit_key_file is None:
            raise ValueError("LABQC_AUDIT_KEY_FILE is required (or a non-production ephemeral key)")
        if self.identity_fabric_url is not None and self.workload_token_file is None:
            raise ValueError("LABQC_WORKLOAD_TOKEN_FILE is required with LABQC_IDENTITY_FABRIC_URL")
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.site_timezone)

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")

    @property
    def fabric_url_str(self) -> str | None:
        return str(self.identity_fabric_url).rstrip("/") if self.identity_fabric_url else None
