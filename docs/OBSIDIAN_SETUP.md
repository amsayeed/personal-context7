# Preparing your Obsidian vault for `pkb`

Your vault doesn't need to change much. What follows is the set of conventions that make `pkb`'s metadata-aware retrieval actually work — and a few things to *avoid* because they degrade chunk quality.

## TL;DR

1. Put the vault in a private Git repo. Push regularly. `pkb` pulls from there.
2. Add a four-line front-matter block to every note you want retrievable.
3. Keep a stable top-level folder structure — folders become a filter dimension.
4. Don't put your attachments folder, daily notes scratch, or templates in the index.

## Vault layout

```
notes/                       ← repo root (= PKB_KB_ROOT on the server)
├── data/                    ← top-level folder = domain (filter via folders=['data'])
│   ├── lakehouse.md
│   └── streaming-vs-batch.md
├── ai/
│   ├── rag-patterns.md
│   └── inference-architectures.md
├── system-design/
│   ├── caching.md
│   └── cap-and-consistency.md
├── arch-patterns/
│   ├── event-sourcing.md
│   └── cqrs.md
├── books/                   ← highlights from books
│   ├── ddia-highlights.md
│   └── designing-data-intensive-applications-ch5.md
├── adrs/                    ← architecture decision records
│   └── 0001-event-bus-choice.md
├── _attachments/            ← skipped by pkb
├── templates/               ← skipped by pkb
└── .obsidian/               ← skipped by pkb
```

The four top-level folders that map cleanly to the `domain` filter — `data`, `ai`, `system-design`, `arch-patterns` — give you the strongest retrieval signal. Adjust naming to whatever you already use; just be consistent.

Folders `_attachments/`, `attachments/`, `templates/`, `.obsidian/`, `.trash/`, and `.git/` are automatically excluded by the walker. You don't need to configure anything.

## Front matter

Every note that should be retrievable starts with a YAML front-matter block. Minimum useful version:

```yaml
---
title: CAP theorem and PACELC
domain: system-design     # data | ai | system-design | arch-patterns
source_type: own-note     # book | blog | paper | adr | own-note
trust_tier: 2             # 0=archive, 1=reference, 2=canonical, 3=synthesis
tags: [distributed-systems, consistency]
summary: "CAP says partitions force a choice between availability and linearizable consistency."
aliases: [CAP, PACELC]
key_concepts: [partition tolerance, availability, consistency]
canonical_for: [CAP theorem, distributed consistency]
canonical_questions:
  - "When does CAP matter?"
  - "How does PACELC extend CAP?"
last_reviewed: 2026-05-20
freshness_status: evergreen
---
```

What each field actually does:

- **`title`** — used as the document name in `resolve_topic` results. If absent, `pkb` falls back to the first H1, then to the filename.
- **`domain`** — hard-filterable. Pass `domain="data"` to a search tool and only `data/` notes come back.
- **`source_type`** — hard-filterable. Useful for "only my own synthesis" (`source_type="own-note"`) or "only books" (`source_type="book"`).
- **`trust_tier`** — *soft boost*. Higher-tier notes get a score multiplier (1.5× at tier 3, 1.2× at tier 2, 1.0× at tier 1, 0.6× at tier 0). This is the most important field — it's how you tell the system *"my own synthesis beats raw highlights, raw highlights beat archived notes."*
- **`tags`** — front-matter tags become filterable. Inline `#tags` in the body are *also* picked up and added to this set automatically.
- **`summary`** — indexed into every chunk and returned in JSON tools. Give canonical notes a blunt one-sentence answer.
- **`aliases`** — alternate names and acronyms. These are indexed and help `resolve_topic` match user phrasing.
- **`key_concepts`** — concepts this note covers. Use these for broad recall.
- **`canonical_for`** — topics this note should win for. This is the strongest manual topic-resolution hint.
- **`canonical_questions`** — natural-language questions the note should answer. Useful for eval design and query matching.
- **`last_reviewed` / `freshness_status`** — used by `pkb doctor` and returned by `/stats`/JSON tools.

### Tier guidance (the rule of thumb)

| Tier | What lives there                                                                              |
| ---- | --------------------------------------------------------------------------------------------- |
| 3    | Your own *synthesized* notes — the ones where you connected ideas across sources.             |
| 2    | Canonical sources you trust deeply — DDIA highlights, your team's ADRs, foundational papers.  |
| 1    | Reference material — blog posts, talks, second-tier books. The default.                       |
| 0    | Archive — old drafts, half-thoughts, "I might look at this someday." Down-weighted but kept.  |

Be deliberate about tier 3. The whole point is that *your* synthesis outranks everything else when relevant.

### Tags vs domain vs folder

These overlap. The right mental model:

- **Folder** = top-level category. Stable, slow-changing. Use for hard filters.
- **Domain** = redundant with folder in most setups but lets you put a file in `books/` and still tag its domain as `data`.
- **Tags** = the long tail. Concepts, technologies, projects. Granular and many per note.

If folder == domain in your vault, just set `domain` to match your folder. The redundancy doesn't hurt and lets you reorganize folders later without breaking filters.

## Writing well-retrievable notes

A few habits that make retrieval better, independent of `pkb`:

**Heading hygiene.** `pkb` chunks at heading boundaries and prepends the H1→H2→H3 path to each chunk before embedding. That means *the words in your headings act as a query signal*. Use specific headings: "Trade-offs under network partition" beats "Trade-offs".

**One concept per note.** A 200-line note covering five topics produces five overlapping chunks competing for the same query. Split it. Cross-link with `[[wikilinks]]` — they stay as text, fully searchable.

**Lead with the conclusion.** Embedding models attend more strongly to the first paragraph of each section. If you have a punchline, put it first; reasoning below.

**Inline tags for in-section topics.** `#caching` inside a CAP note adds a retrieval hook for that section without polluting the front matter. They're aggregated into the doc's tag set automatically.

## Quality loop

Run the doctor after editing batches of notes:

```bash
pkb doctor
pkb doctor --json
```

It checks missing front matter, unknown domains/source types, stale reviews, duplicate titles, broken wikilinks, empty notes, and over-large chunks.

To customize allowed domains/source types, put `.pkb-vocab.json` at the root of your notes repo. Start from `docs/VOCAB.example.json`.

Keep a small retrieval eval file in your notes or this repo:

```jsonl
{"question":"When should I use event sourcing?","expected_sources":["arch-patterns/event-sourcing.md"]}
{"question":"How does PACELC extend CAP?","expected_sources":["system-design/cap-and-consistency.md"]}
```

Then run:

```bash
pkb eval evals/questions.jsonl
```

The report includes recall@k and MRR. Add questions whenever an agent misses an answer you expected it to find.

## Book/doc ingestion flow

For a book split into chapter-by-chapter markdown, treat the book folder as one
ingestion unit. First generate missing front matter in-place, then edit it in
Obsidian's Properties UI, then validate, then index only that folder:

```bash
export PKB_KB_ROOT="/path/to/obsidian/wiki"

pkb ingest annotate "/path/to/obsidian/wiki/Agents/30 Agents Every AI Engineer Must Build" \
  --domain ai \
  --source-type book \
  --trust-tier 2 \
  --tag agents \
  --tag ai-engineering \
  --collection "30 Agents Every AI Engineer Must Build"

pkb ingest check "/path/to/obsidian/wiki/Agents/30 Agents Every AI Engineer Must Build"
pkb ingest index "/path/to/obsidian/wiki/Agents/30 Agents Every AI Engineer Must Build"
```

`annotate` only fills missing metadata unless `--overwrite` is passed. It is safe
to run with `--dry-run` first. `index` refuses to ingest files that are missing
`title`, `domain`, `source_type`, or `trust_tier`; the markdown remains the source
of truth. SQLite and Qdrant are rebuildable indexes.

Set `PKB_REQUIRE_METADATA=true` on the hosted service when you want regular
`pkb build`, `pkb sync`, and `/webhook/sync` to enforce the same required
front-matter gate.

For hundreds of books, do not bulk-ingest everything at once. Ingest 5-10 books,
add eval fixtures for the questions you care about, run `pkb eval`, then continue.
Retrieval quality needs a feedback loop; storage alone will not prevent noisy
or contradictory results.

## What `pkb` doesn't do (yet)

- It doesn't resolve `[[wikilinks]]` to follow citation graphs. They embed as text, which gives partial credit.
- It doesn't read PDFs, EPUBs, or images. Convert highlights to markdown first (Obsidian's *Annotator*, *Readwise sync*, or just paste).
- It doesn't sync media (`_attachments/`). Images are fine in your vault but aren't searchable.

## The git side

Your vault is the source of truth. `pkb` pulls from a private GitHub repo:

```bash
cd ~/notes
git init
git add .
git commit -m "initial vault snapshot"

# Create a private repo on GitHub, then:
git remote add origin git@github.com:you/notes.git
git push -u origin main
```

When you deploy, you'll give `pkb` a tokenized HTTPS URL so the server can pull:

```
https://x:GITHUB_TOKEN@github.com/you/notes.git
```

Generate a fine-grained personal-access token with `Contents: read` on the notes repo. Don't reuse a token that has write access to other things.

## Sync rhythm

Three reasonable patterns; pick one:

1. **Manual after writing.** `git push` from your laptop, then `curl -X POST .../webhook/sync` to trigger reindex on the server. Most control, slowest.
2. **GitHub Action on push.** Add a workflow that POSTs to `/webhook/sync` whenever you push to `main`. Set-and-forget.
3. **Cron on the server.** A scheduled hourly `/webhook/sync` call. Lazy but works. The `pkb` index is incremental so this is cheap.

A GitHub Action example lives in `docs/AGENT_INTEGRATION.md`.
