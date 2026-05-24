"""
MCP server.

Two transports, same tool set:
    stdio  — local mode, plugs into any MCP client on the same machine.
    sse    — hosted mode, served as HTTP under bearer-token auth.

Selected via PKB_TRANSPORT env (default stdio). On Railway, set sse.

Tool set:
    retrieve_json(query, ...)             — default structured retrieval for agents.
    resolve_topic_json(query, ...)        — find candidate documents.
    get_docs_json(topic_id, ...)          — pull ranked chunks from one document.
    multi_search_json(queries[], ...)     — fan-out to N queries, fuse, rerank.
    decision_evidence_json(question, ...) — evidence package for architecture decisions.
    admin profile: sync(), stats_json(), doctor_json().

All search tools accept the same filters: tags, source_types, domains, folders, min_tier.
"""

from __future__ import annotations

import json
import logging
import os
from threading import Thread
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

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


def _split_env_list(key: str) -> list[str]:
    value = os.environ.get(key, "")
    return [part.strip() for part in value.split(",") if part.strip()]


def _host_from_url_or_host(value: str) -> str | None:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return parsed.netloc or parsed.path or None


def _origin_from_url(value: str) -> str | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _transport_security() -> TransportSecuritySettings | None:
    if cfg.transport != "sse":
        return None

    hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    ]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]

    for key in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PRIVATE_DOMAIN"):
        value = os.environ.get(key)
        if value:
            host = _host_from_url_or_host(value)
            if host:
                hosts.append(host)

    for key in ("RAILWAY_STATIC_URL", "RAILWAY_SERVICE_PERSONAL_CONTEXT7_URL", "PKB_PUBLIC_URL"):
        value = os.environ.get(key)
        if value:
            host = _host_from_url_or_host(value)
            origin = _origin_from_url(value)
            if host:
                hosts.append(host)
            if origin:
                origins.append(origin)

    hosts.extend(_split_env_list("PKB_ALLOWED_HOSTS"))
    origins.extend(_split_env_list("PKB_ALLOWED_ORIGINS"))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=sorted(set(origins)),
    )


mcp = FastMCP("pkb", host=cfg.host, port=cfg.port, transport_security=_transport_security())


def _profile_enabled(*profiles: str) -> bool:
    """Control MCP tool exposure without changing retrieval code."""
    profile = cfg.mcp_profile
    if profile == "full":
        return True
    return profile in set(profiles)


def pkb_tool(*profiles: str):
    """Register a function as an MCP tool only for selected profiles."""
    def decorator(fn):
        if _profile_enabled(*profiles):
            return mcp.tool()(fn)
        return fn

    return decorator

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

@pkb_tool("legacy")
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


@pkb_tool("legacy")
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


@pkb_tool("agent", "legacy")
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


@pkb_tool("agent", "legacy")
def get_docs_json(topic_id: str, query: str | None = None, tokens: int | None = None) -> str:
    """Structured variant of get_docs with explicit citation metadata per chunk."""
    hits = retriever.get_docs(_conn, cfg, topic_id, query, token_budget=tokens)
    return _hits_json(hits)


@pkb_tool("agent")
def retrieve_json(
    query: str,
    tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    Default retrieval tool for agents. Uses expanded hybrid retrieval
    (BM25 + vector + RRF + rerank) and returns structured citation records.
    """
    hits = retriever.smart_search(
        _conn,
        cfg,
        query,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _hits_json(hits)


@pkb_tool("legacy")
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


@pkb_tool("legacy")
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


@pkb_tool("legacy")
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


@pkb_tool("legacy")
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


@pkb_tool("legacy")
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


@pkb_tool("agent")
def multi_search_json(
    queries: list[str],
    tokens: int | None = None,
    tags: list[str] | None = None,
    source_types: list[str] | None = None,
    domains: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = None,
) -> str:
    """
    Structured multi-query retrieval. Use for comparative or compound questions
    after decomposing the user request into 2-5 focused sub-queries.
    """
    hits = retriever.multi_search(
        _conn,
        cfg,
        queries,
        filt=_filt(tags=tags, source_types=source_types, domains=domains,
                   folders=folders, min_tier=min_tier),
        token_budget=tokens,
    )
    return _hits_json(hits)


@pkb_tool("agent")
def decision_evidence_json(
    question: str,
    options: list[str] | None = None,
    tokens: int | None = None,
    domains: list[str] | None = None,
    source_types: list[str] | None = None,
    folders: list[str] | None = None,
    min_tier: int | None = 1,
) -> str:
    """
    Retrieve evidence for architecture decisions. Returns candidate evidence plus
    a decision protocol for the calling model: compare claims, tradeoffs,
    conflicts, assumptions, and cite sources before choosing.
    """
    queries = [question]
    if options:
        queries.extend(
            f"{question} {option} tradeoffs failure modes when to use"
            for option in options
        )
    hits = (
        retriever.multi_search(
            _conn,
            cfg,
            queries,
            filt=_filt(source_types=source_types, domains=domains,
                       folders=folders, min_tier=min_tier),
            token_budget=tokens,
        )
        if len(queries) > 1
        else retriever.smart_search(
            _conn,
            cfg,
            question,
            filt=_filt(source_types=source_types, domains=domains,
                       folders=folders, min_tier=min_tier),
            token_budget=tokens,
        )
    )
    records = [retriever.hit_record(hit) for hit in hits]
    sources = []
    seen = set()
    for hit in hits:
        if hit.path in seen:
            continue
        seen.add(hit.path)
        sources.append(
            {
                "path": hit.path,
                "title": hit.title,
                "source_type": hit.source_type,
                "domain": hit.domain,
                "trust_tier": hit.trust_tier,
                "freshness_status": hit.freshness_status,
            }
        )
    return json.dumps(
        {
            "question": question,
            "options": options or [],
            "queries": queries,
            "decision_protocol": [
                "Extract concrete claims and tradeoffs from the evidence.",
                "Group agreement and conflict across sources.",
                "Prefer higher trust_tier, newer edition/current status, and directly relevant chapters.",
                "State assumptions and missing evidence before the recommendation.",
                "Return the final decision with citations to source paths/headings.",
            ],
            "sources": sources,
            "evidence": records,
        },
        ensure_ascii=False,
        indent=2,
    )


@pkb_tool("legacy")
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


@pkb_tool("admin")
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


@pkb_tool("admin", "legacy")
def stats() -> str:
    """Index health: doc/chunk counts, by-tier breakdown, model, config."""
    return json.dumps(stats_module.collect(_conn, cfg), indent=2)


@pkb_tool("admin")
def stats_json() -> str:
    """Structured alias for stats."""
    return stats()


@pkb_tool("admin")
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
