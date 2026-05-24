---
name: personal-context7-pkb
description: "Use when operating Ahmed's personal Context7 PKB: adding books to the Obsidian vault, validating metadata, syncing Railway, checking Qdrant retrieval, running pkb evals, auditing Braintrust/Railway health, or deploying the personal-context7 service."
---

# Personal Context7 PKB

Operate the user's personal knowledge base end to end. The vault is the source of truth; Railway SQLite/Qdrant are derived indexes.

## Fixed Paths

- App repo: `/Volumes/SSD-2/Ahmed-KB/personal-context7`
- Vault: `/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki`
- Hosted URL: `https://personal-context7-production.up.railway.app`
- Railway app service: `personal-context7`
- Railway vector service: `qdrant`
- Vault GitHub repo: `amsayeed/obsidian-wiki-pkb`

## First Moves

1. Work from the app repo unless editing the vault.
2. Check both git worktrees before changes:

```bash
git status --short --branch
git -C "/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki" status --short --branch
```

3. Set local audit env when running `pkb` against the vault:

```bash
export PKB_KB_ROOT="/Users/amsayed/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki"
export PKB_DATA_DIR="/private/tmp/pkb-local-audit"
export PKB_DB_PATH="/private/tmp/pkb-local-audit/kb.db"
export PKB_CACHE_DIR="/Volumes/SSD-2/Ahmed-KB/personal-context7/data/.fastembed_cache"
export PKB_VECTOR_BACKEND=sqlite
```

Do not print API keys. Load `PKB_API_KEY` into a shell variable only when needed.

## Add Or Sync Books

For a new folder, run metadata creation only if the user asks for help filling missing front matter:

```bash
pkb ingest annotate "<book-folder>" --domain ai --source-type book --trust-tier 2 --collection "<Book Title>"
```

Always validate before sync:

```bash
pkb ingest check "<book-folder>" --require-summary-for-tier 2
pkb doctor --json
```

Commit and push the vault, then verify the GitHub Action or manually call sync:

```bash
PKB_API_KEY="$(railway variable list --service personal-context7 --kv | awk -F= '/^PKB_API_KEY=/{print $2}')"
curl -fsSL -X POST -H "Authorization: Bearer ${PKB_API_KEY}" \
  https://personal-context7-production.up.railway.app/webhook/sync
```

## Eval And Retrieval Checks

Run local smoke eval after metadata/index changes:

```bash
pkb build
pkb eval evals/pkb-smoke.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-smoke-report.json
pkb eval evals/wiki-eval-dataset.jsonl --k 10 --min-recall 0.8 --output /private/tmp/pkb-wiki-eval-report.json
```

For production verification, use the remote MCP SSE path and confirm:

- Tools are exactly `resolve_topic_json`, `get_docs_json`, `retrieve_json`, `multi_search_json`, `decision_evidence_json`.
- Retrieval hits include `vec` in `sources` for Qdrant-backed dense retrieval.
- `/stats` has `vector_backend=qdrant`, `qdrant_collection=pkb_chunks`, and the expected vault `git_sha`.

## Monitor And Audit

Use:

```bash
railway status
railway logs --service personal-context7 --lines 200
railway logs --service qdrant --lines 100
gh run list --repo amsayeed/obsidian-wiki-pkb --limit 5
```

Treat `anyio.ClosedResourceError` after completed SSE calls as disconnect noise. Investigate real tracebacks, non-2xx Qdrant responses, failed sync responses, or restarts.

## Deploy

Before deployment:

```bash
python -m compileall src
git status --short --branch
```

Commit, push, and deploy:

```bash
git add README.md docs evals skills src pyproject.toml Dockerfile railway.json
git commit -m "Update PKB operations"
git push origin main
railway up --detach --service personal-context7 -m "Update PKB operations"
```

Verify after deploy:

```bash
railway status
curl -fsSL https://personal-context7-production.up.railway.app/healthz
```

Read `docs/OPERATION_RUNBOOK.md` for command details, recovery, and the full audit checklist.
