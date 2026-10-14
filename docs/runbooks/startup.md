# Runbook: startup (Azure reference deployment)

## Prerequisites

- AKS cluster with the OIDC issuer and Workload Identity enabled, External
  Secrets Operator with a `ClusterSecretStore` named `platform-secrets`
  bound to the Key Vault created below, Prometheus Operator (optional).
- A resource group, a subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers`
  and the private DNS zone `privatelink.postgres.database.azure.com`.
- An Entra ID app registration for the service with app roles
  `qc-technologist`, `qc-supervisor`, `qc-auditor`, `qc-interface`, `qc-agent`.
- Optional: agent-identity-fabric with the QC agents registered under the
  SPIFFE ID you will set as `labqc.agentId`, workload binding
  `system:serviceaccount:lab-qc:lab-quality-control-agents`.

## 1. Infrastructure

```bash
cd deployments/terraform/azure
terraform init
terraform apply -var subscription_id=<subscription> -var resource_group_name=<rg> \
  -var db_subnet_id=<subnet-id> -var db_private_dns_zone_id=<zone-id> \
  -var aks_oidc_issuer_url="$(az aks show -g <rg> -n <cluster> --query oidcIssuerProfile.issuerUrl -o tsv)"
```

Outputs: `workload_identity_client_id`, `key_vault_uri`, `audit_container`.

## 2. Secrets outside Terraform

The audit signing key never passes through Terraform state:

```bash
openssl genpkey -algorithm ed25519 -out audit-2026-10.pem
az keyvault secret set --vault-name <vault> --name lab-quality-control-agents-audit-key-2026-10 \
  --file audit-2026-10.pem
shred -u audit-2026-10.pem
```

If narratives are enabled, store each provider variable as
`labqc-provider-<NAME-WITH-DASHES>` (for example
`labqc-provider-AZURE-OPENAI-ENDPOINT`); External Secrets turns it into
`AZURE_OPENAI_ENDPOINT`.

## 3. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n lab-qc get externalsecrets
```

## 4. Install

```bash
helm install labqc oci://ghcr.io/kogunlowo123/charts/lab-quality-control-agents --version 0.1.0 \
  -n lab-qc -f <env>.yaml
```

`<env>.yaml` sets every REQUIRED value in `values.yaml`, the service account
annotation `azure.workload.identity/client-id` (Terraform output) and the
pod label `azure.workload.identity/use: "true"`.

## 5. Load configuration

1. Supervisors set targets for every control material (`PUT /v1/qc/targets`
   or `POST /v1/qc/targets/establish` from 20 baseline values).
2. Confirm every analyte the middleware sends is in `configs/analytes.yaml`
   (LOINC code or local alias).
3. Point the middleware at `POST /v1/qc/hl7` (or `/v1/qc/astm`) with a
   `qc-interface` client-credentials token.
4. Configure the LIS to read `GET /v1/holds` before autoverification and to
   hold any analyte/instrument pair listed. Validate this with a deliberate
   control failure before go-live (risk R1).

## 6. Verify

```bash
kubectl -n lab-qc rollout status statefulset/labqc-lab-quality-control-agents
curl -sf https://<host>/readyz
curl -sf https://<host>/.well-known/agent-card.json | jq .skills[].id
```

Then post a normal and a failing QC run in a test instrument ID and walk
the CAPA cycle (`docs/runbooks/qc-failure.md`).
