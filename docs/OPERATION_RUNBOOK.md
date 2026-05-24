# PKB Operation Runbook

This is the day-to-day runbook for the personal Context7 knowledge base.

## System Map

- Source of truth: Obsidian vault at `/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki`.
- Vault sync: private GitHub repo `amsayeed/obsidian-wiki-pkb`.
- Hosted app: Railway service `personal-context7`.
- Dense vector store: Railway `qdrant` service, collection `pkb_chunks`.
- Metadata/BM25 store: SQLite database on the Railway `/data` volume.
- Agent interface: remote MCP over SSE at `https://personal-context7-production.up.railway.app/sse`.
- Observability: Railway logs/metrics plus Braintrust sanitized traces.

The vault is authoritative. SQLite and Qdrant are derived indexes and can be rebuilt.

## Local Setup

Run these from the app repo:

```bash
cd /Volumes/SSD-2/Ahmed-KB/personal-context7
source .venv/bin/activate
export PKB_KB_ROOT="/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki"
```

Use a temporary local index when auditing so the repo data directory stays clean:

```bash
export PKB_DATA_DIR="/private/tmp/pkb-local-audit"
export PKB_DB_PATH="/private/tmp/pkb-local-audit/kb.db"
export PKB_CACHE_DIR="/Volumes/SSD-2/Ahmed-KB/personal-context7/data/.fastembed_cache"
export PKB_VECTOR_BACKEND=sqlite
```

Local evals use SQLite dense search. Production retrieval uses Qdrant because Qdrant is private inside Railway.

## Add A New Book Or Document Folder

1. Put the folder under the vault. Example:

```text
/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/Agents/My New Book/
```

2. Add or complete front matter. If the folder has no metadata yet, use `annotate` as a first pass:

```bash
pkb ingest annotate \
  "/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/Agents/My New Book" \
  --domain ai \
  --source-type book \
  --trust-tier 2 \
  --tag agents \
  --tag rag \
  --collection "My New Book"
```

3. Edit metadata manually in Obsidian. Required fields are:

```yaml
title: "..."
domain: "ai"
source_type: "book"
trust_tier: 2
```

Strongly recommended fields:

```yaml
summary: "..."
tags: ["agents", "rag"]
aliases: ["..."]
key_concepts: ["..."]
canonical_for: ["..."]
canonical_questions: ["..."]
freshness_status: "current"
last_reviewed: "2026-05-24"
```

4. Validate just that folder:

```bash
pkb ingest check \
  "/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/Agents/My New Book" \
  --require-summary-for-tier 2
```

5. Validate the whole vault:

```bash
pkb doctor --json
```

The sync gate blocks on errors. Warnings like duplicate titles or large chunks should be triaged, but they do not block ingestion.

## What The Ingest Commands Mean

- `pkb ingest annotate <folder>`: writes missing front matter into markdown files. It does not index anything. Use it to create editable metadata quickly.
- `pkb ingest check <folder>`: validates required metadata and vocabulary for one folder. It does not write or index.
- `pkb ingest index <folder>`: validates the folder, then indexes only that folder into the local index. It is useful for local smoke testing before push.
- `pkb sync`: incremental local index of the configured `PKB_KB_ROOT`.
- `pkb build`: full local rebuild of the configured `PKB_KB_ROOT`.

## Publish And Sync

Commit the vault, then push:

```bash
cd "/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki"
git status --short
git add -A
git commit -m "Add <book or corpus name>"
git push origin main
```

The vault GitHub Action calls `/webhook/sync` automatically on push.

Manual sync is still useful after a large import or when checking the endpoint directly. First load the API key into a shell variable without printing it:

```bash
cd /Volumes/SSD-2/Ahmed-KB/personal-context7
PKB_API_KEY="$(railway variable list --service personal-context7 --kv | awk -F= '/^PKB_API_KEY=/{print $2}')"
curl -fsSL -X POST \
  -H "Authorization: Bearer ${PKB_API_KEY}" \
  https://personal-context7-production.up.railway.app/webhook/sync
```

Expected success shape:

```json
{"ok":true,"pulled":true,"n_files":184,"n_chunks":5261,"message":"..."}
```

If it returns `index: up-to-date`, the Railway clone already has the latest Git SHA and no files needed reindexing.

## Monitor Production

Health:

```bash
curl -fsSL https://personal-context7-production.up.railway.app/healthz
```

Stats:

```bash
PKB_API_KEY="$(railway variable list --service personal-context7 --kv | awk -F= '/^PKB_API_KEY=/{print $2}')"
curl -fsSL \
  -H "Authorization: Bearer ${PKB_API_KEY}" \
  https://personal-context7-production.up.railway.app/stats
```

Important fields:

- `documents`: number of indexed markdown files.
- `chunks`: number of retrievable chunks.
- `git_sha`: vault commit SHA indexed on Railway.
- `vector_backend`: should be `qdrant`.
- `qdrant_collection`: should be `pkb_chunks`.
- `rerank_enabled`: should be `true`.
- `mcp_profile`: should be `agent` for normal agent clients.
- `braintrust_enabled`: should be `true` when tracing is configured.
- `braintrust_log_text`: should normally be `false`.

Railway status and logs:

```bash
railway status
railway logs --service personal-context7 --lines 200
railway logs --service qdrant --lines 100
```

Railway metrics when investigating latency or memory:

```bash
railway metrics --service personal-context7 --since 1h
railway metrics --service qdrant --since 1h
```

Log notes:

- Qdrant `points/delete` and `points?wait=true` with HTTP `200 OK` means indexing is writing vectors successfully.
- `anyio.ClosedResourceError` after MCP calls is usually SSE client disconnect noise if the tool call already returned.
- Real problems are tracebacks around indexing, Qdrant non-2xx responses, auth failures, or repeated process restarts.

## Eval Workflow

Smoke eval fixture:

```bash
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-smoke-report.json
```

Broader baseline fixture:

```bash
pkb eval evals/wiki-eval-dataset.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-wiki-eval-report.json
```

Use the smoke fixture as the deploy gate. Use the broader fixture to find tuning
work as the corpus grows; it intentionally covers more ambiguous questions.

Strict local gate after meaningful retrieval changes:

```bash
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 1.0 --min-mrr 0.5 --output /private/tmp/pkb-smoke-report.json
```

Braintrust logging is enabled by default when configured:

```bash
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 0.8
```

Disable Braintrust for local-only checks:

```bash
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 0.8 --no-braintrust
```

Add eval rows whenever you add an important book:

```jsonl
{"question":"What should the agent retrieve?","expected_sources":["Folder/Book/Chapter.md"],"expected_terms":["term one","term two"]}
```

A good fixture has:

- The exact source path expected.
- A question a real agent would ask.
- Two or three terms that should appear in the retrieved evidence.
- Coverage across domains, not ten questions from one book.

## Remote MCP Smoke Test

Use this when you need to verify the production agent path, including Qdrant:

```bash
python - <<'PY'
import asyncio
import json
import subprocess
from mcp import ClientSession
from mcp.client.sse import sse_client

url = "https://personal-context7-production.up.railway.app/sse"
out = subprocess.check_output(
    ["railway", "variable", "list", "--service", "personal-context7", "--kv"],
    text=True,
)
key = next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("PKB_API_KEY="))

async def main():
    async with sse_client(url, headers={"Authorization": "Bearer " + key}, timeout=30) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            print("tools:", tools)
            res = await session.call_tool(
                "retrieve_json",
                {"query": "data contracts schema evolution architecture", "tokens": 1800},
            )
            hits = json.loads("".join(getattr(c, "text", "") for c in res.content))
            for hit in hits[:3]:
                print(hit["title"], "|", hit["source"], "|", ",".join(hit["sources"]))

asyncio.run(main())
PY
```

Expected tools for production:

```text
resolve_topic_json
get_docs_json
retrieve_json
multi_search_json
decision_evidence_json
```

## Deploy App Changes

Use this for code or docs changes in the app repo:

```bash
cd /Volumes/SSD-2/Ahmed-KB/personal-context7
git status --short
python -m compileall src
git add README.md docs evals skills src pyproject.toml Dockerfile railway.json
git commit -m "Update PKB operations"
git push origin main
railway up --detach --service personal-context7 -m "Update PKB operations"
```

`railway up --detach` starts the deployment but does not wait for health. Verify after deploying:

```bash
railway status
railway logs --service personal-context7 --lines 120
curl -fsSL https://personal-context7-production.up.railway.app/healthz
```

Then check stats and one MCP retrieval call.

## Full Audit Checklist

Run this before declaring the system ready:

```bash
cd /Volumes/SSD-2/Ahmed-KB/personal-context7
git status --short --branch
python -m compileall src
```

```bash
export PKB_KB_ROOT="/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki"
export PKB_DATA_DIR="/private/tmp/pkb-local-audit"
export PKB_DB_PATH="/private/tmp/pkb-local-audit/kb.db"
export PKB_CACHE_DIR="/Volumes/SSD-2/Ahmed-KB/personal-context7/data/.fastembed_cache"
export PKB_VECTOR_BACKEND=sqlite
pkb doctor --json
pkb build
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-smoke-report.json
pkb eval evals/wiki-eval-dataset.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-wiki-eval-report.json
```

Production checks:

```bash
railway status
gh run list --repo amsayeed/obsidian-wiki-pkb --limit 5
```

Then run the `/stats` and remote MCP smoke commands above.

## Recovery

If metadata fails:

```bash
pkb doctor --json
pkb ingest check "<folder>" --require-summary-for-tier 2
```

Fix the markdown front matter, then commit and push the vault again.

If sync fails:

```bash
railway logs --service personal-context7 --lines 300
```

Look for the first real exception above the HTTP 500. Common causes are bad metadata, a Git auth failure, or Qdrant connectivity.

If Qdrant looks stale but SQLite has the right chunks:

```bash
PKB_API_KEY="$(railway variable list --service personal-context7 --kv | awk -F= '/^PKB_API_KEY=/{print $2}')"
curl -fsSL -X POST \
  -H "Authorization: Bearer ${PKB_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"recreate": true, "batch_size": 256}' \
  https://personal-context7-production.up.railway.app/webhook/qdrant-backfill
```

If the app deployment is unhealthy:

```bash
railway status
railway logs --service personal-context7 --lines 300
railway variable list --service personal-context7
```

Check that required variables still exist: `PKB_API_KEY`, `PKB_KB_GIT_REMOTE`, `PKB_TRANSPORT=sse`, `PKB_VECTOR_BACKEND=qdrant`, `PKB_QDRANT_URL`, `PKB_QDRANT_API_KEY`, and `PKB_REQUIRE_METADATA=true`.
