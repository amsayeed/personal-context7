"""Optional Braintrust observability for PKB retrieval and evals.

The default payload is intentionally sanitized: we log queries, source paths,
headings, scores, and aggregate eval metrics, but not full copyrighted book
chunks or private note text unless explicitly enabled.
"""

from __future__ import annotations

import logging
import random
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .config import Config

log = logging.getLogger("pkb.braintrust")


def enabled(cfg: Config) -> bool:
    return (
        cfg.braintrust_enabled
        and bool(cfg.braintrust_api_key)
        and bool(cfg.braintrust_project_id or cfg.braintrust_project)
    )


@lru_cache(maxsize=8)
def _logger(
    api_key: str,
    project_id: str | None,
    project: str | None,
):
    import braintrust

    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "async_flush": True,
    }
    if project_id:
        kwargs["project_id"] = project_id
    elif project:
        kwargs["project"] = project
    return braintrust.init_logger(**kwargs)


def _get_logger(cfg: Config):
    if not enabled(cfg):
        return None
    try:
        return _logger(
            cfg.braintrust_api_key or "",
            cfg.braintrust_project_id,
            cfg.braintrust_project,
        )
    except Exception:
        log.exception("failed to initialize Braintrust logger")
        return None


def _sampled(cfg: Config) -> bool:
    rate = max(0.0, min(1.0, cfg.braintrust_sample_rate))
    return rate >= 1.0 or random.random() < rate


def _filters_dict(filters: Any | None) -> dict[str, Any]:
    if filters is None:
        return {}
    return {
        "paths": getattr(filters, "paths", None),
        "tags": getattr(filters, "tags", None),
        "source_types": getattr(filters, "source_types", None),
        "domains": getattr(filters, "domains", None),
        "folders": getattr(filters, "folders", None),
        "min_tier": getattr(filters, "min_tier", None),
    }


def _hit_summary(hit: Any, cfg: Config) -> dict[str, Any]:
    item = {
        "chunk_id": getattr(hit, "chunk_id", None),
        "source": getattr(hit, "path", None),
        "title": getattr(hit, "title", None),
        "heading_path": getattr(hit, "heading_path", None),
        "score": getattr(hit, "score", None),
        "retrievers": getattr(hit, "sources", None),
        "n_tokens": getattr(hit, "n_tokens", None),
        "source_type": getattr(hit, "source_type", None),
        "domain": getattr(hit, "domain", None),
        "trust_tier": getattr(hit, "trust_tier", None),
        "freshness_status": getattr(hit, "freshness_status", None),
    }
    if cfg.braintrust_log_text:
        text = getattr(hit, "text", "") or ""
        item["text_preview"] = text[: cfg.braintrust_text_max_chars]
    return item


def log_retrieval(
    cfg: Config,
    *,
    tool: str,
    query: str | None = None,
    queries: list[str] | None = None,
    hits: Iterable[Any],
    filters: Any | None = None,
    token_budget: int | None = None,
    elapsed_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log one retrieval call to Braintrust if enabled; never raises."""
    if not enabled(cfg) or not _sampled(cfg):
        return
    logger = _get_logger(cfg)
    if logger is None:
        return

    hits_list = list(hits)
    try:
        with logger.start_span(
            name=f"pkb.{tool}",
            input={
                "query": query,
                "queries": queries,
                "filters": _filters_dict(filters),
                "token_budget": token_budget,
            },
            metadata={
                "vector_backend": cfg.vector_backend,
                "qdrant_collection": (
                    cfg.qdrant_collection if cfg.vector_backend == "qdrant" else None
                ),
                "rerank_enabled": cfg.rerank_enabled,
                "final_topk": cfg.final_topk,
                "elapsed_ms": elapsed_ms,
                "log_text": cfg.braintrust_log_text,
                **(extra or {}),
            },
            tags=["pkb", "retrieval", cfg.vector_backend, tool],
        ) as span:
            span.log(
                output={
                    "hit_count": len(hits_list),
                    "hits": [_hit_summary(hit, cfg) for hit in hits_list],
                }
            )
    except Exception:
        log.exception("failed to log retrieval to Braintrust")


def log_topic_resolution(
    cfg: Config,
    *,
    tool: str,
    query: str,
    topics: Iterable[Any],
    filters: Any | None = None,
    limit: int | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Log topic resolution results without chunk text."""
    if not enabled(cfg) or not _sampled(cfg):
        return
    logger = _get_logger(cfg)
    if logger is None:
        return

    topic_list = list(topics)
    try:
        with logger.start_span(
            name=f"pkb.{tool}",
            input={
                "query": query,
                "filters": _filters_dict(filters),
                "limit": limit,
            },
            metadata={
                "vector_backend": cfg.vector_backend,
                "elapsed_ms": elapsed_ms,
            },
            tags=["pkb", "topic-resolution", tool],
        ) as span:
            span.log(
                output={
                    "topic_count": len(topic_list),
                    "topics": [
                        {
                            "topic_id": getattr(topic, "topic_id", None),
                            "title": getattr(topic, "title", None),
                            "score": getattr(topic, "score", None),
                            "source_type": getattr(topic, "source_type", None),
                            "domain": getattr(topic, "domain", None),
                            "trust_tier": getattr(topic, "trust_tier", None),
                        }
                        for topic in topic_list
                    ],
                }
            )
    except Exception:
        log.exception("failed to log topic resolution to Braintrust")


def log_eval_report(
    cfg: Config,
    *,
    eval_path: Path,
    report: Any,
    min_recall: float,
    min_mrr: float,
) -> None:
    """Log aggregate `pkb eval` results to Braintrust if enabled; never raises."""
    if not enabled(cfg):
        return
    logger = _get_logger(cfg)
    if logger is None:
        return

    payload = report.to_dict()
    results = payload.get("results", [])
    failed = [
        {
            "question": result.get("question"),
            "expected_sources": result.get("expected_sources"),
            "returned_sources": result.get("returned_sources"),
            "reciprocal_rank": result.get("reciprocal_rank"),
            "source_coverage": result.get("source_coverage"),
        }
        for result in results
        if not result.get("hit")
    ]
    try:
        with logger.start_span(
            name="pkb.eval",
            input={
                "eval_path": str(eval_path),
                "k": payload.get("k"),
                "min_recall": min_recall,
                "min_mrr": min_mrr,
            },
            metadata={
                "vector_backend": cfg.vector_backend,
                "qdrant_collection": (
                    cfg.qdrant_collection if cfg.vector_backend == "qdrant" else None
                ),
                "cases": payload.get("cases"),
                "failed_cases": failed,
            },
            tags=["pkb", "eval", cfg.vector_backend],
        ) as span:
            span.log(
                output={
                    "ok": payload.get("ok"),
                    "cases": payload.get("cases"),
                    "failed_count": len(failed),
                },
                scores={
                    "ok": 1.0 if payload.get("ok") else 0.0,
                    "recall_at_k": float(payload.get("recall_at_k") or 0.0),
                    "mrr": float(payload.get("mrr") or 0.0),
                    "avg_source_coverage": float(payload.get("avg_source_coverage") or 0.0),
                    "avg_term_coverage": float(payload.get("avg_term_coverage") or 0.0),
                },
            )
    except Exception:
        log.exception("failed to log eval report to Braintrust")


def flush() -> None:
    """Flush pending Braintrust traces for short-lived CLI commands."""
    try:
        import braintrust

        braintrust.flush()
    except Exception:
        log.exception("failed to flush Braintrust logs")
