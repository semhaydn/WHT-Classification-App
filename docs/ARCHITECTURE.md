# Current architecture

This document describes the application as implemented and tested. It is not a
target redesign. The application is an incremental Django business application;
preserve working behavior unless a requested change explicitly authorizes a
change.

## Runtime and boundaries

- Python with Django 5.2, Django templates, and SQLite by default.
- `config` contains Django settings, URL routing, and WSGI/ASGI entry points.
- `reference_data` owns the Cost Type catalog, supplier-country reference data,
  and versioned WHT mappings.
- `transactions` owns imported accounting and purchasing data, classification
  decisions, WHT determinations, review/audit records, import batches, and
  snapshots.
- `templates/` renders the dashboard, invoice lists, transaction details,
  exceptions, and Import Center screens.
- pytest and pytest-django are used for tests. `openpyxl` creates Excel exports.

Business operations are primarily implemented in `transactions/services/` and
`reference_data/services.py`. Views handle HTTP requests, access checks, form
validation, messages, and rendering; templates do not calculate tax.

## Data flow

1. Synthetic CSVs or Administrator uploads enter through management commands
   or the Import Center.
2. Imports validate headers and values and preserve identifiers as strings.
   Row-level reporting and all-or-nothing behavior vary by import path; see
   [Import semantics](#import-behavior). Import Center previews are staged
   privately and confirmed in one database transaction; MaterialData is
   applied before Open Line Items.
3. Material matches are resolved by Purchasing Document + Item. Match status
   and changes are persisted and audited.
4. A Transaction User records the human Final Cost Type. A WHT determination
   service selects an approved mapping or records an unresolved exception.
5. Transaction Users complete classification and invoice-level review.
   Finalization requires all required lines reviewed and no completion
   blockers; it writes document and line calculation snapshots.
6. Administrators manage reference data, resolve exceptions, and reopen
   completed lines or invoices. Re-completion creates a new snapshot version;
   previous snapshots remain historical evidence.

## Data categories and records

| Category | Current records | Purpose |
|---|---|---|
| Reference/master | `CostType`, `SupplierCountry`, `WHTMapping` | Reusable classification vocabulary, supplier country, and approved WHT rules |
| Source data | `OpenItem`, `MaterialData` | Imported accounting lines and purchasing-document items |
| Human classification | `ClassificationDecision` | Current and historical Final Cost Type decisions, actor, method, and reason |
| Historical suggestions | `HistoricalCostTypeSuggestion` | Imported historical examples kept separate from human decisions |
| Derived result | `WHTDetermination` | Versioned persisted WHT result and snapshots of inputs/rule values |
| Review and audit | `ReviewAction`, `AuditEvent`, `LineExceptionRecord` | Workflow actions, audit evidence, flags, and line exclusions |
| Historical review | `ReviewDocumentSnapshot`, `ReviewLineCalculationSnapshot` | Captured invoice- and line-level completion evidence |
| Import operations | `ImportBatch`, `ImportValidationError`, `ImportUploadSession` | Batch counts, row errors, and private upload-preview lifecycle |
| Workflow configuration | `GLEligibility` | Administrator-managed G/L eligibility; it does not rewrite existing decisions |

Important database keys are described in [the data dictionary](DATA_DICTIONARY.md).
The WHT and classification rules are specified in
[the business rules](BUSINESS_RULES.md).

## Current workflow and access

- Transaction User (and legacy Classifier/Reviewer memberships) and
  Administrator users can use the transaction workflow. Import Center,
  exceptions, reference data administration, and reopening are Administrator
  functions. Django admin keeps workflow, determination, and audit records
  read-only.
- Active Invoices are derived from source lines and exclude documents with a
  current completed snapshot. Completed Invoices read the current snapshot.
  Lists and Excel exports do not recalculate WHT.
- Review completion is invoice-level. Legacy line review routes redirect to
  the invoice summary; service functions retained in the code are not all
  exposed as current browser workflow actions.
- Reconciliation uses saved determinations and signed G/L 229220 postings.
  Missing determinations remain Pending / Provisional, not zero.

## Import behavior

- Repeated MaterialData keys in one file use the last CSV occurrence and warn
  for earlier occurrences. The database still enforces one stored row per
  Purchasing Document + Item.
- Duplicate Open Line Item keys reject the file. Conflicting changes to
  existing Open Line Item source content also reject the file.
- Duplicate WHT Mapping keys (Supplier + Country + Cost Category) and invalid
  mapping rows block the mapping import; imported rules start as drafts.
- Historical Cost Type rows are stored as suggestions only. They are not
  Final Cost Type decisions and do not initiate WHT calculations.
- Re-imports are designed to be idempotent. Imports do not recalculate
  completed snapshots.

## Deployment and architecture limits

The checked-in settings use SQLite. Production database operation, SSO,
deployment, and external integrations are not established by this repository.
Production secrets and private Import Center staging are guarded by settings,
but this is not evidence that the application is production-ready. Do not
change or deploy infrastructure without a separately approved task.

The architecture includes fields that can hold recommendations and historical
suggestions, but it contains no trained model, inference service, or automated
Cost Type classifier. See [the ML strategy](ML_STRATEGY.md); it is future
direction, not current functionality.
