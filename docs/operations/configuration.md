# Configuration reference

All settings are `LABQC_*` environment variables (see
`src/lab_quality_control_agents/config.py`). The service refuses to start
with incomplete configuration.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LABQC_ENVIRONMENT` | no | `production` | `production` forbids the ephemeral audit key |
| `LABQC_PUBLIC_URL` | yes | | External URL; used in the A2A agent card |
| `LABQC_DATABASE_URL` | yes | | SQLAlchemy async URL (`postgresql+asyncpg://...`) |
| `LABQC_AUDIT_KEY_FILE` | yes* | | Ed25519 PEM for audit signatures (key ID = file stem) |
| `LABQC_DEV_EPHEMERAL_AUDIT_KEY` | no | `false` | In-memory audit key, non-production only (replaces the * setting) |
| `LABQC_AUDIT_LOG_PATH` | no | `/var/lib/lab-qc/audit/audit.jsonl` | Audit chain file |
| `LABQC_OIDC_ISSUER` | yes | | Enterprise OIDC issuer (Entra ID tenant) |
| `LABQC_OIDC_AUDIENCE` | yes | | Audience of tokens for this service |
| `LABQC_OIDC_JWKS_URL` | yes | | Issuer JWKS URL |
| `LABQC_ROLES_CLAIM` / `LABQC_NAME_CLAIM` | no | `roles` / `name` | Claims carrying app roles and printed name |
| `LABQC_AGENT_ID` | yes | | SPIFFE ID of the internal QC agents |
| `LABQC_AGENT_SCOPES` | no | `qc:read investigation:run capa:propose capa:read capa:verify` | Agent scopes without the fabric |
| `LABQC_IDENTITY_FABRIC_URL` | no | | Enables fabric introspection/authorization and agent credentials |
| `LABQC_WORKLOAD_TOKEN_FILE` | with fabric | | Projected service-account token for the jwt-bearer grant |
| `LABQC_POLICY_FILE` | no | `policies/lab-qc.yaml` | Authorization policy |
| `LABQC_ANALYTES_FILE` | no | `configs/analytes.yaml` | Analyte catalogue (codes, LOINC, aliases, TEa, calibration interval) |
| `LABQC_PROVIDERS_FILE` | no | | Model providers for narratives; unset disables them |
| `LABQC_SITE_TIMEZONE` | no | `UTC` | IANA zone for instrument timestamps without an offset |
| `LABQC_MCP_ALLOWED_HOSTS` | no | `["127.0.0.1:*","localhost:*"]` | JSON list of Host headers accepted on `/mcp/` |
| `LABQC_INVESTIGATION_LOOKBACK_HOURS` | no | `72` | Event window before a rejected run |
| `LABQC_TARGET_MIN_POINTS` | no | `20` | Minimum baseline values to establish a target |
| `LABQC_SIGMA_MIN_POINTS` | no | `20` | Minimum results for a sigma metric |
| `LABQC_VERIFICATION_MIN_RUNS` | no | `1` | Complete verification runs for random error |
| `LABQC_VERIFICATION_MIN_RUNS_SYSTEMATIC` | no | `2` | Complete verification runs for systematic error |
| `LABQC_LOG_LEVEL` | no | `INFO` | |
| `LABQC_OTLP_ENDPOINT` | no | | OTLP gRPC endpoint for traces |
| `LABQC_HOST` / `LABQC_PORT` | no | `127.0.0.1` / `8080` | The image sets host `0.0.0.0` |
| `LABQC_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |

## Model provider variables (when `LABQC_PROVIDERS_FILE=configs/providers.yaml`)

`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_REGION`,
`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_INPUT_USD_PER_MTOK`,
`AZURE_OPENAI_OUTPUT_USD_PER_MTOK`, `AZURE_AI_FOUNDRY_ENDPOINT`,
`AZURE_AI_FOUNDRY_API_KEY`, `AZURE_AI_FOUNDRY_MODEL`,
`AZURE_AI_FOUNDRY_INPUT_USD_PER_MTOK`, `AZURE_AI_FOUNDRY_OUTPUT_USD_PER_MTOK`.
Loading fails if any is unset. Prices are the laboratory's contracted rates.
