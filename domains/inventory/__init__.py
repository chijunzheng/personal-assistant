"""Inventory domain plugin — household items as hybrid event-log + state.yaml.

The plugin contract:

  - ``handler.write(intent, message, session, ...)`` -> ``InventoryWriteResult``.
    Parses an ``inventory.add | inventory.consume | inventory.adjust`` event
    from natural language, appends one row to ``vault/inventory/events.jsonl``
    (idempotent on a sha256 of intent + message + session id), then
    recomputes ``vault/inventory/state.yaml`` from the full event log so
    state is always derivable from events.

  - ``handler.read(intent, query, ...)`` -> ``InventoryReadResult`` for
    ``inventory.query | inventory.list_low``. Dispatches to
    ``query_inventory(mode='item' | 'low_stock' | 'list')``.

  - ``handler.query_inventory(mode, item=None)`` -> ``dict``. Pure-Python
    state lookup. Exposed as a callable so issue #10 can register it in
    the retrieval tool palette when ``per_domain_shaping=true``.

The single load-bearing invariant is **state derivability**: replaying
the event log from scratch reproduces ``state.yaml`` exactly. Drive sync
replays, classifier reruns, and double-logged events all converge.
"""
