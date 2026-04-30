"""Reminder plugin package — scheduled and state-derived reminders.

Public surface lives in ``handler.py``: ``write``, ``read``, and
``due_reminders``. The plugin appends event rows to
``vault/reminder/events.jsonl`` with sha256 idempotency keys; cancellations
preserve the append-only invariant by adding a new ``cancelled`` event
rather than rewriting prior rows.
"""
