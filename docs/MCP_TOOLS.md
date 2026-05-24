# MCP Tool Profiles

The hosted MCP server should not expose every debug/admin tool to ordinary agents.
Use profiles to control the tool surface.

## Agent Profile

Default:

```bash
PKB_MCP_PROFILE=agent
```

Exposes:

```text
retrieve_json
get_docs_json
resolve_topic_json
multi_search_json
decision_evidence_json
```

Use this profile for Claude Code, Codex, Factory Droid, Grok, Cursor, and other
normal model clients.

### `retrieve_json`

Default retrieval. Uses query expansion, BM25, dense vector retrieval, RRF fusion,
metadata boosts, and reranking.

Use for:

- direct questions
- concept lookup
- finding relevant passages across the KB

### `get_docs_json`

Fetch more ranked chunks from one selected source path.

Use after `resolve_topic_json` or `retrieve_json` when the model needs more context
from the same book/chapter.

### `resolve_topic_json`

Find candidate documents. This mirrors Context7's resolve step.

Use for:

- "which book/chapter covers this?"
- narrowing broad topics before calling `get_docs_json`

### `multi_search_json`

Use for comparative or compound questions. The calling model should decompose the
question into 2-5 subqueries.

Example:

```json
{
  "queries": [
    "event sourcing auditability replay benefits",
    "event sourcing operational complexity failure modes",
    "CRUD administrative workflow tradeoffs"
  ]
}
```

### `decision_evidence_json`

Use for architecture decisions. It retrieves evidence and returns a decision protocol.
The model must still synthesize the final recommendation with citations.

Use for:

- "Should I use X or Y?"
- "What architecture should I choose?"
- "Compare these patterns and make a recommendation."

## Admin Profile

```bash
PKB_MCP_PROFILE=admin
```

Exposes:

```text
sync
stats
stats_json
doctor_json
```

Use this only for maintenance clients or one-off local debugging.

## Legacy And Full Profiles

```bash
PKB_MCP_PROFILE=legacy
PKB_MCP_PROFILE=full
```

`legacy` exposes older markdown-oriented tool names such as `search`, `smart_search`,
and `hyde_search`.

`full` exposes every tool. Use it only while debugging the server because too many
overlapping tools makes agents worse at choosing the right action.
