# Kernel Runtime — Language, Layout, and Tooling

## Language

**Python 3.12+.** Reasons:

- `python-telegram-bot` is the most mature Telegram polling library
- Subprocess invocation of `claude -p` is trivial in Python
- YAML/JSONL/markdown handling is in stdlib + small deps (`pyyaml`, `pydantic`)
- Most Anthropic-ecosystem tooling examples are Python-first
- Single-language stack is one less moving part for a personal project

Not Node/Rust/Go: no upside that justifies the deviation.

## Package layout

```
personal-assistant/
├── kernel/
│   ├── __init__.py
│   ├── orchestrator.py       # entrypoint per Telegram turn
│   ├── classifier.py         # intent classification via claude -p
│   ├── retrieval.py          # tiered retrieval, INDEX-first, expansion, backlinks
│   ├── session.py            # active session manager
│   ├── audit.py              # append-only audit log writer
│   ├── vault.py              # atomic_write, mtime guard, append helpers
│   ├── index.py              # INDEX.md refresh job (every 5 writes)
│   ├── proactive.py          # cron entrypoint: digests, reminder dispatch
│   ├── telegram_bridge.py    # polling loop, message dispatch
│   ├── conflict_watcher.py   # Drive conflict detector + merger
│   ├── claude_runner.py      # subprocess wrapper for `claude -p`
│   └── prompts/
│       ├── system.md
│       ├── classifier.md
│       └── digest_suggestions.md
├── domains/<name>/
│   ├── domain.yaml
│   ├── prompt.md
│   ├── handler.py
│   ├── digest.py             # optional
│   └── eval/cases.jsonl
├── eval/
│   ├── run.py                # head-to-head harness
│   ├── score.py              # 5-dim Likert scorer (LLM-as-judge + manual)
│   ├── cases/                # consolidated cases harvested from domains
│   └── results/<config>-<timestamp>.json
├── configs/
│   ├── default.yaml
│   └── baseline.yaml
└── pyproject.toml
```

## Dependencies (pinned in `pyproject.toml`)

Minimal:
- `python-telegram-bot ~= 21.x` — polling bot
- `pyyaml` — config + state files
- `pydantic ~= 2.x` — schema validation for plugins
- `tenacity` — retry on Claude API/Telegram transient failures
- `python-dateutil` — date math for time-bound queries
- `rich` — pretty CLI output for eval reports

No vector DB, no embedding library. (See `kernel/RETRIEVAL.md`.)

## Process model

**Single Python process** runs the Telegram polling loop.
Per-turn flow:

```
1. telegram_bridge.poll() → message
2. orchestrator.handle(message)
     a. session.load_or_create(chat_id, message_ts)
     b. classifier.classify(message) via claude_runner
     c. dispatch to domain plugin's handler.write or handler.read
     d. retrieval.gather_context(query, config)
     e. claude_runner.invoke(system + context + user_msg)
     f. vault.atomic_write any persisted output
     g. audit.log all of the above
     h. telegram_bridge.send(reply)
3. if writes >= 5 since last index refresh:
     index.refresh()  (inline, blocks ~2-4s)
```

**Cron jobs** (separate `python -m kernel.proactive` invocations):
- 8am daily — daily digest
- Sunday 6pm — weekly digest
- Every 5 min — reminder dispatcher
- Every 1 min — conflict watcher

## Token telemetry (eval requirement)

`kernel/claude_runner.py` parses Claude's `usage` field from each invocation
response and writes it to the audit entry's `tokens_in` / `tokens_out` fields.
The eval harness (`eval/run.py`) aggregates these from audit logs to produce
the per-turn token chart.

**Rule:** Every `claude -p` invocation logs token counts. No exceptions.

## launchd plists (Mac mini scheduling)

Stored in `infra/launchd/`:
- `com.jasonchi.pa.daily-digest.plist` — 8am daily
- `com.jasonchi.pa.weekly-digest.plist` — Sunday 6pm
- `com.jasonchi.pa.reminder-check.plist` — every 5 min
- `com.jasonchi.pa.conflict-watcher.plist` — every 1 min
- `com.jasonchi.pa.bot.plist` — keep-alive for the polling loop (auto-restart on crash)

Install with `launchctl load`.

## Secrets

- `TELEGRAM_BOT_TOKEN` — set in the launchd plist EnvironmentVariables
- `ANTHROPIC_API_KEY` — already configured for `claude -p` via Claude Code's
  default keychain. Kernel inherits it; do not re-store.

Never write secrets to vault, audit log, or git.

## Single-instance enforcement

`kernel/orchestrator.py` acquires a flock on `/tmp/personal-assistant.lock`
on startup. Second instance refuses to start. Prevents accidental double-bot
running both polling, which would race the audit log and Telegram replies.
