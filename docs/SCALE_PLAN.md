# Scale Plan For Hundreds Of Books

The goal is not just "retrieve similar chunks." The goal is decision-quality evidence
across many books, with citations and a way to detect regressions.

## Target Architecture

```text
Obsidian/Git
  -> pkb ingest annotate/check/index
  -> SQLite: documents, chunks, BM25, eval state
  -> Qdrant: dense vectors + filter payload
  -> MCP agent profile: small retrieval/decision tool surface
  -> eval loop: local gates + Braintrust/Autoevals tracking
```

## Retrieval Algorithm

1. query expansion or decomposition
2. BM25 lexical retrieval from SQLite FTS5
3. dense retrieval from Qdrant
4. RRF fusion
5. trust/metadata boost
6. cross-encoder rerank
7. token-budget packing
8. cited JSON output

For architecture decisions, add:

1. retrieve evidence per option
2. extract claims/tradeoffs
3. group agreement and conflicts
4. weight by `trust_tier`, source type, recency, and direct relevance
5. synthesize the final decision with explicit assumptions

`decision_evidence_json` handles the evidence stage. The calling model handles synthesis.

## Rollout

### Phase 1: Baseline

- Keep current 52 docs.
- Add 20-30 eval questions.
- Run `pkb eval` and save the report.

### Phase 2: First 10 Books

- Ingest 10 books in one topic.
- Require metadata.
- Add eval questions before indexing each topic area.
- Run `pkb eval --min-recall 0.9`.

### Phase 3: Qdrant Production

- Create Qdrant Cloud collection or a dedicated Railway Qdrant service with a persistent `/qdrant/storage` volume.
- Set Railway Qdrant variables.
- Run `pkb qdrant-backfill --recreate` for an existing SQLite index, or `pkb build` for a fresh rebuild.
- Verify `/stats` shows `vector_backend=qdrant`.

### Phase 4: Decision Quality

- Add decision eval fixtures for high-value architecture questions.
- Track reports in Braintrust or Confident AI.
- Add a human review set for final architecture recommendations.

## Quality Gates

Do not bulk-add more books if:

- recall@10 is below 0.90
- MRR falls below 0.60
- retrieved evidence lacks expected terms
- same question returns unstable sources across runs
- model answers cite sources but miss obvious counterarguments

Fix metadata/chunking/evals first, then continue ingestion.
