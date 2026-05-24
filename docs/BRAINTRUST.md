# Braintrust Observability And Evals

Braintrust is used as the hosted experiment and trace dashboard for PKB. Local
`pkb eval` remains the hard retrieval gate; Braintrust stores history, charts,
production traces, and score trends.

## Current Setup

Railway variables:

```bash
BRAINTRUST_API_KEY=...
BRAINTRUST_PROJECT_ID=80cee56f-46c6-4b0e-bad8-762179f7c3b7
PKB_BRAINTRUST_ENABLED=true
PKB_BRAINTRUST_SAMPLE_RATE=1.0
PKB_BRAINTRUST_LOG_TEXT=false
```

`PKB_BRAINTRUST_LOG_TEXT=false` is deliberate. It logs source paths and retrieval
metadata, not full book chunks.

## What Gets Captured

For MCP retrieval tools:

- tool name
- user query or decomposed queries
- filters
- vector backend and collection
- rerank status
- latency
- returned source paths, titles, headings, scores, retriever legs, trust tier

For `pkb eval`:

- eval file path
- `k`, `min_recall`, `min_mrr`
- `recall_at_k`
- `mrr`
- source coverage
- expected-term coverage
- failed cases with expected and returned source paths

## What Is Not Captured By Default

- full markdown chunks
- whole books
- private note bodies
- API keys
- GitHub tokens

If you explicitly need text previews for offline debugging, enable:

```bash
PKB_BRAINTRUST_LOG_TEXT=true
PKB_BRAINTRUST_TEXT_MAX_CHARS=500
```

Do not enable text logging for routine production traffic.

## Run Local Evals And Log To Braintrust

```bash
export BRAINTRUST_API_KEY=...
export BRAINTRUST_PROJECT_ID=80cee56f-46c6-4b0e-bad8-762179f7c3b7
export PKB_BRAINTRUST_ENABLED=true

.venv/bin/pkb eval evals/books.jsonl \
  --k 10 \
  --min-recall 0.9 \
  --min-mrr 0.6 \
  --output evals/latest-report.json
```

Disable hosted logging for a local dry run:

```bash
.venv/bin/pkb eval evals/books.jsonl --no-braintrust
```

## Starter Plan Guidance

The Starter plan is enough while the payloads stay sanitized. Keep:

```bash
PKB_BRAINTRUST_LOG_TEXT=false
PKB_BRAINTRUST_SAMPLE_RATE=1.0
```

If traffic grows, reduce production sampling:

```bash
PKB_BRAINTRUST_SAMPLE_RATE=0.1
```

Use 100% sampling for local eval runs and 10-25% for high-volume production MCP
traffic.

## How This Improves The System

Use Braintrust to compare:

- chunking settings
- BM25/vector top-k
- reranker on/off
- embedding model changes
- metadata quality before and after book ingestion
- Qdrant vs SQLite vector behavior

The workflow:

1. Add or update eval cases in JSONL.
2. Run `pkb eval`.
3. Inspect Braintrust experiment traces and scores.
4. Change one retrieval setting.
5. Run the same eval again.
6. Keep changes that improve recall/MRR without hurting term coverage.

For final answer quality later, add Braintrust Autoevals or custom scorers for:

- context recall
- context precision
- faithfulness
- answer relevancy
- decision completeness

Retrieval correctness should still be measured first with deterministic `pkb eval`
fixtures.
