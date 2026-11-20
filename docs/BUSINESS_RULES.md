# Classification and WHT business rules

This document is the authoritative source of truth for the application's
current classification and WHT behavior. It records confirmed prototype rules,
not tax advice. Do not infer additional tax rules from synthetic examples.

## Classification

- Classification operates on non-zero accounting lines whose G/L account is
  eligible. Unconfigured accounts are eligible by default; Administrators can
  explicitly include or exclude accounts. Zero-amount lines are always
  ineligible.
- A line-level Administrator exclusion also removes that line from the required
  classification and review set. Exclusions require a recorded reason and
  audit event.
- The human Final Cost Type is an explicit decision. Each line has one current
  decision; changes retain decision history and produce audit events.
- Historical Cost Type imports and system recommendations, including the
  optional AI recommendation, are not final decisions. A human must select or
  confirm the Final Cost Type. An AI suggestion never replaces an existing
  human decision.
- Stored AI recommendations remain available when AI is disabled or source
  data changes. Only an explicit re-run creates a subsequent recommendation;
  it does not modify previous results or a human decision.
- A MaterialData match can supply purchasing text used as Cost Description; it
  does not itself assign a Final Cost Type.

## MaterialData matching

- Exact key: Purchasing Document + Item. Material Number is not the key.
- No matching record is `NOT_FOUND`; one matching record is `MATCHED`; multiple
  candidates are `AMBIGUOUS`. Ambiguous records are never silently selected.
- The database enforces uniqueness for the stored Purchasing Document + Item
  key. Match state is persisted; changes are audited.
- If an input MaterialData CSV repeats a key, the last occurrence in that file
  is imported. Earlier rows for that key are skipped with warnings. This is
  implemented in both the management-command and Import Center paths and is
  covered by tests. It does not change the stored-key uniqueness constraint.
- A missing match remains visible; corrected purchasing data, rather than a
  manual arbitrary link, is the supported way to resolve it.

## WHT mapping selection

- Exact lookup key: Supplier ID + Supplier Country + Final Cost Type. Entity
  and SAP Posting Date are not part of this lookup key.
- Supplier identity is resolved across the accounting document. If it is
  missing or inconsistent, there is no unique supplier match.
- Only approved mappings can be used. Draft imports are not calculation rules.
  Approval requires an Administrator, effective-from metadata, approver, and
  approval timestamp. Approved rules are immutable; create a new version for
  a rule change. Overlapping effective periods for the same key cannot be
  approved.
- For the exact key, select the highest approved version. This selection is
  independent of SAP Posting Date and does not fall back based on effective
  dates. Effective dates are approval metadata in the current selection
  behavior.
- Multiple candidates at the selected latest version are ambiguous and require
  review; never pick an arbitrary candidate.
- No matching approved mapping is an exception for every country. In
  particular, the tests confirm that a missing mapping for a TR supplier is
  still an exception; do not assume a domestic default.
- A matched approved mapping with a zero rate is an explicit No WHT result.
  Missing mapping is never interpreted as zero. The WHT Mapping CSV has no
  authoritative treatment column and does not establish additional treatment
  semantics.

## WHT calculation

- Base: the signed Open Item document-currency amount for the eligible line.
- Formula: `WHT amount = signed document-currency amount × fractional WHT rate`.
- Positive and negative amounts retain their signs. No currency conversion is
  performed.
- Use Python `Decimal` for rates, bases, and money. Quantize to two decimal
  places with `ROUND_HALF_UP`.
- Persist the determined rate, base, amount, currency, Cost Type, supplier,
  country, mapping title/version, source, calculation version, and actor where
  applicable. Existing results are not silently recalculated when reference
  data changes.
- A determination missing for the current Final Cost Type, a missing/ambiguous
  mapping, or an invalid/unresolved determination blocks review completion.
  An Administrator can explicitly recalculate after reference corrections or
  record a reasoned one-off rate / explicit No WHT decision. Each new result is
  separate historical evidence.
- An Administrator's one-off rate must be finite and non-negative, and zero
  must be recorded as explicit No WHT instead of a zero manual rate. Both
  treatments require an audit reason; neither changes the shared mapping.
- A human reclassification makes the prior WHT result stale until it is
  explicitly recalculated for the new Final Cost Type.

## Invoice amount and reconciliation

- Invoice/control amount is the negative of the sum of signed document-currency
  postings on G/L 210000. Mixed currencies or absent control postings are
  reported as unresolved.
- Calculated WHT is the sum of valid persisted line determinations for required
  classifiable lines in the invoice currency. It is not recalculated during
  dashboard, detail, snapshot, or export reads.
- Signed posted WHT is the sum of document-currency postings on G/L 229220.
  With no such lines, posted WHT is zero. Mixed or mismatched currencies are
  unresolved.
- `Difference = Calculated WHT + Signed Posted WHT`. Round the difference to
  two decimal places using `ROUND_HALF_UP`; absolute differences up to and
  including 0.01 are reconciled. Larger differences are discrepancies.
- If required WHT is missing or unresolved, calculated WHT and the difference
  remain null and the status is Pending / Provisional, not zero.

## Review, completion, and history

- Finalization requires a current Final Cost Type and valid current WHT
  determination for every required line, completed classification/review for
  those lines, and no unresolved Administrator flag or other completion
  blocker.
- Administrators may resolve a user flag with a recorded decision or exclude a
  line with a reason. Unresolved flags block completion; an explicitly
  excluded line is not a required line.
- Completion captures invoice and line snapshots, including the applied
  classification and WHT evidence. Active/completed lists and exports do not
  update those snapshots.
- Routine imports, reference changes, and WHT rule approval do not recalculate
  or replace historical snapshots. Reopening is explicit and audited; it
  marks the current snapshot historical without changing its captured values.
  Re-completion creates a higher snapshot version and retains prior evidence.

## Source and precedence

These rules are based on current services, models, migrations, tests, and
verified synthetic import behavior. Where another document disagrees about
current behavior, follow this file and raise the discrepancy for correction.
The prototype rules do not constitute production tax advice or establish
unmentioned business policy.
