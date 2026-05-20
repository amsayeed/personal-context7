"""
Hybrid retrieval: BM25 + dense vectors, fused with Reciprocal Rank Fusion (RRF),
then optional metadata soft-boost, then optional cross-encoder rerank.

Pipeline:
    query → [BM25 50] + [vec 50]  →  RRF (k=60)  →  tier boost  →  rerank top-N  →  budget pack

Why RRF: score-free. Each retriever's score distribution is different (FTS5 bm25
vs L2 distance), and weighted-sum fusion needs per-corpus calibration. RRF only
uses *ranks*, generalizes well, and matches or beats tuned schemes on BEIR.

Multi-hop:
    multi_search(queries=[...])  → runs each query, RRF-merges across queries.
    hyde_search(query, hypothesis) → embeds the hypothesis instead of the query.
                                     The agent generates the hypothesis itself.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from .config import Config
from .embed import embed_query
from .store import bm25_search, vec_search


@dataclass
class Hit:
    chunk_id: str
    path: str
    title: str
    heading_path: str
    text: str
    n_tokens: int
    source_type: str
    domain: str
    trust_tier: int
    folder: str
    score: float
    sources: list[str] = field(default_factory=list)


@dataclass
class Filters:
    tags: list[str] | None = None
    source_types: list[str] | None = None
    domains: list[str] | None = None
    folders: list[str] | None = None
    min_tier: int | None = None


def _row_to_hit(r) -> Hit:
    return Hit(
        chunk_id=r["chunk_id"],
        path=r["path"],
        title=r["title"],
        heading_path=r["heading_path"],
        text=r["text"],
        n_tokens=r["n_tokens"],
        source_type=r["source_type"] or "unknown",
        domain=r["domain"] or "unknown",
        trust_tier=int(r["trust_tier"] or 1),
        folder=r["folder"] or "",
        score=0.0,
        sources=[],
    )


def _rrf_fuse(*ranked_lists: list, k: int = 60) -> list[Hit]:
    """
    Generalized RRF: takes N ranked lists of either sqlite3.Row or Hit objects.
    Returns deduped Hits sorted by fused score desc. The first list's rows are
    used to populate hit metadata if the chunk hasn't been seen.
    """
    pool: dict[str, Hit] = {}

    for rows in ranked_lists:
        for rank, row in enumerate(rows, start=1):
            cid = row["chunk_id"] if not isinstance(row, Hit) else row.chunk_id
            if cid in pool:
                h = pool[cid]
            else:
                h = _row_to_hit(row) if not isinstance(row, Hit) else row
                h.score = 0.0
                h.sources = list(h.sources) if isinstance(row, Hit) else []
                pool[cid] = h
            h.score += 1.0 / (k + rank)

    return sorted(pool.values(), key=lambda h: h.score, reverse=True)


def _apply_tier_boost(hits: list[Hit], cfg: Config) -> list[Hit]:
    boosts = cfg.tier_boost
    for h in hits:
        h.score *= boosts.get(h.trust_tier, 1.0)
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _maybe_rerank(query: str, hits: list[Hit], cfg: Config) -> list[Hit]:
    if not cfg.rerank_enabled or not hits:
        return hits
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except Exception:
        return hits  # fail open: rerank lib not present
    ce = TextCrossEncoder(model_name=cfg.rerank_model, cache_dir=str(cfg.cache_dir))
    scores = list(ce.rerank(query, [h.text for h in hits]))
    for h, s in zip(hits, scores):
        h.score = float(s)
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _budget(hits: list[Hit], token_budget: int) -> list[Hit]:
    out, spent = [], 0
    for h in hits:
        if spent + h.n_tokens > token_budget and out:
            break
        out.append(h)
        spent += h.n_tokens
    return out


def _both_searches(
    conn: sqlite3.Connection, cfg: Config, query: str, filt: Filters
) -> tuple[list, list]:
    """Run BM25 and vector search in parallel; return raw rows."""
    fkw = dict(
        tags=filt.tags, source_types=filt.source_types, domains=filt.domains,
        folders=filt.folders, min_tier=filt.min_tier,
    )
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bm = ex.submit(bm25_search, conn, query, cfg.bm25_topk, **fkw)
        f_vc = ex.submit(
            lambda: vec_search(
                conn,
                embed_query(query, model=cfg.embed_model, cache_dir=cfg.cache_dir),
                cfg.vec_topk, **fkw,
            )
        )
        return f_bm.result(), f_vc.result()


# ---------- public retrieval entry points ----------

def search(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    *,
    filt: Filters | None = None,
    token_budget: int | None = None,
) -> list[Hit]:
    filt = filt or Filters()
    budget = token_budget or cfg.token_budget_default

    bm25_rows, vec_rows = _both_searches(conn, cfg, query, filt)
    fused = _rrf_fuse(bm25_rows, vec_rows, k=cfg.rrf_k)

    # Mark sources for transparency.
    bm25_ids = {r["chunk_id"] for r in bm25_rows}
    vec_ids = {r["chunk_id"] for r in vec_rows}
    for h in fused:
        if h.chunk_id in bm25_ids:
            h.sources.append("bm25")
        if h.chunk_id in vec_ids:
            h.sources.append("vec")

    boosted = _apply_tier_boost(fused, cfg)
    top = boosted[: max(cfg.final_topk * 3, 30)]
    reranked = _maybe_rerank(query, top, cfg)
    return _budget(reranked[: cfg.final_topk], budget)


def multi_search(
    conn: sqlite3.Connection,
    cfg: Config,
    queries: list[str],
    *,
    filt: Filters | None = None,
    token_budget: int | None = None,
) -> list[Hit]:
    """
    Multi-hop retrieval: run each query independently, fuse with RRF across queries.

    Use this when the agent breaks a question into sub-questions, or when the user
    asks something comparative ("compare X vs Y vs Z").
    """
    filt = filt or Filters()
    budget = token_budget or cfg.token_budget_default

    per_query_results: list[list[Hit]] = []
    for q in queries:
        bm, vc = _both_searches(conn, cfg, q, filt)
        fused = _rrf_fuse(bm, vc, k=cfg.rrf_k)
        bm_ids = {r["chunk_id"] for r in bm}
        vc_ids = {r["chunk_id"] for r in vc}
        for h in fused:
            if h.chunk_id in bm_ids:
                h.sources.append("bm25")
            if h.chunk_id in vc_ids:
                h.sources.append("vec")
        per_query_results.append(fused[: max(cfg.final_topk * 2, 24)])

    # RRF across queries — same algorithm, different inputs.
    fused = _rrf_fuse(*per_query_results, k=cfg.rrf_k)
    boosted = _apply_tier_boost(fused, cfg)
    # Rerank against the *joined* query — best heuristic when sub-queries are
    # different facets of one question.
    joined = " ; ".join(queries)
    top = boosted[: max(cfg.final_topk * 3, 30)]
    reranked = _maybe_rerank(joined, top, cfg)
    return _budget(reranked[: cfg.final_topk], budget)


def hyde_search(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    hypothesis: str,
    *,
    filt: Filters | None = None,
    token_budget: int | None = None,
) -> list[Hit]:
    """
    HyDE (Hypothetical Document Embeddings).

    The agent first drafts a plausible answer to `query` (the `hypothesis`). We embed
    that hypothesis and use it for the dense leg; BM25 still uses the original query.
    Empirically this lifts recall on questions phrased differently than the corpus.
    """
    filt = filt or Filters()
    budget = token_budget or cfg.token_budget_default
    fkw = dict(
        tags=filt.tags, source_types=filt.source_types, domains=filt.domains,
        folders=filt.folders, min_tier=filt.min_tier,
    )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bm = ex.submit(bm25_search, conn, query, cfg.bm25_topk, **fkw)
        f_vc = ex.submit(
            lambda: vec_search(
                conn,
                embed_query(hypothesis, model=cfg.embed_model, cache_dir=cfg.cache_dir),
                cfg.vec_topk, **fkw,
            )
        )
        bm_rows, vc_rows = f_bm.result(), f_vc.result()

    fused = _rrf_fuse(bm_rows, vc_rows, k=cfg.rrf_k)
    bm_ids = {r["chunk_id"] for r in bm_rows}
    vc_ids = {r["chunk_id"] for r in vc_rows}
    for h in fused:
        if h.chunk_id in bm_ids:
            h.sources.append("bm25")
        if h.chunk_id in vc_ids:
            h.sources.append("vec(hyde)")

    boosted = _apply_tier_boost(fused, cfg)
    top = boosted[: max(cfg.final_topk * 3, 30)]
    reranked = _maybe_rerank(query, top, cfg)  # rerank uses the original query
    return _budget(reranked[: cfg.final_topk], budget)


# ---------- topic-style helpers (Context7 UX) ----------

@dataclass
class Topic:
    topic_id: str
    title: str
    tags: list[str]
    source_type: str
    domain: str
    trust_tier: int
    snippet: str


def resolve_topic(
    conn: sqlite3.Connection, cfg: Config, query: str,
    limit: int = 8, filt: Filters | None = None,
) -> list[Topic]:
    """Mirrors Context7's resolve-library-id: returns candidate *documents*."""
    import json
    hits = search(conn, cfg, query, filt=filt, token_budget=8192)
    seen: dict[str, Topic] = {}
    for h in hits:
        if h.path in seen:
            continue
        row = conn.execute(
            "SELECT tags_json, source_type, domain, trust_tier FROM documents WHERE path = ?",
            (h.path,),
        ).fetchone()
        tags = json.loads(row["tags_json"]) if row and row["tags_json"] else []
        seen[h.path] = Topic(
            topic_id=h.path, title=h.title, tags=tags,
            source_type=row["source_type"] or "unknown",
            domain=row["domain"] or "unknown",
            trust_tier=int(row["trust_tier"] or 1),
            snippet=h.heading_path,
        )
        if len(seen) >= limit:
            break
    return list(seen.values())


def get_docs(
    conn: sqlite3.Connection, cfg: Config, topic_id: str,
    query: str | None, token_budget: int | None = None,
) -> list[Hit]:
    budget = token_budget or cfg.token_budget_default
    if query:
        hits = search(conn, cfg, query, token_budget=budget * 3)
        return _budget([h for h in hits if h.path == topic_id], budget)

    rows = conn.execute(
        """
        SELECT c.chunk_id, d.path, d.title, c.heading_path, c.text, c.n_tokens,
               d.source_type, d.domain, d.trust_tier, d.folder
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE d.path = ?
        ORDER BY c.ordinal
        """,
        (topic_id,),
    ).fetchall()
    hits = [_row_to_hit(r) for r in rows]
    for h in hits:
        h.sources = ["doc"]
    return _budget(hits, budget)
