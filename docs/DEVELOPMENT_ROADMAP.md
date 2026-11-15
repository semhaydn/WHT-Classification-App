# Development roadmap

This roadmap is a status summary and a planning aid, not authorization to
implement future work. Implement only a separately requested step. The current
application is substantially implemented; do not restart it or replace
functioning components to match an earlier proposal.

## Verified current capabilities

- Django applications for reference data and transactions, with management
  commands for synthetic reference, purchasing, open-item, WHT mapping, and
  historical Cost Type imports.
- Administrator Import Center with CSV preview, validation, private staging,
  batch history, and atomic confirmation.
- Supplier-country and Cost Type reference data; versioned, approved and
  immutable WHT mappings.
- MaterialData matching and auditable match status.
- Human Final Cost Type selection and history.
- Decimal WHT determinations, persisted rule/rate snapshots, administrator
  exception resolution, reconciliation, and audit events.
- Role-protected Active and Completed invoice lists, filters, invoice-level
  Excel exports, review completion snapshots, and Administrator reopening.
- Synthetic data, test coverage for key business behavior, and guarded
  test-data reset tooling.

See [architecture](ARCHITECTURE.md), [business rules](BUSINESS_RULES.md), and
[data dictionary](DATA_DICTIONARY.md) for precise current behavior. A capability
being listed here does not imply it is production-ready.

## Known gaps and decisions for separately scoped work

These are candidate follow-up areas, not pre-approved implementation tasks:

- Confirm the business review/approval roles and any need for reviewer
  assignment or expanded per-line workflow. Current UI finalization is
  invoice-level.
- Decide the supported deployment database and hosting/security architecture.
  The checked-in default is SQLite; PostgreSQL operation, production hosting,
  and SSO are not established.
- Define production data authorization, retention, privacy, access auditing,
  backup, and operational procedures before using real business data.
- Confirm authoritative WHT treatment/effective-date requirements if the
  business needs semantics beyond the current approved-version selector.
- Consider control totals and operational reporting as separately validated
  import requirements.
- Define any additional audit/reporting, performance, accessibility, and
  security-review requirements for a production target.

Do not silently reinterpret the current latest-approved-version WHT rule while
answering a future effective-date request; it requires explicit business
confirmation and a scoped change.

## Future ML phase

Any future ML work is limited to supervised Cost Type recommendations for human
review. It must not make tax decisions or overwrite human Final Cost Types.
The [ML strategy](ML_STRATEGY.md) describes necessary safeguards. No ML model
or library is currently implemented.
