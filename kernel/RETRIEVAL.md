# Retrieval Architecture — Pure Agentic Tool Calling

No embeddings. No vector DB. No ANN index. The agent navigates the vault using
filesystem tools + structural conventions + LLM-driven keyword expansion at
query time.

## Why this and not RAG

1. **Zero infra.** Claude Code already has Read, Grep, Glob, Bash. The vault is
   on disk via Drive Desktop. There is nothing to set up.
2. **No drift.** Filesystem state IS the index. No re-embedding job, no version
   skew between vault and index.
3. **Time-bound queries are free.** Filenames encode dates (`2026-04-29-*.md`),
   so `ls vault/journal/2026-03-*.md` answers "last month" without an LLM call.
4. **Multi-hop is native.** Agent reads → reasons → reads again. Iterative
   retrieval without an orchestration layer.
5. **Cost reverses for personal use.** RAG amortizes cost (embed once, query
   many). Agentic spends per query. Personal-vault traffic is low-volume +
   high-novelty, which favors agentic.

## How "semantic recall" works without embeddings

The naive worry: grep can't match "coffee" to "cafe."

The reality: the agent isn't doing raw grep. It's doing **LLM-driven keyword
expansion → grep → read → reason**:

```
turn:
  user: "what did I think about consciousness?"

  agent step 1: read vault/_index/INDEX.md
                (gives synonym vocabulary for the topic clusters that exist)

  agent step 2: expand_keywords("consciousness")
                → ["consciousness", "self-awareness", "qualia",
                   "subjective experience", "mind", "cognition"]

  agent step 3: grep -i -E "consciousness|self-awareness|qualia|..." vault/

  agent step 4: rank matches by recency × match-density,
                read top 3 files

  agent step 5: if files reference [[wikilinks]] not yet read,
                follow 1-hop and read those too (backlink expansion)

  agent step 6: synthesize answer with citations
```

Semantic understanding lives in the LLM at *query time*, not in a precomputed
vector store. The cost is ~6–8 tool calls per query instead of 1 vector lookup.
Trade-off accepted.

## The structural retrieval principle

> **Cheap structural retrieval beats expensive semantic retrieval when the
> structure is already there.**

Structural signals available for free:

| Signal | Where it lives | What it answers |
|---|---|---|
| Filename date prefix | `2026-04-29-*.md` | "last week", "in March", "three weekends ago" |
| Folder | `vault/journal/` vs `vault/finance/` | Domain routing |
| Frontmatter `tags:` | YAML at top of file | Topic filtering |
| Frontmatter `links:` | Maintained by writer | Graph neighbors |
| `[[wikilinks]]` in body | Inline | Backlink expansion |
| `vault/_index/INDEX.md` | Auto-maintained TOC | Vocabulary seed for keyword expansion |
| `vault/_index/active_session.md` | Refreshed each turn | Session continuity |

The engineering decisions in `configs/default.yaml` are essentially: *teach
the agent to use these signals.* The baseline `configs/baseline.yaml` is the
agent without any of those teachings.

## Agent tool palette

See `configs/default.yaml#tools` for the full list. Three classes:

1. **Filesystem** — `read_file`, `grep`, `list_dir`. Available in both configs.
2. **Index** — `read_index`, `read_session`. Engineered config only.
3. **Domain** — `query_finance`, `query_inventory`, etc. Engineered config only.
4. **Expansion** — `expand_keywords`, `read_backlinks`. Engineered config only.

A future use case adds itself to class 3 by dropping a `domains/<name>/handler.py`
that registers its query tools. Kernel never changes.

## Embedding fallback (NOT IMPLEMENTED — design note only)

If pure agentic fails on a category of queries we discover later, we can add
a single tool — `semantic_search(query)` — backed by a local embedding model
(sentence-transformers, ~80MB, runs on the Mac mini, free at inference). The
agent invokes this tool only when grep + index isn't yielding results. This
keeps the architecture clean: embeddings become an *escape hatch tool*, not
a foundational layer.

We do **not** start with this. Build agentic-only first. Add the fallback
only if eval results demand it. (This is itself an EDD trigger — see the
NEXUS-style "promote V3 only if eval fails on category X" pattern.)
