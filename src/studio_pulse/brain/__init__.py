"""The grounded insight engine ("brain").

Order of work, cheapest and most trustworthy first:

  catalog.py      metrics.yaml: one definition per metric (unit, grain, aggregation)
  guard.py        sqlglot AST checks for any SQL that did not come from this package
  executor.py     a locked, read-only DuckDB session with row and time limits
  insights/       deterministic statistics -> Evidence objects (meta_insights)
  attribution.py  Adtributor + DIFF-lite over the cohort_dims pull
  fmt.py          numbers are formatted by code (TR %60,9 / EN 60.9%)
  verify.py       every number in user-facing text must match an evidence value
  narrate.py      template text first; an optional Narrator may only restyle it
  semantic.py     the semantic layer: metrics, grain, filters and their values, windows,
                  join paths and METRICS.md formulas (metric_docs.yaml, generated)
  intent.py       what a question asks: apps, filters, period, why / trend / ranking
  qa.py           question router: verified query -> catalog tool -> guarded SQL -> refusal
  drivers.py      "why" questions: the segment that moved a metric (association in time)
  consistency.py  the evidence must cover the question's filters and period, or no answer
  accuracy.py     held-out evaluation against SQL ground truth (pulse ask --eval)

An LLM never computes, never writes SQL that reaches BigQuery and never sends
anything anywhere: it can only pick and phrase. The provider is behind the
narrow protocol in llm.py; tests and CI use the deterministic FakeLLM.
"""
