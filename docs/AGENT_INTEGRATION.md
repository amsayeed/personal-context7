# Wiring `pkb` into your agents

You've deployed to Railway. You have:

- A public URL like `https://pkb-production.up.railway.app`
- An API key (the `PKB_API_KEY` `deploy.sh` printed)

Below: how to plug it into the agents you actually use, plus the GitHub Action that keeps the index fresh.

## The endpoints

| Path             | Method | Auth | Purpose                                                                |
| ---------------- | ------ | ---- | ---------------------------------------------------------------------- |
| `/healthz`       | GET    | none | Liveness check.                                                        |
| `/sse`           | —      | bearer | MCP Server-Sent-Events transport. Where agents connect.              |
| `/messages`      | POST   | bearer | MCP client→server messages. Mounted alongside `/sse`.                |
| `/webhook/sync`  | POST   | bearer | Trigger `git pull` + incremental reindex.                              |
| `/stats`         | GET    | bearer | JSON: doc/chunk counts, tier breakdown.                                |

## MCP clients

### Claude Code

Add to `~/.config/claude-code/mcp_servers.json` (or run `claude mcp add` interactively):

```json
{
  "mcpServers": {
    "pkb": {
      "type": "sse",
      "url": "https://pkb-production.up.railway.app/sse",
      "headers": {
        "Authorization": "Bearer YOUR_PKB_API_KEY"
      }
    }
  }
}
```

Restart Claude Code. You should see `pkb` listed under MCP tools. Try:

```
> using pkb, find my notes on eventual consistency
```

### Cowork (the Claude desktop tool you're using right now)

Settings → Extensions → MCP servers → add a custom SSE server with the URL and bearer header.

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "pkb": {
      "url": "https://pkb-production.up.railway.app/sse",
      "headers": { "Authorization": "Bearer YOUR_PKB_API_KEY" }
    }
  }
}
```

### Continue / Windsurf / others

All recent MCP-supporting clients use the same shape: an SSE URL plus a `headers` block for bearer. If a client only supports stdio MCP, run a local proxy — see "stdio bridge" below.

## A useful system-prompt nudge

The default tool descriptions are decent, but adding a one-liner to the agent's system prompt sharpens behavior. Try something like:

> When the user asks about architecture, data, AI, or system-design patterns,
> prefer `pkb.smart_search_json` over guessing. For comparative questions ("X vs Y"),
> decompose into 2–4 sub-queries and call `pkb.multi_search`. For questions
> phrased very differently than how the notes likely read, draft a 2-sentence
> hypothetical answer first and call `pkb.hyde_search(query, hypothesis)`.
> Cite the returned `source:` paths in your answer.

That nudge alone is the difference between an agent that quotes your KB and one that forgets it exists.

## Tool reference

What's exposed via MCP:

| Tool                                          | When to call                                                     |
| --------------------------------------------- | ---------------------------------------------------------------- |
| `resolve_topic(query, limit, filters...)`     | "Which document covers X?" — returns candidate doc paths.        |
| `get_docs(topic_id, query?, tokens?)`         | After resolve_topic, pull ranked chunks from one doc.            |
| `search(query, ..., min_tier=2)`              | One-shot hybrid search. The workhorse.                           |
| `smart_search(query, ..., min_tier=2)`        | Expanded search with deterministic query variants.               |
| `multi_search(queries=[...])`                 | Comparative or compound questions. Pass 2–5 sub-queries.         |
| `hyde_search(query, hypothesis)`              | Recall lift when the user's phrasing diverges from the notes.    |
| `resolve_topic_json`, `get_docs_json`         | Structured citation metadata; preferred for agent pipelines.     |
| `search_json`, `smart_search_json`            | Structured search payloads with text and metadata.               |
| `doctor_json()`                               | KB hygiene report: metadata, stale reviews, chunks, wikilinks.   |
| `sync()`                                      | Pull from git + re-index changed files.                          |
| `stats()` / `stats_json()`                    | Sanity check with freshness, git SHA, DB size, breakdowns.       |

Filters available on every search tool: `tags`, `source_types`, `domains`, `folders`, `min_tier`.

## Keeping the index fresh

### Option 1 — GitHub Action (recommended)

Add `.github/workflows/sync-pkb.yml` to your **notes** repo (not this one):

```yaml
name: sync pkb
on:
  push:
    branches: [main]
  workflow_dispatch: {}

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger pkb sync
        run: |
          curl -fsSL -X POST \
            -H "Authorization: Bearer ${{ secrets.PKB_API_KEY }}" \
            "${{ secrets.PKB_URL }}/webhook/sync"
```

Add two repo secrets:

- `PKB_API_KEY` — your bearer token.
- `PKB_URL` — `https://pkb-production.up.railway.app`

Now every `git push` to your notes repo triggers an incremental reindex.

### Option 2 — Cron from your laptop

```cron
*/15 * * * * curl -fsSL -X POST -H "Authorization: Bearer $PKB_API_KEY" https://pkb-production.up.railway.app/webhook/sync >/dev/null
```

Idempotent, cheap, fine.

### Option 3 — Manual

```bash
curl -X POST -H "Authorization: Bearer $PKB_API_KEY" https://pkb-production.up.railway.app/webhook/sync
```

## stdio bridge (for MCP clients without SSE)

If you have a client that only supports stdio MCP — uncommon now but possible — run a one-line bridge locally:

```bash
pip install mcp-proxy
mcp-proxy --sse-url https://pkb-production.up.railway.app/sse \
          --headers "Authorization=Bearer $PKB_API_KEY"
```

Point the stdio client at the `mcp-proxy` command. The proxy translates stdio ↔ SSE transparently.

## Sanity checks

```bash
# liveness
curl -s https://pkb-production.up.railway.app/healthz

# index state (requires bearer)
curl -sH "Authorization: Bearer $PKB_API_KEY" \
     https://pkb-production.up.railway.app/stats | jq

# force a sync
curl -X POST -H "Authorization: Bearer $PKB_API_KEY" \
     https://pkb-production.up.railway.app/webhook/sync | jq
```

If `/stats` shows `documents: 0`, your KB hasn't been pulled yet — check `PKB_KB_GIT_REMOTE` and `railway logs --service pkb`.

## Rotating the API key

```bash
NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
railway variables --set "PKB_API_KEY=$NEW_KEY" --service pkb
railway redeploy --service pkb
# then update each MCP client config + GitHub Actions secret
```

Bearer auth is constant-time compared in the server, so rotation is the only real defense against an exfiltrated key.
