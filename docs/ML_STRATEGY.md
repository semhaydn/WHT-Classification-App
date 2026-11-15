# Future supervised Cost Type classification strategy

## Current state

There is no ML model, training pipeline, inference endpoint, or live
recommendation feature in the application. ML is not part of the current MVP.

The schema is partly future-compatible: `ClassificationDecision` separates an
optional suggested Cost Type from the final human Cost Type and includes an
`ML_SUGGESTION` method choice. Historical Cost Type rows can be imported into
`HistoricalCostTypeSuggestion` with optional confidence and prediction-basis
text. The importer deliberately stores them as suggestions; it does not set a
Final Cost Type or calculate WHT. These fields are storage compatibility, not
evidence of an implemented classifier.

## Future objective and boundary

If separately approved, build a **supervised Cost Type recommendation system**
that proposes one or more likely Cost Types for a human to assess. It is not an
autonomous tax decision system.

The classifier's only target is Cost Type. It must not predict, select, or
approve WHT treatment, rate, base, or amount. Existing deterministic WHT rules
run only from the human Final Cost Type and the confirmed approved mapping
process.

## Possible supervised-learning workflow

1. Obtain explicit authorization for the data and its use. Until then, use only
   synthetic fixtures.
2. Define a governed training set from reviewed human Final Cost Type
   decisions. Keep labels, reviewer corrections, timestamps, and any override
   reasons attributable and traceable.
3. Review candidate inputs such as transaction text, purchasing descriptions,
   material group, and other validated source fields. Exclude fields that leak
   the final label or downstream tax result. Do not assume imported historical
   examples are correct labels without validation.
4. Split and evaluate data to avoid leakage across repeated documents,
   suppliers, or time periods as appropriate to the approved use case. Compare
   against a simple baseline and report per-class performance, coverage,
   confidence calibration, and abstention behavior.
5. Present recommendations with model/version and confidence metadata for
   human review. Keep the existing Final Cost Type unchanged until a human
   explicitly makes a decision through the supported workflow.
6. Monitor recommendation acceptance, override, abstention, and error patterns
   under an agreed governance process. Disable or roll back a model when
   validation or data-quality requirements fail.

These are design guardrails, not a request to create a model, select a library,
or change the current workflow. Any implementation needs an explicit future
task, data authorization, acceptance criteria, privacy/security review, and
tests.

## Non-negotiable safeguards

- A model output is always a recommendation, never a human decision.
- Predictions never overwrite a human Final Cost Type or historical approved
  result.
- No inference result can bypass reviewer permissions, audit history, or
  deterministic WHT rules.
- Never infer tax rules from labels, synthetic mappings, or model confidence.
- Do not add ML dependencies or real company data without explicit
  authorization.
