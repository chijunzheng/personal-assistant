"""Journal domain plugin — narrative markdown notes.

The journal captures substantive thoughts, learnings, and ideas worth
persisting and (later) linking. It writes dated markdown files under
``vault/journal/{date}-{slug}.md`` with YAML frontmatter, and is the first
plugin wired into the kernel's per-turn dispatch (issue #2).

The plugin contract:

  - ``handler.write(intent, message, session, ...)`` -> JournalWriteResult
  - ``handler.read(...)`` is deferred to issue #3.

Idempotency: every write derives a content-sha256 id. Re-submitting the
same input is a no-op that returns the same path.
"""
