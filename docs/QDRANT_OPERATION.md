# Qdrant Operation Guide

Qdrant is the recommended dense-vector backend once the KB grows beyond a small personal index.
SQLite remains required for document metadata, chunk text, and BM25 lexical retrieval.

## Recommended Production Shape

```text
Obsidian vault -> private GitHub repo -> Railway pkb service
                                      -> SQLite /data/kb.db for metadata + BM25
                                      -> Qdrant for dense vectors
```

Use Qdrant Cloud when you want a managed vector database. A separate Railway
Qdrant service is also viable when you want the whole stack in one Railway project;
use a private service URL, an API key, and a persistent volume mounted at
`/qdrant/storage`.

## Environment

Set these on Railway and locally when using Qdrant:

```bash
PKB_VECTOR_BACKEND=qdrant
PKB_QDRANT_URL=https://YOUR-CLUSTER.qdrant.io
PKB_QDRANT_API_KEY=...
PKB_QDRANT_COLLECTION=pkb_chunks
```

Optional:

```bash
PKB_QDRANT_TIMEOUT=20
PKB_VEC_TOPK=50
PKB_BM25_TOPK=50
PKB_FINAL_TOPK=12
```

## Local Development

Run Qdrant locally:

```bash
docker run --rm -p 6333:6333 -p 6334:6334 qdrant/qdrant
```

Then:

```bash
export PKB_VECTOR_BACKEND=qdrant
export PKB_QDRANT_URL=http://localhost:6333
export PKB_QDRANT_COLLECTION=pkb_dev
pkb qdrant-check
pkb build
pkb smart "event sourcing tradeoffs" --json
```

For fast smoke tests only:

```bash
PKB_VECTOR_BACKEND=qdrant PKB_QDRANT_URL=:memory: pkb qdrant-check
```

Do not use `:memory:` for real indexing; it disappears when the process exits.

## Migration From SQLite Vectors

1. Keep `PKB_DB_PATH` and `/data` volume unchanged.
2. Add Qdrant variables.
3. Run `pkb qdrant-check`.
4. Run `pkb qdrant-backfill --recreate` to copy the existing SQLite vectors.
5. Run `pkb eval evals/questions.jsonl --k 10 --min-recall 0.9`.
6. Switch the hosted MCP service to `PKB_VECTOR_BACKEND=qdrant`.

The SQLite vector table can remain as fallback. It is not used for dense retrieval when
`PKB_VECTOR_BACKEND=qdrant`.

## Railway

Set variables without printing secrets:

```bash
cd /Volumes/SSD-2/Ahmed-KB/personal-context7
railway variable set PKB_VECTOR_BACKEND=qdrant --service personal-context7
railway variable set PKB_QDRANT_COLLECTION=pkb_chunks --service personal-context7
railway variable set PKB_QDRANT_URL --stdin --service personal-context7
railway variable set PKB_QDRANT_API_KEY --stdin --service personal-context7
railway up --detach --service personal-context7 --message "Enable Qdrant backend"
```

For a Qdrant service inside the same Railway project:

```bash
railway add --image qdrant/qdrant:v1.17.1 --service qdrant --json
railway volume --service <qdrant-service-id> add --mount-path /qdrant/storage --json
railway variable set QDRANT__SERVICE__API_KEY --stdin --service qdrant --skip-deploys
railway variable set QDRANT__LOG_LEVEL=INFO --service qdrant --skip-deploys
railway variable set 'PKB_QDRANT_URL=http://${{qdrant.RAILWAY_PRIVATE_DOMAIN}}:6333' --service personal-context7 --skip-deploys
railway variable set 'PKB_QDRANT_API_KEY=${{qdrant.QDRANT__SERVICE__API_KEY}}' --service personal-context7 --skip-deploys
railway variable set PKB_VECTOR_BACKEND=qdrant PKB_QDRANT_COLLECTION=pkb_chunks --service personal-context7 --skip-deploys
```

Verify:

```bash
railway logs --service personal-context7 --lines 120
curl -fsSL -H "Authorization: Bearer $PKB_API_KEY" \
  https://personal-context7-production.up.railway.app/stats
```

Backfill Qdrant on the hosted service without SSH:

```bash
curl -fsSL -X POST \
  -H "Authorization: Bearer $PKB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"recreate":true,"batch_size":256}' \
  https://personal-context7-production.up.railway.app/webhook/qdrant-backfill
```

`/stats` should show:

```json
{
  "vector_backend": "qdrant",
  "qdrant_collection": "pkb_chunks"
}
```

## Failure Modes

- `qdrant-client missing`: install dependencies with `uv pip install -e .`.
- `collection dimension mismatch`: create a new collection or keep `PKB_EMBED_DIM` consistent with the embedding model.
- `no vector hits`: run `pkb qdrant-backfill --recreate` or `pkb build`; sync only indexes changed files.
- slow retrieval: reduce `PKB_VEC_TOPK`, `PKB_BM25_TOPK`, or disable rerank temporarily with `PKB_RERANK=false`.
- poor quality: add eval cases before tuning. Do not tune by anecdote.
