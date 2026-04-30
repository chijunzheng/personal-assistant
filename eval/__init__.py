"""Eval harness package — head-to-head, 5-dim Likert scoring, token chart.

This package implements the "engineered vs baseline" eval (issue #13). The
modules are deliberately small + composable:

  * ``eval.run``    — case discovery, vault_setup materialization, per-case
                      execution under both configs, results JSON writer
  * ``eval.score``  — manual 5-dim Likert scorer (interactive + non-interactive)
  * ``eval.report`` — composes ``docs/eval-progression.md`` from paired
                      results + (optionally) scored output
"""
