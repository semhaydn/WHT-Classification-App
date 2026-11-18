# WHT Classification and Review Application

This project is an incremental Django application for transaction-level withholding-tax classification, calculation, review, approval, and audit tracking.

The development dataset is synthetic and is stored under `data/wht_synthetic_data/`.

## Project documentation

The current implementation and its rules are documented here:

- [Architecture](docs/ARCHITECTURE.md) — verified application structure and data flow
- [Business rules](docs/BUSINESS_RULES.md) — authoritative classification and WHT behavior
- [Data dictionary](docs/DATA_DICTIONARY.md) — synthetic CSV schemas, grains, and application records
- [ML strategy](docs/ML_STRATEGY.md) — future supervised Cost Type recommendations only
- [Development roadmap](docs/DEVELOPMENT_ROADMAP.md) — current capabilities and separately scoped future work

The roadmap and ML strategy do not authorize implementation of future
functionality. Use the business rules document as the source of truth for
classification and WHT behavior.

## Current implementation

The data-discovery work is complete. The prototype currently implements reference data and CSV import, invoice-level Active and Completed lists with filters and Excel export, line-level review screens, material matching, human Cost Type decisions, WHT determination, review actions, persisted completed-review calculation snapshots, dashboard summaries, and administrator reopening.

MaterialData is matched by the validated business key `Purchasing Document + Item`. The current match status is persisted, and match-state changes are audited. If a MaterialData CSV repeats a key, the final occurrence wins and earlier occurrences are reported as warnings. Open Line Items and WHT Mapping imports reject duplicate business keys rather than choosing a row. A missing match remains visible on the transaction detail page, and only corrected purchasing data clears it.

Non-zero accounting lines are eligible for classification by default, including negative amounts. Administrators can explicitly mark a G/L account eligible or excluded in Django Admin under **G/L eligibility**; zero-amount lines are always excluded. The additive seed migration explicitly excludes the existing invoice/control (`210000`) and posted-WHT (`229220`) accounts. Eligibility changes affect workflow checks only and do not modify imported lines, existing classifications, WHT determinations, review statuses, or completed snapshots.

Transaction Users can flag an accounting line for Administrator review. Administrators can clear a flag with a recorded decision or exclude the individual line with a reason. The Administrator-only Exceptions tab also lists unresolved and stale WHT determinations, which can be explicitly recalculated after reference data is corrected or resolved with an audited one-off rate / explicit No WHT decision. Flags, exclusions, and unresolved WHT determinations block final invoice snapshot completion; excluded lines and reconciliation discrepancies do not. A document snapshot is created only after all required lines have been reviewed. Administrators can reopen one line or an entire completed invoice. Reopening retains prior snapshot evidence and decision/audit history, moves the invoice out of the current completed list, and exposes prior snapshot versions in document history. Re-completion creates a new version; historical snapshots remain read-only.

**Active Invoices** groups source accounting lines by entity and accounting document and excludes invoices with a current completed snapshot. **Completed Invoices** reads captured values from the current completed snapshot; reopening moves an invoice back to Active without deleting its historical snapshot. Both lists support server-side filters and invoice-level Excel exports using the same curated columns. Listing, filtering, and exporting do not recalculate WHT or modify snapshots.

WHT rules match exactly on supplier ID, supplier country, and final Cost Type. The latest approved version for that key is used regardless of SAP Posting Date; effective dates remain approval metadata and are not used as a fallback selector. Missing rules are exceptions for every country. An approved zero-rate mapping is recorded distinctly as No WHT.

WHT calculations use the signed document-currency amount multiplied by the persisted rate, with `Decimal` and `ROUND_HALF_UP` to two places. Reconciliation sums signed GL `229220` postings and compares `Calculated WHT + Posted WHT`; differences up to one cent in either direction are reconciled. Missing determinations show Pending / Provisional. Dashboard, detail, snapshot and export reads use persisted determinations rather than current mapping rules.

The synthetic CSV files are development fixtures, not approved tax guidance. The MaterialData fixture contains duplicate keys and exercises last-occurrence-wins behavior. Duplicate keys in a WHT Mapping upload block the complete import. The checked-in mapping fixture has been corrected and contains no duplicate keys. The Cost Type catalog is maintained separately in `cost_types_synthetic.csv`; historical cost-type examples do not define which Cost Types are valid. The WHT Mapping CSV has no authoritative No WHT treatment field, so imports do not infer that treatment from a zero or positive rate.

## Optional AI Cost Type recommendations

The invoice-line page can show up to three ranked Cost Type recommendations
from the OpenAI Responses API. This is a classification aid only: users must
confirm or change the editable Cost Type selection, and only that submitted
human selection becomes `final_cost_type`. The recommendation cannot determine
WHT treatment, rates, or approvals. Existing human decisions are never replaced.
Opening the page does not create a classification or audit record; the top
recommendation is stored as the existing suggested Cost Type only when the
human submits the form.

The feature is off by default. To enable it, install the declared requirements
and configure these environment variables for the Django process:

- `OPENAI_API_KEY`: API key; keep it in a Codespaces secret or other secret
  store, never in source control or a committed `.env` file.
- `OPENAI_COST_TYPE_MODEL`: a model identifier enabled for the API account.
- `LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True`: explicit opt-in. The default is
  `False`.

The API request contains only the G/L account and description, accounting-line
text and assignment, material description, matched MaterialData short text and
material group, relevant purchasing-document type, and active Cost Type codes,
names, and descriptions. Supplier identifiers/names/countries, document
identifiers, amounts, currencies, tax information, usernames, approval
information, and workflow state are not included. Do not enable this feature on
real company transactions without separate authorization; use synthetic UAT
data only.
Descriptions are treated as untrusted input and cannot override the system
classification instructions. Requests use `store=False`, an 8-second timeout,
and one bounded SDK retry.

Authentication failures, rate limits, timeouts, service outages, refusals,
incomplete responses, and invalid recommendations leave manual classification
available. Only active database Cost Types are accepted; duplicates and
unknown/inactive codes are discarded. Recommendations are not calibrated
probabilities and do not display confidence percentages. To disable the
feature immediately, set `LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=False` and
restart the Django process. No schema changes or migrations are required.

In a Codespaces terminal, configure `OPENAI_API_KEY` as a Codespaces secret
before starting the app, then set the model and flag and launch the preview:

```bash
python -m pip install -r requirements.txt
export OPENAI_COST_TYPE_MODEL="<model-id-enabled-for-your-account>"
export LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=True
python manage.py check
bash scripts/start_codespaces_preview.sh
```

To turn recommendations off, run `export LLM_COST_TYPE_RECOMMENDATIONS_ENABLED=False`
and restart the preview process. The model variable is intentionally not
defaulted to a model that may be unavailable to a particular API account.

## Application access

Invoice lists, details, exports, dashboards, and workflow actions require an
authenticated user in the `Transaction User` or `Administrator` group. Existing
`Classifier` and `Reviewer` group memberships are accepted as Transaction User
memberships for compatibility. Imports and reference-data administration are
restricted to Administrators. Transaction, classification, WHT, review, and
audit records are read-only in Django admin; workflow changes must use the
validated application actions. User and reference-data management remain
available to Administrators in Django admin.

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Apply migrations and load the synthetic data:

```powershell
python manage.py migrate
python manage.py import_reference_data
python manage.py import_wht_mapping
python manage.py import_material_data
python manage.py import_cost_type_decisions
python manage.py import_open_items
```

WHT rule imports create drafts. Sign in to Django admin as a staff user in the `Administrator` group, add effective dates, and approve the rules. Approved rules are immutable; create a new version for changes and use non-overlapping effective dates.

## Recurring data uploads

An Administrator can use **Import Center** at `/imports/` to upload recurring
MaterialData and Open Line Items CSV files. The page validates the files and
shows a preview before the Administrator confirms the import. When both files
are selected, MaterialData is processed first, followed by Open Line Items and
material matching. Either file can be uploaded alone; for open items uploaded
without MaterialData, existing purchasing data is used and unmatched lines
remain explicitly unmatched.

Use the same CSV column headers as the existing source files under
`data/wht_synthetic_data/`:

- MaterialData: `material_data_synthetic.csv`
- Open Line Items: `open_line_items_synthetic.csv`

The preview reports expected create/update/skip/reject counts, row errors,
non-blocking MaterialData duplicate warnings, and material-match outcomes.
Any blocking validation error rejects the complete upload; valid rows are not
partially applied. Repeated MaterialData keys use the final CSV occurrence.
Duplicate Open Line Item keys and conflicting changes to existing source lines
block that file. The import is idempotent and is executed in a single database
transaction. The Import Center does not modify supplier
countries, WHT mappings, Cost Types, users, or historical Cost Type
suggestions. Routine uploads do not require a test-data reset.

Only users in the `Administrator` group (and superusers) can access uploads,
previews, confirmation, and batch history. Create/manage the group and staff
accounts through Django admin. The management commands remain available for
operator use.

Each file is limited to 10 MiB by default. Configure
`WHT_IMPORT_MAX_FILE_SIZE` (bytes) and `WHT_IMPORT_STAGING_TTL_HOURS` (default
24) to change the limit and retention period. Files awaiting confirmation are
stored under `WHT_IMPORT_STAGING_DIR`, which must be writable by the web
process and outside the repository, public media, and static-file directories.
For production, set `WHT_IMPORT_STAGING_DIR` to private storage available to
all web workers handling the upload and confirmation requests. The application
requires this setting in production. Staged files are deleted after successful
import; abandoned previews are expired and cleaned when an Administrator opens
the Import Center. Cleanup failures are logged and surfaced in the UI. Keep
normal infrastructure access controls and backups appropriate for the
configured storage.

The sample source files are synthetic development data only. Do not upload
real company data to an environment that has not been explicitly approved for
it.

## Completed reviews and test-data reset

After all eligible expense lines for a document are reviewer-approved, view the
persisted WHT calculation and SAP reconciliation at `/review-completed/` or use
its **Download Excel** link. Amounts use the signed SAP document-currency
postings. Routine source imports, supplier-country changes, and WHT-rule
approval do not recalculate or replace completed snapshots. A subsequent valid
review completion creates a new snapshot version and retains prior versions.
Export fields captured in a snapshot come from that historical snapshot; fields
not captured there may still be read from the current Open Item record.

To clear only tracked imported transaction and derived test records, first run
the guarded reset command. It creates a timestamped SQLite backup under
`backups/` before deleting anything; users, permissions, supplier countries,
WHT mappings, and Cost Types are preserved. It also removes Import Center
preview sessions and their privately staged files:

```powershell
python manage.py reset_wht_test_data --confirm
python manage.py import_reference_data
python manage.py import_wht_mapping
python manage.py import_material_data
python manage.py import_cost_type_decisions
python manage.py import_open_items
```

The historical Cost Type import creates suggestions only; it does not create or
replace reviewer-approved Final Cost Type decisions. The reset command supports
SQLite backups and refuses to delete records if the backup cannot be made.

Run checks and tests:

```powershell
python manage.py check
python -m pytest
```

Start the development server:

```powershell
python manage.py runserver
```

Open `http://127.0.0.1:8000/` to view the application.

## GitHub Codespaces preview (development only)

From the repository root in a Codespaces terminal, after the setup and
migration steps above have been completed:

```bash
bash scripts/start_codespaces_preview.sh
```

The script serves the app on port `8001`. It exits with a message if it is not
run in Codespaces or if port `8001` is already in use; it never kills existing
processes. It sets `DJANGO_ENVIRONMENT=development` and `DJANGO_DEBUG=true`,
and limits `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` to the
Codespaces forwarded hostname (derived from `CODESPACE_NAME` and
`GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`) and `https://localhost:8001`. It
does not create, migrate, seed, reset, or copy a database.

Open the forwarded address printed by the script. Keep the port visibility set
to **Private**; never make it Public. `DEBUG=True` is acceptable only for this
development preview with synthetic data.

Stop the server with `Ctrl+C`. When finished, stop the Codespace (Command
Palette: **Codespaces: Stop Current Codespace**, or from github.com/codespaces)
so the forwarded port is no longer served.
