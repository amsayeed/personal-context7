---
title: "<Short, specific name — used as the doc title in resolve_topic>"
domain: system-design          # data | ai | system-design | arch-patterns
source_type: own-note          # book | blog | paper | adr | own-note
trust_tier: 2                  # 0 archive · 1 reference · 2 canonical · 3 your synthesis
tags: [tag1, tag2]             # front-matter tags; inline #tags in body are merged in
aliases: []                    # optional: alternate names (not indexed yet, useful for Obsidian)
summary: ""                    # one sentence; indexed into every chunk
key_concepts: []               # concepts the note should resolve for
canonical_for: []              # topics this note should win in resolve_topic
canonical_questions: []        # questions this note should answer
last_reviewed: 2026-05-20      # used by pkb doctor
freshness_status: current      # current | stale | evergreen | archived
created: 2026-05-20
---

# <Same as title, or a punchier rephrasing>

> One-sentence summary of the punchline. The embedding model attends to this hard —
> make it concrete: what is true, when does it apply, what does it cost.

## Context

Where does this come up? What problem prompts it?

## The idea

State the pattern, theorem, or trade-off. Prefer crisp definitions over hedged prose.

## Trade-offs

| Choice          | Wins                                              | Costs                                       |
| --------------- | ------------------------------------------------- | ------------------------------------------- |
| Option A        | …                                                 | …                                           |
| Option B        | …                                                 | …                                           |

## When to reach for it

- Specific situation 1
- Specific situation 2

## When NOT to

- The trap that looks like it but isn't
- The case where the cost outweighs

## Worked example

A short concrete example. Code is fine — `pkb`'s chunker handles fenced blocks.

```python
# pseudocode is fine; keep it tight
def example():
    ...
```

## Related

- [[Other note in the vault]] — short why the link matters
- [[Yet another]] — …

## Sources

- Author, *Title*, ch. N — page or section
- https://link.to/blog or paper

---

<!--
HOW TO USE THIS TEMPLATE
========================

1. Copy this file to the right folder (e.g. system-design/cap.md).
2. Edit the front matter — esp. trust_tier. Be deliberate.
3. Fill `summary`, `canonical_for`, and `canonical_questions` on important notes.
   pkb indexes those fields into every chunk, which makes topic resolution much sharper.
4. Headings matter — pkb prepends "H1 > H2 > H3" to each chunk before embedding,
   so specific headings act as a free retrieval signal.
5. Inline #tags in the body are picked up automatically — use them for in-section
   topics without bloating the front matter list.
6. Lead each section with the conclusion. The first paragraph dominates the chunk.

TIER GUIDANCE
=============

3  Your own synthesis. Notes where you connected ideas across sources.
2  Canonical sources you trust. ADRs, foundational papers, top-quality book chapters.
1  Reference material. Blog posts, talks. The default if you're unsure.
0  Archive. Old drafts, half-thoughts. Still searchable but down-weighted.

-->
