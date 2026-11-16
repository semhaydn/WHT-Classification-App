# Data dictionary

This dictionary describes the checked-in synthetic CSVs and current Django
records. The fixtures are synthetic development/test data, not approved tax
guidance or production business data. Keep identifiers as text where
appropriate so leading zeros and source formatting are preserved.

## Synthetic CSV inventory

Counts are raw rows in the checked-in CSVs unless noted. They are not counts of
database records after validation or deduplication.

| File | Grain | Raw rows | Key fields / notes |
|---|---|---:|---|
| `countries_synthetic.csv` | Supplier-country reference row | 2,325 | Supplier identifier; two rows are missing required values and are rejected by the importer, yielding 2,323 imported rows |
| `cost_types_synthetic.csv` | Cost Type catalog entry | 13 | Cost Type |
| `material_data_synthetic.csv` | Purchasing document item | 234 | Purchasing Document + Item; 10 duplicate key groups (20 rows) collapse to 224 distinct keys with last-occurrence-wins import |
| `open_line_items_synthetic.csv` | Accounting document line | 42 | Entity + Document Number + Line Number |
| `wht_mapping_synthetic.csv` | Supplier/country/Cost Type mapping | 47 | Supplier + Country + Cost Category; no repeated keys in this fixture |
| `cost_type_decisions_synthetic.csv` | Historical Cost Type example | 613 | Source document/item plus Cost Type; imported as suggestions, never as final decisions |

`metadata.json` records generator/source counts and output counts. Its
`source_rows.wht_mapping` value is 51, while the checked-in WHT CSV contains 47
rows after fixture correction; `output_rows.wht_mapping` now reflects the
checked-in file. The supplier-country fixture contains 2,325 raw CSV rows,
while importer tests confirm 2,323 valid imported records.

### File columns

- `countries_synthetic.csv`: `Title`, `Country`, `Supplier`, `Supplier Name`.
- `cost_types_synthetic.csv`: `Cost Type`.
- `material_data_synthetic.csv`: `MaterialData Key`, `Purchasing Document`,
  `Item`, `Material`, `Plant`, `Order Unit`, `Document Date`,
  `Supplier/Supplying Plant`, `Short Text`, `Purch. Organization`,
  `Net Price`, `Currency`, `Net Order Value`, `Purchasing Info Rec.`,
  `Purchasing Doc. Type`, `Purch. Doc. Category`, `Purchasing Group`,
  `Material Group`.
- `open_line_items_synthetic.csv`: `Title`, `Entity`, `Document Number`,
  `Line Number`, `Document Type`, `Document Date`, `Posting Date`,
  `Document Reference`, `Vendor Number`, `Vendor Name`,
  `Purchasing Document`, `Purchasing Document Item`, `MaterialData Key`,
  `Material Group`, `Material`, `Material Description`,
  `Document Currency Amount`, `Document Currency`,
  `Company Code Currency Amount`, `Company Code Currency`,
  `Group Currency Amount`, `Group Currency`, `GL Account`,
  `GL Account Description`, `Offsetting Account`, `Text`,
  `Cost Description`, `Cost Type`, `WHT Rate`, `WHT Amount`, `WHT Posted`.
  The import command uses source fields for the OpenItem; trailing
  Cost Description, Cost Type, and WHT columns do not create human decisions
  or determinations.
- `wht_mapping_synthetic.csv`: `Title`, `Supplier`, `Supplier Name`,
  `Cost Category`, `WHT %`, `Country`.
- `cost_type_decisions_synthetic.csv`: `Title`, `Document Number`, `Item`,
  `Document Date`, `Posting Date`, `Document Reference`, `Vendor Name`,
  `Purchasing Document`, `Item2`, `Material`, `Material: Description`,
  `G/L Account`, `G/L Account: Long Text`, `Assignment`, `PO Description`,
  `Cost Type`, `Confidence Rate`, `Prediction Basis`.

The WHT mapping source has no authoritative treatment column. A zero rate is
handled according to the approved-zero-rate rule in
[Business Rules](BUSINESS_RULES.md); do not infer further treatment from the
fixture.

## Dataset grains and validated keys

| Record | Grain and identity | Relationships and cautions |
|---|---|---|
| SupplierCountry | One supplier identifier; unique `supplier_id` | Stores the supplier's country. Supplier names are descriptive, not keys. |
| CostType | One catalog code; unique `code` | The catalog is separate from historical examples. |
| MaterialData | One Purchasing Document + Item; database unique constraint | Material Number is not unique. Repeated CSV keys use the last row with warnings. |
| OpenItem | One entity + accounting document + line; database unique constraint | Source identifiers remain strings. A document can contain multiple accounting lines. |
| WHTMapping | One Supplier + Country + Cost Type + version; unique constraint includes `version` | Only approved rows participate in matching. Selection uses latest approved version, not Posting Date. |
| HistoricalCostTypeSuggestion | One source-batch + source-row | Not a Final Cost Type decision or active ML prediction. |

Material matching uses OpenItem Purchasing Document + Purchasing Document
Item to locate MaterialData Purchasing Document + Item. No/one/multiple
matches are represented explicitly. The current unique database key prevents
multiple stored MaterialData rows for the same key.

WHT supplier identity is resolved at accounting-document level before matching
the exact Supplier ID + Supplier Country + Final Cost Type key. Multiple or
missing supplier identities are exceptions, not an invitation to select a
line arbitrarily.

## Current application records by purpose

| Purpose | Django records | Important values |
|---|---|---|
| Source accounting | `OpenItem` | Entity, document and line identifiers; document/posting dates; supplier; purchasing references; signed amounts in document/company/group currencies; G/L data; workflow status |
| Source purchasing | `MaterialData` | Purchasing document/item, material, descriptions, plant/organization, order values and currency |
| Reference | `SupplierCountry`, `CostType`, `WHTMapping` | Supplier country; active Cost Type; WHT supplier/country/category/version, rate, effective metadata, status, approver |
| Human classification | `ClassificationDecision` | Optional suggested Cost Type, required Final Cost Type, method, reason, actor/time, current marker |
| Derived tax result | `WHTDetermination` | Status, candidate count, matched mapping, Cost Type/supplier/country/rule/rate/base/amount/currency snapshots, source, version, actor/time |
| Match history | `MaterialMatch` | Match status, candidate count, selected material when unique, checked time |
| Review and exceptions | `ReviewAction`, `LineExceptionRecord` | Review action/comments; user flag or Administrator exclusion and resolution |
| Audit | `AuditEvent` | Event type, actor/time, previous/new values, comment |
| Completion history | `ReviewDocumentSnapshot`, `ReviewLineCalculationSnapshot` | Invoice/line captured amounts, classification, mapping/version, result, exceptions, completion actor/time, snapshot version |
| Import history | `ImportBatch`, `ImportValidationError`, `ImportUploadSession` | File digest, row counts/errors, uploader, preview and staging status |
| Eligibility configuration | `GLEligibility` | Per-G/L eligibility override; unconfigured G/Ls default eligible for non-zero lines |

## Import semantics

- CSV numeric tax and monetary values are parsed as `Decimal`; dates are parsed
  explicitly. Identifiers are read as text.
- MaterialData CSV duplicate keys use the last occurrence; earlier rows are
  skipped and warned. The 234-row fixture therefore yields 224 unique rows.
- Duplicate Open Line Item business keys and conflicting changes to existing
  source lines block that file.
- Invalid or duplicate WHT mapping input blocks the complete mapping import;
  valid rows are not partially written. WHT rule imports create drafts.
- Historical Cost Type CSV rows become suggestions only.

For behavior governing these values, use [Business Rules](BUSINESS_RULES.md).
