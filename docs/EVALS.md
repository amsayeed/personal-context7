# Retrieval And Decision Evals

Evals are mandatory before scaling to hundreds of books. A large KB can look useful while
silently retrieving the wrong chapter or mixing weak sources into decisions.

## Local Retrieval Evals

Create JSONL fixtures:

```jsonl
{"question":"When should I use event sourcing?","expected_sources":["arch-patterns/event-sourcing.md"],"expected_terms":["auditability","replay"]}
{"question":"How does PACELC extend CAP?","expected_sources":["system-design/cap-and-consistency.md"],"expected_terms":["latency","else consistency"]}
```

Run:

```bash
pkb eval evals/questions.jsonl --k 10 --min-recall 0.9 --output evals/latest-report.json
```

Metrics:

- `recall_at_k`: at least one expected source appeared in top `k`.
- `mrr`: expected source appeared early, not merely somewhere.
- `avg_source_coverage`: fraction of expected sources retrieved.
- `avg_term_coverage`: expected terms appeared in retrieved evidence.

Recommended gates:

```text
small KB:     recall@10 = 1.00, MRR >= 0.80
large KB:     recall@10 >= 0.90, MRR >= 0.60
decision KB:  every high-value architecture question gets a hand-written eval
```

## Paid Eval Platform Recommendation

Use Braintrust with Autoevals for experiment tracking and LLM-as-judge scoring.

Why Braintrust:

- model-agnostic experiment tracking
- datasets and traces
- local Python evals can be logged to a hosted dashboard
- Autoevals provides factuality-style scorers

Keep local deterministic retrieval evals as the source of truth for retrieval regressions.
Use Braintrust for history, dashboards, model comparisons, and judged answer quality.

This repo logs `pkb eval` aggregate results to Braintrust when these variables are set:

```bash
BRAINTRUST_API_KEY=...
BRAINTRUST_PROJECT_ID=...
PKB_BRAINTRUST_ENABLED=true
```

Use `--no-braintrust` for a local-only run.

## Decision Evals

For architecture decisions, retrieval recall is not enough. Add cases that test whether
the system finds conflicting evidence and makes a complete recommendation.

Suggested fixture shape:

```json
{
  "question": "Should this system use event sourcing or CRUD?",
  "expected_sources": [
    "architecture/event-sourcing.md",
    "architecture/crud.md"
  ],
  "expected_terms": [
    "auditability",
    "replay",
    "operational complexity",
    "simple administrative workflows"
  ]
}
```

The decision process should:

1. retrieve from multiple books/chapters
2. extract claims and tradeoffs
3. group agreement and conflict
4. weight by `trust_tier`, source type, edition/year, and direct relevance
5. cite the exact source paths/headings
6. state assumptions and missing evidence

The MCP tool `decision_evidence_json` is built for this. It returns evidence plus a
decision protocol; the calling model still writes the final decision memo.

## Tuning Order

Tune in this order:

1. metadata quality
2. chunking and summaries
3. query decomposition
4. `PKB_BM25_TOPK` / `PKB_VEC_TOPK`
5. reranker model
6. embedding model
7. vector database settings

Do not tune vector DB settings before metadata and evals are clean.
