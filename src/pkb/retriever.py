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

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

from .config import Config
from .embed import embed_query
from . import qdrant_store
from .store import bm25_search, vec_search as sqlite_vec_search


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "i",
    "in", "is", "it", "of", "on", "or", "should", "the", "to", "vs", "what",
    "when", "where", "which", "who", "why", "with", "would",
}


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
    summary: str = ""
    aliases: list[str] = field(default_factory=list)
    key_concepts: list[str] = field(default_factory=list)
    canonical_for: list[str] = field(default_factory=list)
    canonical_questions: list[str] = field(default_factory=list)
    last_reviewed: str = ""
    freshness_status: str = ""
    score: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass
class Filters:
    paths: list[str] | None = None
    tags: list[str] | None = None
    source_types: list[str] | None = None
    domains: list[str] | None = None
    folders: list[str] | None = None
    min_tier: int | None = None


def _row_to_hit(r) -> Hit:
    def value(key: str, default=None):
        return r[key] if key in r.keys() else default

    def list_value(key: str) -> list[str]:
        raw = value(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

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
        summary=value("summary", "") or "",
        aliases=list_value("aliases_json"),
        key_concepts=list_value("key_concepts_json"),
        canonical_for=list_value("canonical_for_json"),
        canonical_questions=list_value("canonical_questions_json"),
        last_reviewed=value("last_reviewed", "") or "",
        freshness_status=value("freshness_status", "") or "",
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


def _apply_query_metadata_boost(hits: list[Hit], query: str) -> list[Hit]:
    query_l = query.lower()
    for h in hits:
        if any(term.lower() in query_l for term in h.canonical_for):
            h.score *= 1.35
        elif any(term.lower() in query_l for term in h.aliases):
            h.score *= 1.2
        elif any(term.lower() in query_l for term in h.key_concepts):
            h.score *= 1.1
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
    prior_scores = [h.score for h in hits]
    ce_scores = [float(s) for s in scores]

    def normalize(values: list[float]) -> list[float]:
        if not values:
            return []
        lo, hi = min(values), max(values)
        if hi == lo:
            return [1.0 for _ in values]
        return [(v - lo) / (hi - lo) for v in values]

    prior_norm = normalize(prior_scores)
    ce_norm = normalize(ce_scores)
    for idx, h in enumerate(hits):
        ce_part = ce_norm[idx] if idx < len(ce_norm) else 0.0
        h.score = (0.85 * ce_part) + (0.15 * prior_norm[idx])
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
        paths=filt.paths, tags=filt.tags, source_types=filt.source_types,
        domains=filt.domains, folders=filt.folders, min_tier=filt.min_tier,
    )
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bm = ex.submit(bm25_search, conn, query, cfg.bm25_topk, **fkw)
        f_vc = ex.submit(
            lambda: vec_search(
                conn,
                cfg,
                embed_query(query, model=cfg.embed_model, cache_dir=cfg.cache_dir),
                cfg.vec_topk, **fkw,
            )
        )
        return f_bm.result(), f_vc.result()


def vec_search(
    conn: sqlite3.Connection,
    cfg: Config,
    embedding,
    k: int,
    **filters,
) -> list:
    """Dispatch dense vector search to SQLite fallback or Qdrant."""
    if qdrant_store.enabled(cfg):
        return qdrant_store.vec_search(conn, cfg, embedding, k, **filters)
    return sqlite_vec_search(conn, embedding, k, **filters)


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

    boosted = _apply_query_metadata_boost(_apply_tier_boost(fused, cfg), query)
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
    # Rerank against the *joined* query — best heuristic when sub-queries are
    # different facets of one question.
    joined = " ; ".join(queries)
    fused = _rrf_fuse(*per_query_results, k=cfg.rrf_k)
    boosted = _apply_query_metadata_boost(_apply_tier_boost(fused, cfg), joined)
    top = boosted[: max(cfg.final_topk * 3, 30)]
    reranked = _maybe_rerank(joined, top, cfg)
    return _budget(reranked[: cfg.final_topk], budget)


def _query_variants(query: str) -> list[str]:
    """Deterministic query expansion for agents that should not choose retrieval mode."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_/-]*", query.lower())
    keywords = " ".join(w for w in words if w not in _STOPWORDS)
    variants = [query]
    if keywords and keywords != query.lower():
        variants.append(keywords)
    if keywords:
        variants.append(f"overview tradeoffs failure modes {keywords}")
        variants.append(f"definition examples when to use {keywords}")

    seen: set[str] = set()
    out: list[str] = []
    for variant in variants:
        normalized = " ".join(variant.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            out.append(normalized)
    return out[:4]


def smart_search(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    *,
    filt: Filters | None = None,
    token_budget: int | None = None,
) -> list[Hit]:
    """Search with built-in deterministic query expansion and RRF fusion."""
    variants = _query_variants(query)
    if len(variants) == 1:
        hits = search(conn, cfg, query, filt=filt, token_budget=token_budget)
    else:
        hits = multi_search(conn, cfg, variants, filt=filt, token_budget=token_budget)
    for h in hits:
        if "smart" not in h.sources:
            h.sources.append("smart")
    return hits


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
        paths=filt.paths, tags=filt.tags, source_types=filt.source_types,
        domains=filt.domains, folders=filt.folders, min_tier=filt.min_tier,
    )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bm = ex.submit(bm25_search, conn, query, cfg.bm25_topk, **fkw)
        f_vc = ex.submit(
            lambda: vec_search(
                conn,
                cfg,
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

    boosted = _apply_query_metadata_boost(_apply_tier_boost(fused, cfg), query)
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
    score: float
    summary: str
    aliases: list[str]
    key_concepts: list[str]
    canonical_for: list[str]
    canonical_questions: list[str]
    last_reviewed: str
    freshness_status: str


def resolve_topic(
    conn: sqlite3.Connection, cfg: Config, query: str,
    limit: int = 8, filt: Filters | None = None,
) -> list[Topic]:
    """Mirrors Context7's resolve-library-id: returns candidate *documents*."""
    hits = smart_search(conn, cfg, query, filt=filt, token_budget=8192)
    seen: dict[str, Topic] = {}
    for h in hits:
        if h.path in seen:
            continue
        row = conn.execute(
            """
            SELECT tags_json, source_type, domain, trust_tier, summary, aliases_json,
                   key_concepts_json, canonical_for_json, canonical_questions_json,
                   last_reviewed, freshness_status
            FROM documents
            WHERE path = ?
            """,
            (h.path,),
        ).fetchone()
        tags = json.loads(row["tags_json"]) if row and row["tags_json"] else []
        def load_list(key: str) -> list[str]:
            if not row or not row[key]:
                return []
            data = json.loads(row[key])
            return data if isinstance(data, list) else []
        seen[h.path] = Topic(
            topic_id=h.path, title=h.title, tags=tags,
            source_type=row["source_type"] or "unknown",
            domain=row["domain"] or "unknown",
            trust_tier=int(row["trust_tier"] or 1),
            snippet=h.heading_path,
            score=h.score,
            summary=row["summary"] or "",
            aliases=load_list("aliases_json"),
            key_concepts=load_list("key_concepts_json"),
            canonical_for=load_list("canonical_for_json"),
            canonical_questions=load_list("canonical_questions_json"),
            last_reviewed=row["last_reviewed"] or "",
            freshness_status=row["freshness_status"] or "",
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
        return search(
            conn,
            cfg,
            query,
            filt=Filters(paths=[topic_id]),
            token_budget=budget,
        )

    rows = conn.execute(
        """
        SELECT c.chunk_id, d.path, d.title, c.heading_path, c.text, c.n_tokens,
               d.source_type, d.domain, d.trust_tier, d.folder,
               d.summary, d.aliases_json, d.key_concepts_json, d.canonical_for_json,
               d.canonical_questions_json, d.last_reviewed, d.freshness_status
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


def hit_record(h: Hit, *, include_text: bool = True) -> dict:
    record = {
        "chunk_id": h.chunk_id,
        "source": h.path,
        "title": h.title,
        "heading_path": h.heading_path,
        "score": h.score,
        "sources": h.sources,
        "n_tokens": h.n_tokens,
        "metadata": {
            "source_type": h.source_type,
            "domain": h.domain,
            "trust_tier": h.trust_tier,
            "folder": h.folder,
            "summary": h.summary,
            "aliases": h.aliases,
            "key_concepts": h.key_concepts,
            "canonical_for": h.canonical_for,
            "canonical_questions": h.canonical_questions,
            "last_reviewed": h.last_reviewed,
            "freshness_status": h.freshness_status,
        },
    }
    if include_text:
        record["text"] = h.text
    return record


def topic_record(t: Topic) -> dict:
    return {
        "topic_id": t.topic_id,
        "title": t.title,
        "score": t.score,
        "snippet": t.snippet,
        "tags": t.tags,
        "source_type": t.source_type,
        "domain": t.domain,
        "trust_tier": t.trust_tier,
        "summary": t.summary,
        "aliases": t.aliases,
        "key_concepts": t.key_concepts,
        "canonical_for": t.canonical_for,
        "canonical_questions": t.canonical_questions,
        "last_reviewed": t.last_reviewed,
        "freshness_status": t.freshness_status,
    }
