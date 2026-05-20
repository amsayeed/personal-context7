"""
MCP server.

Two transports, same tool set:
    stdio  — local mode, plugs into any MCP client on the same machine.
    sse    — hosted mode, served as HTTP under bearer-token auth.

Selected via PKB_TRANSPORT env (default stdio). On Railway, set sse.

Tool set:
    resolve_topic(query, ...)             — find candidate documents.
    get_docs(topic_id, query?, tokens?)   — pull ranked chunks from one document.
    search(query, ...)                    — single hybrid search across the KB.
    multi_search(queries[], ...)          — fan-out to N queries, fuse, rerank.
    hyde_search(query, hypothesis, ...)   — agent supplies a hypothetical doc; we embed it.
    sync()                                — git pull + incremental reindex.
    stats()                               — index health.

All search tools accept the same filters: tags, source_types, domains, folders, min_tier.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config as cfg_module
from . import retriever, store
from .retriever import Filters
from .sync import bootstrap_kb, sync_now

logging.basicConfig(level=logging.INFO, format="[pkb] %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pkb")

cfg = cfg_module.load()
bootstrap_kb(cfg)  # idempotent: clones the KB repo on first boot

mcp = FastMCP("pkb")

_conn = store.connect(cfg.db_path)
store.init(_conn, cfg.embed_dim)


def _filt(
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> Filters:
    return Filters(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier)


def _render_hits(hits) -> str:
    if not hits:
        return "_no matches_"
    parts: list[str] = []
    for i, h in enumerate(hits, 1):
        meta = f"tier={h.trust_tier} • {h.source_type} • {h.domain}"
        parts.append(f"### [{i}] {h.title} — {h.heading_path}")
        parts.append(f"_source: `{h.path}` • score: {h.score:.4f} • {meta} • via: {','.join(h.sources)}_")
        parts.append("")
        parts.append(h.text)
        parts.append("")
    return "\n".join(parts)


# ---------- tools ----------

@mcp.tool()
def resolve_topic(
    query: str, limit: int = 8,
    domain: str | None = None, source_type: str | None = None, min_tier: int | None = None,
) -> str:
    """
    Find candidate documents matching a query. Returns JSON of
    [{topic_id, title, tags, source_type, domain, trust_tier, snippet}].

    Pass `topic_id` back to `get_docs`. Filters: domain, source_type, min_tier.
    """
    f = _filt(
        domains=[domain] if domain else None,
        source_types=[source_type] if source_type else None,
        min_tier=min_tier,
    )
    topics = retriever.resolve_topic(_conn, cfg, query, limit=limit, filt=f)
    return json.dumps(
        [
            {
                "topic_id": t.topic_id, "title": t.title, "tags": t.tags,
                "source_type": t.source_type, "domain": t.domain,
                "trust_tier": t.trust_tier, "snippet": t.snippet,
            }
            for t in topics
        ],
        ensure_ascii=False, indent=2,
    )


@mcp.tool()
def get_docs(topic_id: str, query: str | None = None, tokens: int | None = None) -> str:
    """
    Fetch ranked chunks from a chosen document (topic_id == relative path).

    - topic_id : value returned by `resolve_topic`.
    - query    : optional refinement; chunks are ranked against it.
    - tokens   : token budget (default PKB_TOKEN_BUDGET).
    """
    hits = retriever.get_docs(_conn, cfg, topic_id, query, token_budget=tokens)
    if not hits:
        return f"_No content found for topic `{topic_id}`._"
    parts: list[str] = [f"# {hits[0].title}", f"_source: `{topic_id}`_", ""]
    for h in hits:
        parts.append(f"## {h.heading_path}")
        parts.append(h.text)
        parts.append("")
    return "\n".join(parts)


@mcp.tool()
def search(
    query: str, tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    One-shot hybrid search (BM25 + dense, RRF-fused, tier-boosted, optionally reranked).

    Filters are AND-combined and applied before fusion:
      tags         — front-matter or inline `#tags` (case-insensitive).
      source_types — e.g. ['book','own-note'].
      domains      — e.g. ['system-design','data'].
      folders      — top-level folder names.
      min_tier     — keep documents at trust_tier >= N. 2 = canonical only.
    """
    hits = retriever.search(
        _conn, cfg, query,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _render_hits(hits)


@mcp.tool()
def multi_search(
    queries: list[str], tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    Multi-hop retrieval. Run several sub-queries in parallel and fuse across them.

    Use when the user's question is comparative ("X vs Y") or compound
    ("how does A interact with B under C"). Decompose into 2-5 sub-queries
    and pass them here.
    """
    hits = retriever.multi_search(
        _conn, cfg, queries,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _render_hits(hits)


@mcp.tool()
def hyde_search(
    query: str, hypothesis: str, tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    HyDE search. You (the agent) draft a plausible answer first (`hypothesis`),
    then we embed it for the dense leg of retrieval. Big recall lift when the
    user's question phrases things differently than the notes do.

    Tip: keep the hypothesis short (≤300 tokens) and concrete.
    """
    hits = retriever.hyde_search(
        _conn, cfg, query, hypothesis,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _render_hits(hits)


@mcp.tool()
def sync() -> str:
    """
    Trigger a git pull (if PKB_KB_GIT_REMOTE is configured) and incrementally
    re-index any changed files. Returns a JSON summary.
    """
    r = sync_now(cfg)
    return json.dumps({
        "ok": r.ok, "pulled": r.pulled,
        "n_files": r.n_files, "n_chunks": r.n_chunks,
        "message": r.message,
    }, indent=2)


@mcp.tool()
def stats() -> str:
    """Index health: doc/chunk counts, by-tier breakdown, model, config."""
    n_docs = _conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    n_chunks = _conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    by_tier = {
        r["trust_tier"]: r["n"]
        for r in _conn.execute(
            "SELECT trust_tier, COUNT(*) AS n FROM documents GROUP BY trust_tier"
        )
    }
    by_source = {
        r["source_type"]: r["n"]
        for r in _conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM documents GROUP BY source_type"
        )
    }
    return json.dumps({
        "kb_root": str(cfg.kb_root),
        "db_path": str(cfg.db_path),
        "documents": n_docs,
        "chunks": n_chunks,
        "by_tier": by_tier,
        "by_source_type": by_source,
        "embed_model": cfg.embed_model,
        "rerank_enabled": cfg.rerank_enabled,
        "transport": cfg.transport,
    }, indent=2)


# ---------- entrypoint ----------

def main() -> None:
    if cfg.transport == "sse":
        import uvicorn
        from .http_app import build_app
        log.info("starting MCP SSE server on %s:%d (db=%s, kb=%s)",
                 cfg.host, cfg.port, cfg.db_path, cfg.kb_root)
        app = build_app(mcp.sse_app())
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", access_log=False)
    else:
        log.info("starting MCP stdio server (db=%s, kb=%s)", cfg.db_path, cfg.kb_root)
        mcp.run()


if __name__ == "__main__":
    main()
