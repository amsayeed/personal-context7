"""Structured index health payloads."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from .config import Config


def _group_counts(conn, column: str) -> dict:
    return {
        r[column] if r[column] is not None else "": r["n"]
        for r in conn.execute(f"SELECT {column}, COUNT(*) AS n FROM documents GROUP BY {column}")
    }


def _git_sha(cfg: Config) -> str | None:
    if not (cfg.kb_root / ".git").exists():
        return None
    res = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=cfg.kb_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else None


def collect(conn, cfg: Config) -> dict:
    n_docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    max_mtime = conn.execute("SELECT MAX(mtime) AS m FROM documents").fetchone()["m"]
    db_bytes = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
    last_indexed = (
        datetime.fromtimestamp(max_mtime, tz=timezone.utc).isoformat() if max_mtime else None
    )
    return {
        "kb_root": str(cfg.kb_root),
        "db_path": str(cfg.db_path),
        "db_bytes": db_bytes,
        "git_sha": _git_sha(cfg),
        "documents": n_docs,
        "chunks": n_chunks,
        "last_indexed_source_mtime": last_indexed,
        "by_tier": _group_counts(conn, "trust_tier"),
        "by_source_type": _group_counts(conn, "source_type"),
        "by_domain": _group_counts(conn, "domain"),
        "by_folder": _group_counts(conn, "folder"),
        "by_freshness_status": _group_counts(conn, "freshness_status"),
        "embed_model": cfg.embed_model,
        "embed_dim": cfg.embed_dim,
        "rerank_enabled": cfg.rerank_enabled,
        "rerank_model": cfg.rerank_model,
        "transport": cfg.transport,
    }
