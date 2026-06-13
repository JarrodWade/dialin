"""Prompt eval harness for dialin.

Two layers, deliberately separate:
  * Plumbing/behavior tests run with a *fake* model (deterministic, free, CI).
  * Prompt-quality scenarios run against a *live* model on demand (``make eval``).

See ``harness.py`` for the assertion DSL and ``fakes.py`` for the scripted model.
"""
