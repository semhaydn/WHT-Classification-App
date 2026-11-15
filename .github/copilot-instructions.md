# Development instructions

These instructions apply to all work in this repository. This is an incremental
Python/Django business application for transaction-level withholding-tax
classification, calculation, review, approval, and audit tracking. It is not a
chatbot or an AI-agent application.

## Before changing anything

- Inspect the relevant code, tests, migrations, data, and documentation before
  proposing or making a change. Verify current behavior in code and tests;
  documentation may be stale.
- Work only on the step explicitly requested. Do not restart the project,
  redesign functioning components, or implement roadmap/future-phase items
  without a separate request.
- For substantial changes, identify the affected files and explain the approach,
  tests, and important risks before implementation. Stop for approval when a
  change spans multiple application components, changes database schema, adds a
  dependency, changes security-sensitive behavior, or requires substantial
  refactoring.
- Preserve existing behavior and confirmed business rules. If a requirement
  conflicts with implementation, investigate and make the discrepancy explicit
  rather than silently changing behavior.

## Authoritative project documentation

- [Business rules](../docs/BUSINESS_RULES.md) is the source of truth for
  classification and WHT behavior.
- [Architecture](../docs/ARCHITECTURE.md) describes the verified current
  application.
- [ML strategy](../docs/ML_STRATEGY.md) and [development roadmap](../docs/DEVELOPMENT_ROADMAP.md)
  describe future direction only; neither authorizes implementation.
- [Data dictionary](../docs/DATA_DICTIONARY.md) describes the current synthetic
  CSV fixtures and data model.
- `PROJECT_SPECIFICATION.md` records broader requirements and proposals. Where
  it describes current behavior, prefer the verified current-state documents
  above.

## Business and data safeguards

- Never invent tax rules, rates, bases, matching criteria, or treatments. Raise
  uncertainty instead of guessing.
- Preserve the confirmed Supplier ID + Supplier Country + Final Cost Type WHT
  lookup key, latest-approved-version selection independent of SAP Posting
  Date, Decimal arithmetic, and `ROUND_HALF_UP` rounding.
- Keep system suggestions separate from the human Final Cost Type. ML
  predictions must never overwrite human decisions or make autonomous tax
  decisions.
- Handle no match, one match, and multiple matches explicitly. Never silently
  choose the first or arbitrarily ordered match when a relationship is
  ambiguous.
- Preserve source identifiers and leading zeros as text where appropriate.
- Use only synthetic data during development. Never use real company,
  supplier, employee, transaction, or tax-sensitive data without explicit
  authorization. Do not add credentials, tokens, secrets, or `.env` files.
- Do not assume local code or synthetic data is approved for production.

## Implementation and verification

- Keep changes surgical, understandable, and within the requested phase. Follow
  existing Django, service, naming, formatting, and testing patterns.
- Identify affected files before substantial changes. Do not change models,
  migrations, database configuration, or application behavior unless requested.
- Keep tax and business-rule logic in focused service modules, not views or
  templates. Use Python type hints for business services.
- Use `Decimal` for rates and monetary calculations; do not use binary
  floating-point arithmetic for tax or money.
- Do not add dependencies or frameworks unless explicitly requested and
  justified.
- Add focused regression tests for behavior changed or fixed. Run the smallest
  relevant checks and tests; report failures and limitations honestly. Do not
  claim completion while relevant tests fail.
- Update directly related documentation. Report what changed, test results, and
  stop after the requested step.

## Environment and operations

- Do not delete project data, discard user changes, reset Git history, drop a
  database, deploy, run production migrations, or modify external systems
  without explicit authorization.
- Do not treat a roadmap, broad project description, or future architecture
  document as authorization to build additional phases.
- Keep architecture choices simple and explain them clearly; the project owner
  is building the application incrementally.
