"""Fitness domain plugin — workouts, meals, metrics, profile + query_fitness.

The fitness plugin uses a hybrid storage model:

  - ``vault/fitness/profile.yaml`` — canonical user state (mutable).
  - ``vault/fitness/profile_events.jsonl`` — append-only audit of profile edits.
  - ``vault/fitness/workouts.jsonl`` — append-only workout log.
  - ``vault/fitness/meals.jsonl`` — append-only meal log.
  - ``vault/fitness/metrics.jsonl`` — append-only body-metrics log.
  - ``vault/fitness/plans/`` — generated markdown plans (issue #8).

The plugin contract (this issue, #7 — logging surface only):

  - ``handler.write(intent, message, session, ...)`` -> ``FitnessWriteResult``
    for ``fitness.workout_log | meal_log | metric_log | profile_update``.
    Idempotent on a content-derived sha256 ``id`` per row.
  - ``handler.read(intent, query, context_bundle, ...)`` -> reply text for
    ``fitness.query`` (dispatches to ``query_fitness`` for trend queries).
  - ``handler.query_fitness(kind, date_range, agg, ...)`` is a pure-Python
    aggregation utility per ``domain.yaml`` signatures.

Plan generation (``fitness.workout_plan`` / ``fitness.nutrition_plan``) is
issue #8's responsibility and is **not** implemented here.

The single load-bearing invariant is idempotency on content sha256. Drive
sync replays, classifier reruns, and double-logged events all converge.
"""
