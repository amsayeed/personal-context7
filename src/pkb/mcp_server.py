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
from threading import Thread

from mcp.server.fastmcp import FastMCP

from . import config as cfg_module
from . import doctor as doctor_module
from . import retriever, stats as stats_module, store
from .chunker import walk_kb
from .retriever import Filters
from .sync import bootstrap_kb, sync_now

logging.basicConfig(level=logging.INFO, format="[pkb] %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pkb")

cfg = cfg_module.load()
bootstrap_kb(cfg)  # idempotent: clones the KB repo on first boot

mcp = FastMCP("pkb")

_conn = store.connect(cfg.db_path)
store.init(_conn, cfg.embed_dim)

if cfg.transport == "sse":
    n_docs = _conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    if n_docs == 0 and cfg.kb_root.exists() and any(walk_kb(cfg.kb_root)):
        def initial_sync() -> None:
            log.info("empty index detected in hosted mode; running initial sync")
            result = sync_now(cfg)
            if result.ok:
                log.info("initial sync complete: %s", result.message)
            else:
                log.error("initial sync failed: %s", result.message)

        Thread(target=initial_sync, name="pkb-initial-sync", daemon=True).start()


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
        parts.append(
            f"_source: `{h.path}` • score: {h.score:.4f} • {meta} "
            f"• via: {','.join(h.sources)}_"
        )
        parts.append("")
        parts.append(h.text)
        parts.append("")
    return "\n".join(parts)


def _hits_json(hits) -> str:
    return json.dumps([retriever.hit_record(hit) for hit in hits], ensure_ascii=False, indent=2)


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
                **retriever.topic_record(t),
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
def resolve_topic_json(
    query: str, limit: int = 8,
    domain: str | None = None, source_type: str | None = None, min_tier: int | None = None,
) -> str:
    """Structured variant of resolve_topic for agents that should not parse markdown."""
    f = _filt(
        domains=[domain] if domain else None,
        source_types=[source_type] if source_type else None,
        min_tier=min_tier,
    )
    topics = retriever.resolve_topic(_conn, cfg, query, limit=limit, filt=f)
    return json.dumps(
        [retriever.topic_record(topic) for topic in topics],
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_docs_json(topic_id: str, query: str | None = None, tokens: int | None = None) -> str:
    """Structured variant of get_docs with explicit citation metadata per chunk."""
    hits = retriever.get_docs(_conn, cfg, topic_id, query, token_budget=tokens)
    return _hits_json(hits)


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
def search_json(
    query: str, tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """Structured one-shot hybrid search with explicit citation metadata."""
    hits = retriever.search(
        _conn, cfg, query,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _hits_json(hits)


@mcp.tool()
def smart_search(
    query: str, tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    Expanded search. Uses deterministic query variants internally, then fuses results.
    Prefer this when the agent is unsure whether plain search or multi_search fits.
    """
    hits = retriever.smart_search(
        _conn, cfg, query,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _render_hits(hits)


@mcp.tool()
def smart_search_json(
    query: str, tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """Structured expanded search with explicit citation metadata."""
    hits = retriever.smart_search(
        _conn, cfg, query,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _hits_json(hits)


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
    return json.dumps(stats_module.collect(_conn, cfg), indent=2)


@mcp.tool()
def stats_json() -> str:
    """Structured alias for stats."""
    return stats()


@mcp.tool()
def doctor_json(stale_days: int = 180) -> str:
    """Run KB quality checks: metadata, stale reviews, duplicate titles, chunks, wikilinks."""
    report = doctor_module.run_doctor(cfg, stale_days=stale_days)
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


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
