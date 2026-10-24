# GitHub Copilot Instructions

## Project purpose

This repository is for an incremental Python/Django web application for transaction-level withholding-tax classification, calculation, review, approval and audit tracking.

The core product is a business application. It is not a chatbot or AI-agent project.

Machine learning may eventually provide classification suggestions, but ML is not part of the initial MVP.

## Mandatory development method

The application owner is building this project incrementally and wants to understand the implementation.

Therefore:

- Do not build the whole application at once.
- Work only on the explicitly requested step.
- Do not implement future phases unless requested.
- Identify the files that will be created or modified.
- Keep implementations simple, practical and understandable.
- Prefer one recommended approach instead of presenting many alternatives.
- Do not introduce unnecessary frameworks, dependencies or abstractions.
- After implementing a step, run the relevant tests, report the result and stop.
- Do not interpret a broad project description as permission to build the entire application.

## Change approval

For changes involving multiple application components, database
schema changes, new dependencies, security-sensitive behavior,
or substantial refactoring:

1. Inspect the relevant existing code.
2. Explain the proposed approach and why it is needed.
3. List the files likely to change, tests, and key risks.
4. Stop and wait for explicit approval before implementing.

For small, clearly specified changes, proceed directly when
the request authorizes implementation.

Never treat a roadmap or broad project description as
authorization to implement future phases.

## Environment and destructive operations

Never delete project data, reset Git history, discard existing
user changes, drop databases, or execute destructive database
operations without explicit approval.

Do not deploy, run production migrations, or change external
systems unless the requested task explicitly authorizes it.

Before running any unfamiliar command, explain its purpose
and identify material side effects.

## Data-first architecture

The repository contains:

`data/Synthetic_WHT_Demo_Data.xlsx`

This workbook contains synthetic prototype data based on the expected structure of the business datasets.

Before creating database models, inspect the actual workbook structure.

Do not infer database relationships merely because columns have similar names.

In particular:

- Material Number is not necessarily unique.
- A material can occur on multiple Purchasing Documents and Purchasing Document Items.
- Purchasing Document plus Item may be an important relationship, but it must be validated against the supplied data.
- Additional fields may be necessary to create a reliable business key.
- Never silently select the first match when multiple records satisfy a relationship.

When analyzing data, distinguish between:

1. Reference/master data
2. Source transaction data
3. Classification decisions
4. Derived/calculated results
5. Human review information
6. Audit/history information

If the user requests data analysis, analyze only. Do not create Django models or application code unless explicitly instructed.

## Business process

The overall process is expected to resemble:

Open Item<br>
→ Purchasing / Material information<br>
→ Cost Description<br>
→ Cost Type<br>
→ WHT determination<br>
→ WHT Rate<br>
→ WHT Amount<br>
→ Review<br>
→ Approval

Do not invent business or tax rules that are not represented in the provided data or explicitly explained by the user.

If the data does not establish a rule, identify the uncertainty instead of guessing.

## Tax calculation requirements

Tax calculations must use Python `Decimal`.

Do not use binary floating-point arithmetic for:

- WHT rates
- Tax bases
- WHT amounts
- Monetary calculations

Tax rules must not be scattered through views or templates.

When implemented, business-rule evaluation must be isolated in focused service modules.

Do not invent tax rates, bases, matching criteria or WHT treatments.

## Relationships and ambiguity

Every proposed business key or unique constraint must be supported by the analyzed data and confirmed by the business owner.

The system must explicitly handle:

- No match
- Exactly one match
- Multiple possible matches

Multiple matches are an exception condition. Never resolve them with `.first()`, arbitrary ordering, or another silent selection.

Preserve leading zeros and source identifiers as text where appropriate.

## Historical decisions

The future application must distinguish between:

- System suggestion
- Human final decision

A future machine-learning suggestion must never overwrite the human final classification.

The application should eventually retain enough historical information to determine:

- What classification was made
- Who made it
- When it was made
- Which WHT rule and rate were applied
- Whether a suggestion was overridden
- Why an override occurred

Historical approved results must not silently change because reference data or tax rules are subsequently updated.

## Future machine learning

Machine learning is explicitly out of scope for the initial MVP.

Architecture decisions should nevertheless avoid preventing future supervised-learning use cases.

Potential labels may include:

- Final Cost Type
- Final WHT treatment
- Reviewer correction
- Override reason

Potential features may come from source transactions and purchasing/material data.

Do not build an ML model unless explicitly requested.

Do not treat an ML suggestion as a final tax decision.

## Coding principles

When coding begins:

- Use Python type hints for business-service functions.
- Use clear business-oriented names.
- Keep functions focused.
- Write tests for business logic.
- Use database constraints only for validated business constraints.
- Use Django migrations for schema changes.
- Keep business logic out of templates.
- Keep tax-rule logic out of views.
- Avoid premature optimization.
- Never add a dependency without explaining why it is required.
- Never claim completion while relevant tests are failing.

## Synthetic data and security

Use only synthetic data during development.

Do not add:

- Real company transaction data
- Real supplier data
- Real employee data
- Credentials
- Passwords
- API tokens
- Secrets

Do not commit `.env` files.

Do not assume that local prototype data or code is approved for production deployment.

## Interaction rules

If the user asks you to analyze the data:

- Read the specification and workbook.
- Analyze the data only.
- Explain the table grain, candidate keys and relationships.
- Identify ambiguity and unresolved questions.
- Do not start implementing the application.

If the user asks you to propose architecture:

- Propose architecture only.
- State assumptions clearly.
- Do not automatically implement it.

If the user asks you to implement one phase:

- Implement only that phase.
- Add or update tests.
- Run the tests.
- Report what changed.
- Stop before starting another phase.
