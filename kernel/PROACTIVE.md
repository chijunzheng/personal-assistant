# Proactive Layer — Digests, Reminders, Suggested Actions

The system has two loops: **reactive** (Telegram message → response) and
**proactive** (cron → kernel → Telegram outbound). This file documents the
proactive loop.

## Three things the proactive loop does

### 1. Daily digest *(8am, configurable)*

```
launchd: 8am
  └─ kernel/proactive.py --task daily-digest
       ├─ window = "yesterday"
       ├─ for each domain with digest.cadence == "daily":
       │    sections.append(plugin.digest.summarize(window))
       ├─ digest_text = compose(sections)
       ├─ if config.suggested_actions:
       │    digest_text = llm_pass(digest_text)   # "what should the user notice / consider?"
       ├─ telegram.send(digest_text)
       └─ audit.log("daily_digest_sent", ...)
```

What's in a daily digest:
- Yesterday's spending highlights (finance plugin, daily slice)
- Low-stock items (inventory plugin)
- Reminders fired in the last 24h
- Items added to `_inbox/` (count + first-line preview)
- Optional LLM-suggested actions ("you spent 30% more on coffee this week — consider…")

### 2. Weekly digest *(Sunday 6pm, configurable)*

Same plumbing, `window = "last 7 days"`. Calls each domain's weekly summarize.
Includes:
- Weekly spending breakdown by category
- Journal themes (topic momentum from INDEX.md)
- Inventory consumption rates
- **Inbox triage prompt** — the safety-net drain. Lists `_inbox/` items with
  numbers; user replies with classifications via Telegram.

### 3. Reminder dispatcher *(every 5 min)*

```
cron: every 5 min
  └─ kernel/proactive.py --task check-reminders
       ├─ events = load(vault/reminder/events.jsonl, status="pending")
       ├─ for each event:
       │    fire = (event.kind == "scheduled" and event.fire_at <= now)
       │           or (event.kind == "state_derived" and eval(event.condition))
       │    if fire:
       │        telegram.send(event.message)
       │        if event.recurrence:
       │            schedule_next(event)
       │        else:
       │            mark_fired(event)
       └─ audit.log
```

The unified dispatcher handles both kinds:
- **Scheduled**: fires when `fire_at <= now`
- **State-derived**: re-evaluates a condition (e.g.,
  `"inventory.state.AAA_batteries.quantity == 0"`) every 5 min; fires when true

State-derived conditions are stored as expression strings, evaluated by the
kernel's vault-query API (NOT `eval()` on raw Python — that's an injection
vector). Allowed expressions: comparisons over `inventory.state.*`,
`finance.transactions.*` aggregates, dates.

## Why reminders are a domain plugin (not kernel)

Following the cardinal rule from `CLAUDE.md`: new feature = new plugin.
Reminders are technically just structured events with a fire condition. The
*only* kernel-level coupling is the cron entry point — and that's a small
contract: kernel runs `domains/reminder/handler.py:check_due()` every 5 min.

This means future proactive features (e.g., "weekly retrospective prompt that
asks me reflective questions") are also plugins, not kernel changes.

## The inbox triage loop

`_inbox/` accumulates items the classifier flagged as ambiguous. Without a
consumption loop, this graveyard quietly defeats the "save selectively" policy.

The weekly digest drains it:

```
Weekly digest message (Sunday 6pm) ends with:

  "📥 Inbox (7 items this week):
    1. [2026-04-25] 'wonder if there's a paper on consciousness in agents'
    2. [2026-04-26] '$47 amex charge from BURGER...'
    3. ...

   Reply with classifications:  '1 journal, 2 finance, 3 drop'"

User replies → kernel parses → routes each item to its domain's write() →
moves the file out of _inbox/ → audit.log
```

If user doesn't reply within 48h of a weekly digest, items stay in `_inbox/`
and reappear in next week's digest. After 4 unanswered weeks, they're auto-
archived to `_inbox/_archive/<year>/` (still recoverable, but out of digest).

## Configuration

Add to `configs/default.yaml` (and add `suggested_actions: false` to baseline):

```yaml
proactive:
  daily_digest:
    enabled: true
    time: "08:00"
    timezone: "America/Vancouver"
  weekly_digest:
    enabled: true
    day: "Sunday"
    time: "18:00"
  reminder_check_interval_min: 5
  inbox_archive_after_weeks: 4
  suggested_actions: true   # 7th engineering decision; off in baseline
```

## What baseline does without `suggested_actions`

Daily/weekly digests still fire, but they're pure data dumps — no LLM-driven
"what should you notice?" pass. The eval will compare digest usefulness with
vs without this pass. If the engineered system measurably scores higher on the
"trust" and "connection" axes for digest content, that's the case for keeping
the LLM pass on.
