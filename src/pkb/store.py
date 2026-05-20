"""
SQLite-backed store: FTS5 for BM25, sqlite-vec for ANN, plus a chunks table.

Single .db file. WAL mode for concurrent reads (MCP server) during writes (re-index).
Typed metadata columns (source_type, domain, trust_tier, folder) enable hard filters
and the soft-boost rerank in retriever.py.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Sequence

import sqlite_vec

INDEX_VERSION = 2


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456;  -- 256 MB

CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    title       TEXT,
    tags_json   TEXT,
    source_type TEXT,                       -- book / blog / paper / adr / own-note / unknown
    domain      TEXT,                       -- data / ai / system-design / arch-patterns / unknown
    trust_tier  INTEGER NOT NULL DEFAULT 1, -- 0..3 (see config.tier_boost)
    folder      TEXT,                       -- top-level folder, proxy for category
    summary     TEXT,
    aliases_json TEXT,
    key_concepts_json TEXT,
    canonical_for_json TEXT,
    canonical_questions_json TEXT,
    last_reviewed TEXT,
    freshness_status TEXT,
    index_version INTEGER NOT NULL DEFAULT 2,
    mtime       REAL NOT NULL,
    n_chunks    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);
CREATE INDEX IF NOT EXISTS idx_documents_tier   ON documents(trust_tier);
CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    heading_path  TEXT,
    text          TEXT NOT NULL,
    n_tokens      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    heading_path,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_rowid INTEGER PRIMARY KEY,
    embedding   FLOAT[{dim}]
);
"""

# Light forward-migration so older DBs gain the new columns without a full rebuild.
_MIGRATIONS = [
    "ALTER TABLE documents ADD COLUMN source_type TEXT",
    "ALTER TABLE documents ADD COLUMN domain TEXT",
    "ALTER TABLE documents ADD COLUMN trust_tier INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE documents ADD COLUMN folder TEXT",
    "ALTER TABLE documents ADD COLUMN summary TEXT",
    "ALTER TABLE documents ADD COLUMN aliases_json TEXT",
    "ALTER TABLE documents ADD COLUMN key_concepts_json TEXT",
    "ALTER TABLE documents ADD COLUMN canonical_for_json TEXT",
    "ALTER TABLE documents ADD COLUMN canonical_questions_json TEXT",
    "ALTER TABLE documents ADD COLUMN last_reviewed TEXT",
    "ALTER TABLE documents ADD COLUMN freshness_status TEXT",
    "ALTER TABLE documents ADD COLUMN index_version INTEGER NOT NULL DEFAULT 1",
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection, embed_dim: int) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(VEC_SCHEMA.format(dim=embed_dim))
    # Best-effort migrations: ignore "duplicate column" errors.
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                continue
            raise
    conn.commit()


def f32_blob(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------- writes ----------

def upsert_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    path: str,
    title: str,
    tags: list[str],
    source_type: str,
    domain: str,
    trust_tier: int,
    folder: str,
    summary: str,
    aliases: list[str],
    key_concepts: list[str],
    canonical_for: list[str],
    canonical_questions: list[str],
    last_reviewed: str,
    freshness_status: str,
    mtime: float,
    n_chunks: int,
) -> None:
    conn.execute(
        """
        INSERT INTO documents
            (doc_id, path, title, tags_json, source_type, domain, trust_tier, folder,
             summary, aliases_json, key_concepts_json, canonical_for_json,
             canonical_questions_json, last_reviewed, freshness_status, index_version,
             mtime, n_chunks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
            path=excluded.path,
            title=excluded.title,
            tags_json=excluded.tags_json,
            source_type=excluded.source_type,
            domain=excluded.domain,
            trust_tier=excluded.trust_tier,
            folder=excluded.folder,
            summary=excluded.summary,
            aliases_json=excluded.aliases_json,
            key_concepts_json=excluded.key_concepts_json,
            canonical_for_json=excluded.canonical_for_json,
            canonical_questions_json=excluded.canonical_questions_json,
            last_reviewed=excluded.last_reviewed,
            freshness_status=excluded.freshness_status,
            index_version=excluded.index_version,
            mtime=excluded.mtime,
            n_chunks=excluded.n_chunks
        """,
        (doc_id, path, title, json.dumps(tags),
         source_type, domain, int(trust_tier), folder, summary,
         json.dumps(aliases), json.dumps(key_concepts), json.dumps(canonical_for),
         json.dumps(canonical_questions), last_reviewed, freshness_status, INDEX_VERSION,
         mtime, n_chunks),
    )


def delete_doc_chunks(conn: sqlite3.Connection, doc_id: str) -> None:
    rows = conn.execute("SELECT rowid FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()
    rowids = [r["rowid"] for r in rows]
    if rowids:
        qmarks = ",".join("?" * len(rowids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({qmarks})", rowids)
        conn.execute(f"DELETE FROM chunks_vec WHERE chunk_rowid IN ({qmarks})", rowids)
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    doc_id: str,
    ordinal: int,
    heading_path: str,
    text: str,
    n_tokens: int,
    embedding: Sequence[float],
) -> None:
    cur = conn.execute(
        """
        INSERT INTO chunks (chunk_id, doc_id, ordinal, heading_path, text, n_tokens)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, doc_id, ordinal, heading_path, text, n_tokens),
    )
    rowid = cur.lastrowid
    conn.execute(
        "INSERT INTO chunks_fts (rowid, text, heading_path) VALUES (?, ?, ?)",
        (rowid, text, heading_path),
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_rowid, embedding) VALUES (?, ?)",
        (rowid, f32_blob(embedding)),
    )


# ---------- reads ----------

def doc_mtime(conn: sqlite3.Connection, path: str) -> float | None:
    row = conn.execute("SELECT mtime FROM documents WHERE path = ?", (path,)).fetchone()
    return row["mtime"] if row else None


def doc_index_version(conn: sqlite3.Connection, path: str) -> int | None:
    row = conn.execute("SELECT index_version FROM documents WHERE path = ?", (path,)).fetchone()
    return int(row["index_version"]) if row else None


def all_paths(conn: sqlite3.Connection) -> set[str]:
    return {r["path"] for r in conn.execute("SELECT path FROM documents")}


def doc_metadata(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT path, title, tags_json, source_type, domain, trust_tier, folder,
               summary, aliases_json, key_concepts_json, canonical_for_json,
               canonical_questions_json, last_reviewed, freshness_status, index_version,
               mtime, n_chunks
        FROM documents
        WHERE path = ?
        """,
        (path,),
    ).fetchone()


def _fts_query(q: str) -> str:
    """Sanitize free-text for FTS5 — strip operator chars, OR each token, prefix-expand."""
    safe = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in q)
    terms = [t for t in safe.split() if t]
    if not terms:
        return '""'
    return " OR ".join(f"{t}*" for t in terms)


def _build_filters(
    sql: str,
    params: list,
    *,
    paths: Iterable[str] | None,
    tags: Iterable[str] | None,
    source_types: Iterable[str] | None,
    domains: Iterable[str] | None,
    folders: Iterable[str] | None,
    min_tier: int | None,
) -> str:
    if paths:
        ps = list(paths)
        sql += f" AND d.path IN ({','.join('?' * len(ps))})"
        params.extend(ps)
    if tags:
        for tag in tags:
            sql += " AND d.tags_json LIKE ?"
            params.append(f'%"{tag}"%')
    if source_types:
        st = list(source_types)
        sql += f" AND d.source_type IN ({','.join('?' * len(st))})"
        params.extend(st)
    if domains:
        dm = list(domains)
        sql += f" AND d.domain IN ({','.join('?' * len(dm))})"
        params.extend(dm)
    if folders:
        fl = list(folders)
        sql += f" AND d.folder IN ({','.join('?' * len(fl))})"
        params.extend(fl)
    if min_tier is not None:
        sql += " AND d.trust_tier >= ?"
        params.append(int(min_tier))
    return sql


def bm25_search(
    conn: sqlite3.Connection,
    query: str,
    k: int,
    *,
    paths: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    source_types: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
    folders: Iterable[str] | None = None,
    min_tier: int | None = None,
) -> list[sqlite3.Row]:
    sql = """
    SELECT
        c.chunk_id, d.path, d.title, c.heading_path, c.text, c.n_tokens,
        d.source_type, d.domain, d.trust_tier, d.folder,
        d.summary, d.aliases_json, d.key_concepts_json, d.canonical_for_json,
        d.canonical_questions_json, d.last_reviewed, d.freshness_status,
        -bm25(chunks_fts) AS score
    FROM chunks_fts
    JOIN chunks   c ON c.rowid = chunks_fts.rowid
    JOIN documents d ON d.doc_id = c.doc_id
    WHERE chunks_fts MATCH ?
    """
    params: list = [_fts_query(query)]
    sql = _build_filters(sql, params, paths=paths, tags=tags, source_types=source_types,
                         domains=domains, folders=folders, min_tier=min_tier)
    sql += " ORDER BY score DESC LIMIT ?"
    params.append(k)
    return conn.execute(sql, params).fetchall()


def vec_search(
    conn: sqlite3.Connection,
    embedding: Sequence[float],
    k: int,
    *,
    paths: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    source_types: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
    folders: Iterable[str] | None = None,
    min_tier: int | None = None,
) -> list[sqlite3.Row]:
    """
    KNN over sqlite-vec, then filter by metadata in SQL.

    Note: we over-fetch from vec0 (k * 3) when filters are present so we still
    have ~k passing the filter. Cheap.
    """
    if paths:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()
        overfetch = max(k, int(row["n"] or 0))
    elif any([tags, source_types, domains, folders, min_tier is not None]):
        overfetch = k * 3
    else:
        overfetch = k
    sql = """
    SELECT
        c.chunk_id, d.path, d.title, c.heading_path, c.text, c.n_tokens,
        d.source_type, d.domain, d.trust_tier, d.folder,
        d.summary, d.aliases_json, d.key_concepts_json, d.canonical_for_json,
        d.canonical_questions_json, d.last_reviewed, d.freshness_status,
        v.distance AS distance
    FROM chunks_vec v
    JOIN chunks    c ON c.rowid = v.chunk_rowid
    JOIN documents d ON d.doc_id = c.doc_id
    WHERE v.embedding MATCH ? AND k = ?
    """
    params: list = [f32_blob(embedding), overfetch]
    sql = _build_filters(sql, params, paths=paths, tags=tags, source_types=source_types,
                         domains=domains, folders=folders, min_tier=min_tier)
    sql += " ORDER BY v.distance LIMIT ?"
    params.append(k)
    return conn.execute(sql, params).fetchall()
