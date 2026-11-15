# WHT Classification and Review Application

> **Current documentation authority:** This specification retains the broader
> product requirements and proposals. For verified current architecture, rules,
> data, and roadmap status, use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
> [docs/BUSINESS_RULES.md](docs/BUSINESS_RULES.md),
> [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md), and
> [docs/DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md). The business rules
> document is authoritative for current classification and WHT behavior.

## 1. Project purpose

Build an internal web application for transaction-level withholding tax classification, calculation, review, approval, and audit tracking.

The application replaces an Excel-based operational process used to review SAP/open-item transactions and determine:

- Cost Description
- Cost Type
- WHT applicability
- WHT rate
- WHT amount
- Whether human review is required
- Final reviewer decision

This is not a chatbot or AI-agent application.

The core product is a structured transaction classification and review application. Machine learning may be added later, but ML is not part of the initial MVP.

## 2. Data discovery and prototype data

The business data discovery was completed before application implementation. The workbook analysis established the prototype dataset grains and relationships, and the business owner confirmed the material and WHT matching rules documented below. Do not repeat discovery as a prerequisite to implementing the remaining requested phases.

The current repository contains synthetic CSV fixtures under `data/wht_synthetic_data/`, not the original business workbooks. These fixtures model the source structures and are for development/testing only. The imported CSVs are not approved tax guidance.

The confirmed material business key is Purchasing Document + Purchasing Document Item. Material Number is not a key. The current synthetic material CSV contains repeated keys. The importer uses the last occurrence for each repeated key and warns that earlier rows were skipped; the database still enforces uniqueness for stored Purchasing Document + Item values.

The confirmed WHT lookup key is Supplier + Supplier Country + Final Cost Type. The checked-in synthetic WHT CSV has been corrected to remove duplicate keys; uploads with repeated keys are rejected in full.

Non-zero accounting lines are eligible for classification by default. An Administrator may explicitly configure a G/L account as eligible or excluded; zero-amount lines are always excluded. The additive seed migration explicitly excludes the existing invoice/control (`210000`) and posted-WHT (`229220`) accounts. Eligibility changes do not rewrite imported lines or recalculate existing classifications, WHT determinations, review statuses, or completed snapshots.

Transaction Users may flag lines for Administrator review. Administrators resolve flags with a recorded decision or exclude a line with a reason. WHT exceptions remain derived from persisted determinations; an Administrator must explicitly recalculate after shared reference corrections or record a one-off rate / explicit No WHT decision. Invoice snapshots are created only after all required lines have been reviewed and no required completion blockers remain. Administrators may reopen one required line or the entire invoice, which marks the latest snapshot historical without changing its captured business values and resets the applicable lines to classification. Re-completion creates a new monotonically increasing snapshot version and records an audit event. Historical snapshots remain accessible from invoice history and read-only in Django Admin.

The primary workflow destinations are Active Invoices, Completed Invoices, and Administrator-only Exceptions. Active and completed lists are grouped from accounting lines and current snapshots respectively, use server-side filtering and pagination, and export a single curated invoice row per document. Completed list/export financial and supplier values come from the captured snapshot. Read-only list and export operations do not recalculate determinations or modify snapshot history.

## 3. Development approach

The application must be built incrementally.

Do not generate the complete application in one operation.

For each development step:

1. Explain what will be built.
2. Explain briefly why it is needed.
3. Identify the files that will be created or modified.
4. Make only the changes required for the current step.
5. Add or update tests.
6. Run the relevant tests.
7. Report the result.
8. Stop before starting the next phase.

Do not silently redesign the application.

If a requirement is unclear, state the assumption before implementing it.

Prefer one clear recommended implementation over presenting many alternatives.

## 4. Proposed technology stack

The current recommended stack is:

- Python
- Django
- Django ORM
- Django templates
- Bootstrap for basic styling
- SQLite during local development
- PostgreSQL-compatible model design for future deployment
- pytest and pytest-django
- Git for version control

This is the current architectural direction, but the Django project should not be initialized until the supplied synthetic data has been analyzed and the initial data model has been reviewed.

Do not introduce React, FastAPI, Celery, Redis, Docker, machine-learning libraries, cloud services, external APIs, or a separate frontend architecture unless a later requirement justifies them.

## 5. Security and data restrictions

The initial project uses synthetic data only.

Do not include:

- Real company names
- Real vendor names
- Real employee information
- Real SAP exports
- Real invoices
- Real tax-sensitive data
- Credentials, tokens, connection strings, or secrets

Use environment variables for configuration that could later contain sensitive values.

Add `.env`, local database files, exported data, and generated files to `.gitignore` where appropriate.

Do not log complete transaction records or potentially sensitive descriptions.

Future production authentication and deployment must not be assumed. Microsoft Entra ID and corporate hosting may be evaluated later, subject to company security and architecture approval.

## 6. High-level workflow

The long-term workflow is:

SAP/open-item source<br>
→ Transaction record<br>
→ Purchasing and material information<br>
→ Cost Description<br>
→ Cost Type classification<br>
→ WHT rule evaluation<br>
→ WHT rate<br>
→ WHT amount<br>
→ Review and approval<br>
→ Dashboard and reporting<br>
→ Future ML feedback loop

The proposed MVP workflow is:

1. Load synthetic transactions.
2. Display an open-items list.
3. Open one transaction.
4. Display related purchasing/material data.
5. Allow the user to select a Final Cost Type.
6. Evaluate a matching WHT rule.
7. Calculate the WHT amount.
8. Save the decision as a historical snapshot.
9. Submit the transaction for review.
10. Approve or return the transaction.
11. Record material decisions in an audit history.

This workflow remains subject to validation against the synthetic workbook.

## 7. Architectural principles

The data model must distinguish between the following categories.

### 7.1 Reference and master data

Reusable definitions that may be referenced by multiple records, potentially including:

- Country
- Cost Type
- Vendor, if introduced
- WHT rule definitions

### 7.2 Source transaction data

Imported information describing an individual accounting line, potentially including:

- Entity
- Fiscal Year
- Accounting Document Number
- Line Number
- Document Date
- Posting Date
- Vendor identifier
- Vendor country
- Transaction amount
- Currency
- Purchasing Document
- Purchasing Document Item
- Material
- GL Account
- Transaction description

Actual fields must be confirmed from the workbook.

### 7.3 Classification decision

The classification proposed or selected for a specific transaction:

- Suggested Cost Type
- Final Cost Type
- Classification Method
- Classification Reason
- Decision timestamp
- Decision maker

### 7.4 Derived result

A result calculated by applying business logic:

- WHT applicable
- WHT rate
- WHT base
- WHT amount
- Matched rule
- Calculation timestamp

### 7.5 Review and audit information

Human workflow and historical evidence:

- Workflow status
- Assigned reviewer
- Submitted by
- Submitted at
- Reviewed by
- Reviewed at
- Review result
- Review comments
- Override reason
- Decision history

## 8. References versus historical snapshots

Use references for stable reusable definitions.

Store historical snapshots for values that explain the tax decision at the time it was made.

Potential references include:

- Country
- Final Cost Type
- Matched WHT rule

Potential decision snapshots include:

- Applied rule version
- Applied WHT rate
- Calculated WHT amount
- Cost Description at the time of decision
- Vendor country code at the time of decision
- Classification method
- Decision timestamp

A WHT rule or reference description may change later. A previously approved transaction must continue to show what rule, rate, and relevant values were used at the time of approval.

Never recalculate an approved historical transaction merely because reference data changed. Recalculation must be an explicit action and must create an audit event.

## 9. Candidate business keys

All candidate keys must be validated against the supplied workbook before they become database constraints.

### 9.1 Open Item candidate key

A likely business key may include:

- Entity Code
- Fiscal Year
- Accounting Document Number
- Line Number

Conceptual example:

`ENTITY-FISCAL_YEAR-DOCUMENT_NUMBER-LINE_NUMBER`

The database may use an internal numeric or UUID primary key, while enforcing a separate validated business-key constraint.

### 9.2 Confirmed MaterialData relationship

The business owner confirmed that Purchasing Document + Purchasing Document Item uniquely identifies a MaterialData record. A database uniqueness constraint enforces this key. Material Number is not unique.

The system handles:

- No matching MaterialData record
- Exactly one matching MaterialData record
- Multiple possible matching MaterialData records

The database enforces uniqueness. For duplicate keys within a MaterialData CSV, the current importer keeps the last occurrence and warns about earlier rows. This import policy does not permit silently selecting among multiple matching stored records: zero, one, and multiple match outcomes remain explicit, and the application must never arbitrarily choose a match.

The latest match status is persisted per open item and changes are written to audit history. A no-match exception remains visible on the transaction detail page. Only corrected purchasing data clears it; users do not manually link a material record.

Match statuses are:

- `NOT_FOUND`
- `MATCHED`
- `AMBIGUOUS`

### 9.3 Confirmed WHT rule key and lifecycle

The business owner confirmed the matching key: Supplier ID + Supplier Country + Final Cost Type. Entity is not part of the key in the one-entity pilot. Rules retain version, effective-from/effective-to metadata, draft/approved status, approver, and approval timestamp. New determinations use the latest approved version for the exact key; SAP Posting Date and effective dates do not select or fall back between rules.

WHT rules are maintained in Django admin by staff in the Administrator group. Imported mappings are drafts and cannot be used for calculation until an Administrator supplies required approval metadata and approves them. Approved rules are immutable; create another version for changes. A missing or ambiguous exact mapping requires review for every country. A zero-rate approved mapping means No WHT; absence of a mapping never means No WHT.

Only supplier, country, final Cost Type, approved status, and version determine a match. The source mapping format has no separate WHT Treatment field, so a No WHT treatment paired with a positive rate cannot be validated until an authoritative treatment field is introduced.

## 10. Proposed Django application boundaries

These are proposed boundaries and must be reviewed after data discovery:

### `reference_data`

Contains:

- SupplierCountry
- CostType
- WHTMapping

### `transactions`

Contains:

- OpenItem
- MaterialData
- MaterialMatch
- ClassificationDecision
- WHTDetermination
- ReviewAction
- AuditEvent

Review workflow data currently lives in the `transactions` application; there is no separate `reviews` application.
- AuditEvent

Do not create all models immediately. Implement them phase by phase after validating the architecture.

## 11. Proposed workflow statuses

Use controlled values, potentially through Django `TextChoices`:

- `NEW`
- `IN_CLASSIFICATION`
- `READY_FOR_REVIEW`
- `UNDER_REVIEW`
- `APPROVED`
- `RETURNED`
- `CANCELLED`

Proposed transitions:

- NEW → IN_CLASSIFICATION
- IN_CLASSIFICATION → READY_FOR_REVIEW
- READY_FOR_REVIEW → UNDER_REVIEW
- UNDER_REVIEW → APPROVED
- UNDER_REVIEW → RETURNED
- RETURNED → IN_CLASSIFICATION

These transitions must be confirmed with the business owner before implementation.

Do not permit arbitrary status changes through direct form editing. Implement transitions through explicit service functions.

## 12. Classification requirements

A transaction classification should preserve:

- Suggested Cost Type
- Final Cost Type
- Classification Method
- Classification Reason
- Classified By
- Classified At

Potential classification methods:

- `HUMAN`
- `RULE`
- `IMPORT`
- `ML_SUGGESTION`
- `HUMAN_OVERRIDE`

The MVP normally uses `HUMAN` unless the supplied data establishes an existing rules-based suggestion.

Suggested Cost Type and Final Cost Type must remain separate.

A future ML prediction must never overwrite the human Final Cost Type.

## 13. WHT calculation requirements

The confirmed calculation for each non-zero classifiable open-item line is:

`WHT Amount = Signed document-currency amount × WHT Rate`

Positive and negative source amounts retain their signs. No currency conversion is performed. The latest approved exact-key mapping version is used regardless of the transaction's Posting Date. This confirmed prototype behavior does not establish production tax advice.

Use decimal fields and Python `Decimal`, not binary floating-point values.

The business owner confirmed this rate and rounding convention:

- Store fractional rates, such as `0.10` for 10%.
- Round to two document-currency decimal places using `ROUND_HALF_UP`.

Rounding must use Python `Decimal` and an explicitly defined rounding method.

Reconciliation uses persisted WHT determinations and signed G/L `229220` document-currency postings:

`Difference = Calculated WHT + Signed Posted WHT`

Round the difference to two decimal places before tolerance. Absolute differences up to and including `0.01` are reconciled; larger differences are discrepancies. No posted-WHT lines means zero. Missing required determinations produce Pending / Provisional status; explicit excluded lines do not block final reconciliation.

## 14. Desired screens

### 14.1 Home dashboard

Display:

- Open items
- Items awaiting review
- My reviews
- Approved items
- Returned items

### 14.2 Open-items list

Potential fields include:

- Business key
- Entity
- Document number
- Line number
- Vendor identifier
- Vendor country
- Amount
- Currency
- Final Cost Type
- WHT rate
- WHT amount
- Status
- Assigned reviewer

Potential filters include:

- Entity
- Vendor
- Country
- Status
- Reviewer
- Document number

### 14.3 Transaction detail

#### DOCUMENT

- Business key
- Document Number
- Line Number
- Document Date
- Posting Date
- Vendor
- Vendor Country
- Amount
- Currency

#### PURCHASING / MATERIAL

- Purchasing Document
- Item
- Material
- Material Description
- PO Description / Short Text
- Cost Description
- Material match status

#### CLASSIFICATION

- Suggested Cost Type
- Final Cost Type
- Classification Method
- Classification Reason

#### WHT

- WHT Applicable
- Matched rule
- Rule version
- WHT Rate
- WHT Base
- WHT Amount
- Rule-match status

#### REVIEW

- Status
- Assigned Reviewer
- Submitted By
- Submitted At
- Reviewed By
- Reviewed At
- Comments
- Override Reason

#### ACTIONS

- Save Draft
- Calculate WHT
- Submit for Review
- Approve
- Return for Correction
- Change Classification

Buttons should appear only when appropriate for the current status and user permissions.

## 15. Audit requirements

Create an append-only audit history when the corresponding functionality is implemented.

An audit event should record:

- Transaction
- Event type
- Timestamp
- User
- Previous value, where relevant
- New value, where relevant
- Comment or reason
- Correlation or request identifier, if available

Important auditable events may include:

- Transaction imported
- Material matched
- Material match failed
- Cost Type selected
- Cost Type changed
- WHT rule matched
- WHT rate calculated
- WHT result overridden
- Submitted for review
- Assigned to reviewer
- Approved
- Returned
- Recalculated

Do not use the audit table as the current-state transaction table.

Do not update or delete previous audit events through the application UI.

## 16. Placement of business logic

### Django models

Use for:

- Data structure
- Constraints
- Relationships
- Basic field validation

### Service layer

Use for:

- Material matching
- Classification saving
- WHT rule matching
- WHT calculation
- Workflow transitions
- Creating audit events

Potential service modules include:

- `transactions/services/material_matching.py`
- `transactions/services/classification.py`
- `transactions/services/wht_determination.py`
- `reviews/services/workflow.py`

### Views

Use for:

- Receiving requests
- Validating forms
- Calling service functions
- Returning templates or redirects

Do not place tax-rule logic directly inside views.

### Templates

Use for:

- Display
- Forms
- Conditional button visibility

Do not perform tax calculations in templates.

## 17. Data import

The first import may read the synthetic workbook directly or use reviewed CSV extracts from it. The approach should be selected after workbook analysis.

Import requirements should eventually include:

- Validate required columns
- Normalize whitespace carefully
- Preserve source identifiers as text
- Preserve leading zeros
- Parse dates explicitly
- Parse decimal values explicitly
- Detect duplicate business keys
- Report rejected rows
- Protect approved transactions from uncontrolled overwrite
- Make repeated imports idempotent where practical

The import should report:

- Rows read
- Rows created
- Rows updated
- Rows skipped
- Rows rejected
- Validation errors

Do not implement direct SAP integration in the MVP.

## 18. Future ML compatibility

The system should eventually generate clean labeled data for supervised learning.

Preserve separately:

- Original transaction features
- Suggested classification
- Suggestion source
- Suggestion confidence
- Human Final Cost Type
- Whether the human accepted or changed the suggestion
- Reviewer
- Decision timestamp
- Rule version
- Override reason

Do not train a model during the MVP.

Do not present ML predictions as final tax decisions.

A future prediction must be treated as a suggestion until accepted through the defined workflow.

## 19. Testing requirements

Every implemented business service must have tests.

Expected tests eventually include:

- Validated OpenItem business-key uniqueness
- Validated MaterialData business-key uniqueness
- No material match
- One material match
- Ambiguous material match
- WHT rule not found
- One WHT rule matched
- Multiple same-priority rules matched
- Decimal WHT calculation
- Workflow transition allowed
- Workflow transition rejected
- Approved decision snapshot remains unchanged after rule update
- Audit event created for classification change
- Audit event created for approval
- Duplicate import does not duplicate transactions

Do not claim a development step is complete while its tests are failing.

## 20. Coding standards

- Use clear business-oriented names.
- Add type hints to service functions.
- Keep functions small and focused.
- Avoid unexplained abbreviations.
- Use `Decimal` for rates and monetary amounts.
- Use database constraints only after business-key validation.
- Keep templates simple.
- Avoid premature abstraction.
- Do not add dependencies unless they solve an immediate requirement.
- Add docstrings to business services.
- Explain important tax-related assumptions in comments.
- Never invent tax rules.
- Clearly identify synthetic examples as fictional.

## 21. Out of scope for the initial MVP

Do not implement yet:

- Direct SAP connection
- SharePoint synchronization
- Power Automate integration
- Machine learning
- Generative AI
- Chatbot
- Power BI embedding
- Multi-country tax-rule completeness
- Production deployment
- Production SSO
- Email notifications
- Background task queues
- Automatic booking or payment instructions

## 22. Original incremental implementation phases

The phases below record the original implementation plan, not a current
backlog. Some listed work is now implemented and some status descriptions may
be stale. See [docs/DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md) for
verified current capabilities and known gaps. This plan does not authorize
implementation.

### Data Discovery Phase

Before creating Django code:

- Inspect every workbook sheet
- Identify the grain of each dataset
- Identify candidate business keys
- Identify relationship cardinality
- Identify incomplete and ambiguous matches
- Trace Cost Description, Cost Type and WHT fields across datasets
- Propose a simple relationship diagram
- Document assumptions and unresolved questions

Stop after analysis. Do not create models during this phase.

### Phase 0 — Repository and development environment (complete)

After data discovery and architecture review:

- Create the Django project
- Add only required dependencies
- Configure `.gitignore`
- Create `.env.example`
- Configure pytest
- Create a basic health/home page
- Update the README

Stop when the application runs and the initial test passes.

### Phase 1 — Validated reference data (complete)

Implement only the reference entities approved after discovery, potentially:

- Country
- CostType

Provide Django admin pages and tests.

### Phase 2 — Open items (complete)

Implement:

- OpenItem using the validated data structure
- Validated business-key constraint
- Synthetic-data importer
- Open-items list
- Read-only transaction detail screen

Classification and WHT calculations are implemented in later phases.

### Phase 3 — Material matching (complete)

Implemented:

- MaterialData
- Validated relationship fields
- Match statuses
- Material-matching service
- No-match and ambiguous-match handling
- Persisted current match state and audit events
- Administrator-only MaterialData maintenance in Django admin

Do not silently select an ambiguous record.

### Phase 4 — Human Cost Type classification (complete)

Implement:

- ClassificationDecision
- Final Cost Type selection
- Classification reason
- User and timestamps
- Classification audit event

Do not implement ML.

### Phase 5 — WHT rules and calculations (complete)

Implemented:

- Validated WHT rule structure
- Rule versioning and effective dates where required
- Rule-matching service
- WHTDetermination snapshot
- Decimal calculation
- No-match and ambiguous-match handling
- Latest approved version selected for an exact supplier/country/Cost Type key
- Zero-rate approved mapping recorded as No WHT
- Persisted determination-based reconciliation with one-cent tolerance
- Versioned rules and Administrator approval in Django admin
- Rejection of overlapping approved rule periods

Use only approved matching criteria.

### Phase 6 — Review workflow (partially implemented)

Implement:

- Reviewer assignment
- Submit
- Approve
- Return
- Controlled status transitions
- Comments
- Audit events

### Phase 7 — Dashboard and filtering (implemented)

Implement:

- Home counts
- My reviews
- Status filters
- Basic reporting export

### Phase 8 — Hardening (remaining work)

Implement as required:

- Permissions
- Improved validation
- Concurrency controls
- Import control totals
- Audit reporting
- PostgreSQL testing
- Security review preparation

### Future phase — ML suggestions

Only after sufficient validated human decisions exist:

- Prepare a labeled training dataset
- Build classification experiments outside the transaction workflow
- Store model version
- Store suggestion
- Store confidence
- Compare suggestion with the human final decision
- Monitor acceptance and override rates

## 23. Current instruction

Data discovery is complete. Implement only the phase explicitly requested and
do not treat this specification or the roadmap as authorization for additional
work. Follow the authoritative current behavior in
[docs/BUSINESS_RULES.md](docs/BUSINESS_RULES.md).
