# Realistic UAT dataset v1

All records are synthetic. These files are a reproducible test fixture, not tax
advice, production data, or approved accounting entries. The fixed generator
seed is `20261116`; generated transaction rows are stable for the same database
reference records.

## Files and upload

- `open_line_items_uat.csv`: accounting source lines. Upload through the
  existing Import Center using the Open Items import flow.
- `material_data_uat.csv`: 152 purchasing-document items. Upload through the
  existing Import Center using the Material Data import flow.
- `uat_scenarios.csv`: expected results and review aid. **Do not upload this
  file.** Join each row to Open Items using `Entity` + `Document Number` +
  `Line Number`.
- `metadata.json`: seed, generation timestamp, source-reference counts, row
  counts, scenario distributions, and actual percentages.

Upload the two import CSVs separately in the Import Center and use its existing
preview and confirmation flow. Upload MaterialData before Open Items so exact
purchasing references are available for matching. The generator does not import
any file or write transaction records to the database. Source accounting CSV
`Cost Type`, `WHT Rate`, `WHT Amount`, and `WHT Posted` fields are blank; human
Final Cost Type decisions remain separate from imported accounting data.

## Scenario mix

- 100 invoices: 60 with one, 30 with two, and 10 with four eligible expense
  lines (160 eligible lines total).
- WHT by invoice: 85 approved positive-rate mappings, 10 approved zero-rate
  mappings explicitly representing No WHT, and 5 missing approved mappings.
- Posting by invoice: 80 correct positive-rate postings, 5 deliberate
  positive-rate discrepancies (3 incorrect amounts and 2 missing postings),
  10 zero-rate invoices without WHT postings, and 5 unresolved invoices
  without WHT postings.
- Material references across eligible expense lines: 152 exact matches, 5
  blank purchasing references, and 3 references intentionally absent from the
  MaterialData file.
- Ten additional zero-amount expense-account lines are ineligible by the
  application rule. Control account `210000` and WHT account `229220` are
  excluded by current GLEligibility settings.
- Eight invoices contain signed negative expense postings as synthetic credit
  notes. Documents use GBP, USD, and EUR. All invoices use USD group currency;
  company-code currencies vary by synthetic entity.

Positive and zero WHT mappings, rates, supplier IDs, countries, and Cost Types
come from the current database. Missing-mapping cases use existing supplier and
active Cost Type records and remain `REQUIRES_REVIEW`, not zero WHT.

Four positive-rate invoices have posting dates outside the mappings' effective
date range. The application currently ignores SAP Posting Date when selecting
the latest approved mapping. The current reference set has no key with more
than one approved version, so these rows exercise date independence with the
latest available version (v1); they cannot distinguish version ranking against
an older version.

## Calculation and currency notes

Line WHT uses signed document-currency expense amounts and the application's
Decimal `ROUND_HALF_UP` behavior. Invoice calculation is the rounded sum of
line determinations. Reconciliation follows the application formula:
`calculated WHT + signed posted WHT`; correct postings use the opposite sign.
Deliberate discrepancies differ by more than 0.01. Missing mappings keep
calculated WHT and the reconciliation difference blank/null and use pending /
provisional status.

Company-code and group-currency amounts use fixed synthetic illustrative
conversion factors recorded in `metadata.json`. They are not sourced FX rates
and are not used in WHT calculations.
