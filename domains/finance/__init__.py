"""Finance domain plugin — JSONL transactions + structured spending queries.

The plugin contract:

  - ``handler.write(intent, message_or_attachment, session, ...)`` parses
    a credit-card / bank statement (already extracted to text by the
    orchestrator, or via a pluggable extractor) and appends one row per
    transaction to ``vault/finance/transactions.jsonl``. Idempotent on a
    sha256 of ``date|amount|merchant|raw``.
  - ``handler.read(intent, query, ...)`` answers ``finance.query`` intents
    by parsing the question into a structured ``query_finance`` call and
    returning a numeric answer plus a one-sentence summary.
  - ``handler.query_finance(category, date_range, agg)`` is a callable
    utility — exposed for retrieval to register as a tool when
    ``per_domain_shaping=true`` (issue #10 wires this).

The single load-bearing invariant is **idempotency on the row id**. Drive
sync replays, classifier retries, and statement re-uploads all converge.
"""
