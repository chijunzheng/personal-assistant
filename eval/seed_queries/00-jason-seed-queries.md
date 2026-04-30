# Seed Queries — Jason

Purpose: anchor the eval taxonomy. These are real things I'd want to ask a
second-brain right now, today, before any system exists. Each query later maps
to one or more `.md` notes that *should* be retrieved — those mappings become
`recall.jsonl`.

## How to use this file

- Write 10–15 queries below. Don't censor — vague is good, time-bound is good.
- Tag each with `[recall]`, `[link]`, `[finance]`, `[inventory]`, `[meta]`,
  `[vague]`, `[time-bound]`, or `[multi-hop]`. Multiple tags allowed.
- Don't worry if the answer doesn't exist in the vault yet — these queries
  drive *what* gets seeded into the vault.

## Taxonomy reference

| Tag | Meaning | Why it matters for eval |
|---|---|---|
| `[recall]` | "What did I write about X?" | Tests basic memory retrieval |
| `[link]` | "What does note A connect to?" | Tests the linking layer |
| `[finance]` | Spending / budget / category questions | Tests structured-data reasoning |
| `[inventory]` | "Do I still have X?" "What ran out?" | Tests state-tracking |
| `[meta]` | "What have I been thinking about lately?" | Tests aggregation across vault |
| `[vague]` | Underspecified, uses fuzzy referents | Tests semantic retrieval beyond keywords |
| `[time-bound]` | "Last month / three weekends ago / before X" | Tests temporal grounding |
| `[multi-hop]` | Requires connecting >1 note | Tests retrieval depth |

## Queries

<!-- Draft generated from stated use cases + visible context. EDIT these.
     The act of swapping a query for one you'd actually type is the high-value
     calibration step — that's what teaches the system how YOU phrase things. -->

### Journaling / learning (6)

1. `[recall][vague]` what was that thing about MemGPT memory tiers I jotted down a while back
2. `[multi-hop][link]` how does the iterative-retrieval pattern from my NEXUS work connect to what I'm building here
3. `[meta][time-bound]` what RAG / agent concepts have I been wrestling with this month
4. `[vague][recall]` that idea I had about evals being a forcing function on schema design — find it
5. `[link]` show me everything I've journaled that touches on agent memory architectures
6. `[time-bound]` what was I anxious about three weekends ago

### Finance (4)

7. `[finance][time-bound]` how much did I spend on coffee shops last month vs the month before
8. `[finance]` flag any charge over $100 from the last statement that doesn't match a recurring pattern
9. `[finance][meta]` am I on track with my food spending this month
10. `[finance][time-bound]` what subscription charges have I been paying that I forgot about

### Inventory / household (3)

11. `[inventory]` do I still have those AAA batteries I picked up at Costco
12. `[inventory][meta]` what's running low across the kitchen right now
13. `[inventory]` build my grocery list for this weekend based on what I'm out of

### Cross-cutting / meta (2)

14. `[meta]` what topics keep recurring across my journal entries this quarter
15. `[multi-hop][link]` what does my note on "deep work" connect to in my interview prep notes

## Notes for me as I write these

- The *shape* of these queries reveals what categories the memory system
  must support. If 8/15 are `[finance]`, finance retrieval gets prioritized.
- The *vocabulary* I use here teaches the agent how I phrase things. Don't
  translate into "system-friendly" language — write how I'd actually type
  them at 11pm into Telegram.
- A query I'd be embarrassed to ask aloud is probably the most useful one,
  because it captures real friction in my current memory workflow.
