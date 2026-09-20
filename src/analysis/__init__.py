"""Analysis layer: indicator computation + LLM prompt construction (§7.17).

Extracted from ``core/decision_pipeline.py`` so the pipeline orchestrates while the
market-analysis math and prompt engineering live where the plan always intended.
Pure functions only — no I/O, no network; everything here is unit-testable as-is.
"""
