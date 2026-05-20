# Personal Context7 (`pkb`)

A Context7-style MCP server over **your own markdown KB** — books, notes, architecture playbooks, system-design references. Hybrid retrieval (BM25 + dense embeddings) with Reciprocal Rank Fusion, metadata-aware soft boost, cross-encoder rerank, and multi-hop tools (`multi_search`, `hyde_search`). One SQLite file. Runs locally over stdio or hosted on Railway over SSE behind bearer auth.

```
Obsidian vault
   │  git push
   ▼
GitHub (private repo)
   │  /webhook/sync       (or GitHub Action on push)
   ▼
Railway service  ──►  pulls KB  ──►  pkb sync (incremental)  ──►  kb.db (FTS5 + sqlite-vec)
   │ /sse  bearer
   ▼
your agents (Claude Code, Cowork, Cursor, …)
```

## What's inside

- **Hybrid retrieval** — BM25 (FTS5) + dense (`sqlite-vec` + `BAAI/bge-small-en-v1.5`) fused with Reciprocal Rank Fusion (k=60).
- **Metadata-aware ranking** — `trust_tier` soft-boosts your own synthesis over raw highlights over archive.
- **Cross-encoder rerank** — on by default; ~150ms latency, big quality lift.
- **Multi-hop** — `multi_search` for comparative questions, `hyde_search` when phrasing diverges from notes.
- **Hard filters** — by `tags`, `source_type`, `domain`, `folder`, `min_tier`.
- **Two transports** — stdio (local) or SSE (hosted) over the same code, same tool set.
- **Git-backed KB** — clones a private repo on boot, pulls on `/webhook/sync` or via the `sync` MCP tool.

## Deploy to Railway (one shot)

Prereqs: `railway` CLI, a private GitHub repo with your notes, a GitHub fine-grained token with read access to it.

```bash
git clone <this repo> && cd personal-context7
./deploy.sh
```

`deploy.sh` will:

1. Log in to Railway if needed.
2. Create the project + service.
3. Mount a persistent volume at `/data`.
4. Generate a strong `PKB_API_KEY` (or use yours).
5. Set every env var.
6. Trigger the first build (3–5 min).
7. Hand back a public URL + your API key.

Then read `docs/AGENT_INTEGRATION.md` to wire it into Claude Code / Cowork / Cursor.

## Local-only mode (no Railway)

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
export PKB_KB_ROOT=~/notes
pkb build                # full index (one-time, ~minutes for 5GB)
pkb sync                 # incremental from then on
pkb serve                # MCP stdio — point a local MCP client at this command
```

CLI also has `pkb search "..."`, `pkb topic "..."`, `pkb stats` for poking around.

## How to prepare your vault

See `docs/OBSIDIAN_SETUP.md`. The short version: front matter on every note with `title`, `domain`, `source_type`, `trust_tier`, `tags`. The template lives at `docs/TEMPLATE.md`.

## How to add to agents

See `docs/AGENT_INTEGRATION.md`. SSE URL + bearer header is all most MCP clients need.

## Configuration (env vars)

| Var                     | Default                       | Purpose                                          |
| ----------------------- | ----------------------------- | ------------------------------------------------ |
| `PKB_KB_ROOT`           | `$PKB_DATA_DIR/notes`         | Where your markdown lives on disk.               |
| `PKB_DB_PATH`           | `$PKB_DATA_DIR/kb.db`         | Single-file SQLite database.                     |
| `PKB_DATA_DIR`          | `./data` (Railway: `/data`)   | Parent for db + cache. Mounted as a volume.      |
| `PKB_KB_GIT_REMOTE`     | —                             | `https://x:TOKEN@github.com/you/notes.git`       |
| `PKB_KB_GIT_BRANCH`     | `main`                        |                                                  |
| `PKB_TRANSPORT`         | `stdio`                       | `stdio` or `sse`. Railway uses `sse`.            |
| `PKB_API_KEY`           | (required for SSE)            | Bearer token clients send.                       |
| `PORT`                  | `8000`                        | Railway sets this; uvicorn binds to it.          |
| `PKB_RERANK`            | `true`                        | Cross-encoder rerank on the top-N.               |
| `PKB_EMBED_MODEL`       | `BAAI/bge-small-en-v1.5`      | Any fastembed model id (mind `PKB_EMBED_DIM`).   |
| `PKB_FINAL_TOPK`        | `12`                          | Hits returned to the agent.                      |
| `PKB_TOKEN_BUDGET`      | `4000`                        | Default `tokens` for retrieval tools.            |
| `PKB_TIER_BOOST_{0..3}` | `0.6 / 1.0 / 1.2 / 1.5`       | Score multiplier per `trust_tier`.               |
| `PKB_BM25_TOPK`         | `50`                          | Candidates from FTS5.                            |
| `PKB_VEC_TOPK`          | `50`                          | Candidates from sqlite-vec.                      |
| `PKB_RRF_K`             | `60`                          | RRF constant.                                    |

## Design choices, one-liner each

- **SQLite as the *only* datastore.** FTS5 + sqlite-vec live in the same file. Backup = `cp`. Cold start <100ms.
- **`fastembed` over `sentence-transformers`.** ONNX runtime, no torch, 80MB. CPU is fine at this scale.
- **RRF over weighted-sum fusion.** Score-free, no per-corpus tuning, robust across query types.
- **Heading-aware chunks with the H1>H2>H3 path prepended.** Architecture content puts meaning in headings; we don't throw that away.
- **Bearer auth, constant-time compare.** Single key, simple to rotate.
- **Volume-mounted `/data`** holds the DB, the model cache, and the cloned KB so redeploys are instant.

## Performance notes

- Query latency (200k chunks, hosted): ~50ms hybrid + ~100ms rerank + ~50ms network = ~200ms p50.
- First build of 5GB markdown: ~20–60 min depending on CPUs. Subsequent `sync` only touches changed files.
- Embedding model + reranker together: ~250MB resident. 1GB instance is comfortable.
- `sqlite-vec` brute-force KNN is fine up to ~500k vectors. Past 1M, partition by folder/domain or graduate to LanceDB while keeping FTS5 in SQLite.

## Repo layout

```
personal-context7/
├── Dockerfile                # multi-stage; runtime is ~250MB
├── railway.json              # Railway build/deploy config
├── deploy.sh                 # one-shot deploy: project + volume + vars + push
├── pyproject.toml
├── src/pkb/
│   ├── config.py             # all env vars
│   ├── chunker.py            # heading-aware, Obsidian-friendly
│   ├── store.py              # FTS5 + sqlite-vec + metadata columns
│   ├── embed.py              # fastembed wrapper
│   ├── indexer.py            # build + sync pipelines
│   ├── retriever.py          # RRF + tier boost + rerank + multi/hyde
│   ├── sync.py               # git pull + incremental index
│   ├── http_app.py           # Starlette: SSE + /webhook/sync + /healthz
│   ├── mcp_server.py         # tools, dual transport
│   └── cli.py                # local CLI (pkb build / sync / search / serve)
└── docs/
    ├── OBSIDIAN_SETUP.md     # vault prep guide
    ├── TEMPLATE.md           # note template
    └── AGENT_INTEGRATION.md  # MCP client configs, GitHub Action, key rotation
```
