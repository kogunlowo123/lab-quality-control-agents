# Contributing

## Setup

```bash
uv sync
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, contract drift,
  tests with at least 90% branch coverage.
- New behaviour has tests. Changes to QC rules, statistics or verification
  rules add or update worked examples in `tests/unit` and
  `docs/architecture/qc-methods.md`. Security controls need adversarial
  cases in `tests/security`.
- Public API changes regenerate contracts
  (`uv run python -m lab_quality_control_agents.contracts`) and are recorded
  in `CHANGELOG.md`.
- Architecturally significant decisions get an ADR in
  `docs/architecture/adr/`.
- No credentials, customer data or patient data in code, tests or fixtures.
  Use synthetic control data only.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/lab_quality_control_agents/security`,
`src/lab_quality_control_agents/domain` (QC rules), `policies/`,
`configs/analytes.yaml` or `deployments/` need approval from a code owner
(see `CODEOWNERS`). Changes to QC rules or the analyte catalogue also need
sign-off from the laboratory's QC supervisor before release.
