"""Qdrant dense-vector backend.

SQLite remains the source for document metadata, BM25, and chunk text. Qdrant
stores dense vectors plus filterable payload fields, then we hydrate matched
chunk IDs from SQLite before fusion/reranking.
"""

from __future__ import annotations

import sqlite3
import json
import struct
import uuid
from functools import lru_cache
from typing import Iterable, Sequence

from . import store
from .chunker import Chunk
from .config import Config

_ENSURED_COLLECTIONS: set[tuple[str, str, int]] = set()


def enabled(cfg: Config) -> bool:
    return cfg.vector_backend == "qdrant"


def _require_client():
    try:
        from qdrant_client import QdrantClient, models
    except Exception as exc:  # pragma: no cover - depends on optional install state
        raise RuntimeError(
            "PKB_VECTOR_BACKEND=qdrant requires qdrant-client. "
            "Install project dependencies or run `pip install qdrant-client`."
        ) from exc
    return QdrantClient, models


@lru_cache(maxsize=8)
def _client(url: str, api_key: str | None, timeout: float):
    QdrantClient, _ = _require_client()
    if url == ":memory:":
        return QdrantClient(":memory:")
    return QdrantClient(url=url, api_key=api_key, timeout=timeout)


def client(cfg: Config):
    url = cfg.qdrant_url or "http://localhost:6333"
    return _client(url, cfg.qdrant_api_key, cfg.qdrant_timeout)


def _collection_key(cfg: Config) -> tuple[str, str, int]:
    return (cfg.qdrant_url or "http://localhost:6333", cfg.qdrant_collection, cfg.embed_dim)


def _point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pkb:{chunk_id}"))


def ensure_collection(cfg: Config) -> None:
    """Create the Qdrant collection and payload indexes if needed."""
    key = _collection_key(cfg)
    if key in _ENSURED_COLLECTIONS:
        return

    _, models = _require_client()
    qd = client(cfg)
    try:
        exists = qd.collection_exists(cfg.qdrant_collection)
    except Exception:
        try:
            qd.get_collection(cfg.qdrant_collection)
            exists = True
        except Exception:
            exists = False

    if not exists:
        qd.create_collection(
            collection_name=cfg.qdrant_collection,
            vectors_config=models.VectorParams(
                size=cfg.embed_dim,
                distance=models.Distance.COSINE,
            ),
        )

    # Best-effort payload indexes. Older/local clients may not support every call,
    # and search correctness does not depend on indexes for small dev datasets.
    for field, schema in (
        ("chunk_id", models.PayloadSchemaType.KEYWORD),
        ("doc_id", models.PayloadSchemaType.KEYWORD),
        ("path", models.PayloadSchemaType.KEYWORD),
        ("source_type", models.PayloadSchemaType.KEYWORD),
        ("domain", models.PayloadSchemaType.KEYWORD),
        ("folder", models.PayloadSchemaType.KEYWORD),
        ("tags", models.PayloadSchemaType.KEYWORD),
        ("trust_tier", models.PayloadSchemaType.INTEGER),
    ):
        try:
            qd.create_payload_index(
                collection_name=cfg.qdrant_collection,
                field_name=field,
                field_schema=schema,
            )
        except Exception:
            continue

    _ENSURED_COLLECTIONS.add(key)


def recreate_collection(cfg: Config) -> None:
    """Drop and recreate the configured Qdrant collection."""
    key = _collection_key(cfg)
    _ENSURED_COLLECTIONS.discard(key)
    qd = client(cfg)
    try:
        if qd.collection_exists(cfg.qdrant_collection):
            qd.delete_collection(cfg.qdrant_collection)
    except Exception:
        try:
            qd.delete_collection(cfg.qdrant_collection)
        except Exception:
            pass
    ensure_collection(cfg)


def delete_doc(cfg: Config, doc_id: str) -> None:
    """Delete all Qdrant points for a document."""
    if not enabled(cfg):
        return
    _, models = _require_client()
    ensure_collection(cfg)
    client(cfg).delete(
        collection_name=cfg.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_id",
                        match=models.MatchValue(value=doc_id),
                    )
                ]
            )
        ),
        wait=True,
    )


def delete_doc_except(cfg: Config, doc_id: str, keep_chunk_ids: Iterable[str]) -> None:
    """Delete Qdrant points for a document except the current chunk IDs."""
    keep = [str(chunk_id) for chunk_id in keep_chunk_ids if str(chunk_id)]
    if not keep:
        delete_doc(cfg, doc_id)
        return
    if not enabled(cfg):
        return
    _, models = _require_client()
    ensure_collection(cfg)
    client(cfg).delete(
        collection_name=cfg.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_id",
                        match=models.MatchValue(value=doc_id),
                    )
                ],
                must_not=[
                    models.FieldCondition(
                        key="chunk_id",
                        match=models.MatchAny(any=keep),
                    )
                ],
            )
        ),
        wait=True,
    )


def upsert_chunks(
    cfg: Config,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
) -> None:
    """Upsert chunk vectors and filterable payload into Qdrant."""
    if not enabled(cfg) or not chunks:
        return
    _, models = _require_client()
    ensure_collection(cfg)

    points = []
    for chunk, embedding in zip(chunks, embeddings):
        points.append(
            models.PointStruct(
                id=_point_id(chunk.chunk_id),
                vector=list(embedding),
                payload={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "path": chunk.path,
                    "title": chunk.title,
                    "heading_path": chunk.heading_path,
                    "source_type": chunk.source_type,
                    "domain": chunk.domain,
                    "trust_tier": int(chunk.trust_tier),
                    "folder": chunk.folder,
                    "tags": list(chunk.tags),
                    "n_tokens": int(chunk.n_tokens),
                },
            )
        )

    client(cfg).upsert(
        collection_name=cfg.qdrant_collection,
        points=points,
        wait=True,
    )


def replace_doc(
    cfg: Config,
    doc_id: str,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
) -> None:
    """Replace a document's Qdrant points without creating a delete-before-upsert gap."""
    if not enabled(cfg):
        return
    upsert_chunks(cfg, chunks, embeddings)
    delete_doc_except(cfg, doc_id, (chunk.chunk_id for chunk in chunks))


def _match_any(models, values: Iterable[str]):
    vals = [str(value) for value in values if str(value)]
    if not vals:
        return None
    if len(vals) == 1:
        return models.MatchValue(value=vals[0])
    return models.MatchAny(any=vals)


def _filter(
    models,
    *,
    paths: Iterable[str] | None,
    tags: Iterable[str] | None,
    source_types: Iterable[str] | None,
    domains: Iterable[str] | None,
    folders: Iterable[str] | None,
    min_tier: int | None,
):
    must = []
    for field, values in (
        ("path", paths),
        ("source_type", source_types),
        ("domain", domains),
        ("folder", folders),
    ):
        if values:
            match = _match_any(models, values)
            if match:
                must.append(models.FieldCondition(key=field, match=match))

    # Require every requested tag. This mirrors the SQLite filter behavior.
    if tags:
        for tag in tags:
            must.append(
                models.FieldCondition(
                    key="tags",
                    match=models.MatchAny(any=[str(tag)]),
                )
            )

    if min_tier is not None:
        must.append(models.FieldCondition(key="trust_tier", range=models.Range(gte=int(min_tier))))

    return models.Filter(must=must) if must else None


def vec_search(
    conn: sqlite3.Connection,
    cfg: Config,
    embedding: Sequence[float],
    k: int,
    *,
    paths: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    source_types: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
    folders: Iterable[str] | None = None,
    min_tier: int | None = None,
) -> list[dict]:
    """KNN over Qdrant, hydrated from SQLite into retriever-compatible rows."""
    _, models = _require_client()
    ensure_collection(cfg)
    qfilter = _filter(
        models,
        paths=paths,
        tags=tags,
        source_types=source_types,
        domains=domains,
        folders=folders,
        min_tier=min_tier,
    )
    result = client(cfg).query_points(
        collection_name=cfg.qdrant_collection,
        query=list(embedding),
        query_filter=qfilter,
        limit=k,
        with_payload=True,
    )

    points = getattr(result, "points", result)
    ids: list[str] = []
    scores: dict[str, float] = {}
    for point in points:
        payload = point.payload or {}
        chunk_id = payload.get("chunk_id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        ids.append(chunk_id)
        scores[chunk_id] = float(point.score)

    rows = store.chunks_by_ids(conn, ids)
    hydrated: list[dict] = []
    for chunk_id in ids:
        row = rows.get(chunk_id)
        if not row:
            continue
        item = {key: row[key] for key in row.keys()}
        vector_score = scores.get(chunk_id, 0.0)
        item["vector_score"] = vector_score
        item["distance"] = 1.0 - vector_score
        hydrated.append(item)
    return hydrated


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _embedding_from_blob(raw, expected_dim: int) -> list[float]:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif not isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw)
    dim = len(raw) // 4
    if dim != expected_dim:
        raise RuntimeError(
            f"SQLite vector dimension mismatch: got {dim}, expected {expected_dim}"
        )
    return list(struct.unpack(f"{dim}f", raw))


def _row_point(models, row):
    tags = _json_list(row["tags_json"])
    return models.PointStruct(
        id=_point_id(row["chunk_id"]),
        vector=_embedding_from_blob(row["embedding"], int(row["embed_dim"])),
        payload={
            "chunk_id": row["chunk_id"],
            "doc_id": row["doc_id"],
            "path": row["path"],
            "title": row["title"],
            "heading_path": row["heading_path"],
            "source_type": row["source_type"] or "unknown",
            "domain": row["domain"] or "unknown",
            "trust_tier": int(row["trust_tier"] or 1),
            "folder": row["folder"] or "",
            "tags": tags,
            "n_tokens": int(row["n_tokens"] or 0),
        },
    )


def backfill_from_sqlite(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    batch_size: int = 256,
) -> int:
    """Copy existing SQLite vectors into Qdrant without re-embedding markdown."""
    if not enabled(cfg):
        raise RuntimeError("PKB_VECTOR_BACKEND must be qdrant to backfill Qdrant")

    _, models = _require_client()
    ensure_collection(cfg)
    qd = client(cfg)

    total = 0
    last_rowid = 0
    while True:
        rows = conn.execute(
            """
            SELECT
                c.rowid AS chunk_rowid,
                c.chunk_id,
                c.doc_id,
                c.heading_path,
                c.n_tokens,
                d.path,
                d.title,
                d.tags_json,
                d.source_type,
                d.domain,
                d.trust_tier,
                d.folder,
                v.embedding,
                ? AS embed_dim
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN chunks_vec v ON v.chunk_rowid = c.rowid
            WHERE c.rowid > ?
            ORDER BY c.rowid
            LIMIT ?
            """,
            (cfg.embed_dim, last_rowid, batch_size),
        ).fetchall()
        if not rows:
            break

        points = []
        for row in rows:
            last_rowid = max(last_rowid, int(row["chunk_rowid"]))
            points.append(_row_point(models, row))

        if points:
            qd.upsert(
                collection_name=cfg.qdrant_collection,
                points=points,
                wait=True,
            )
            total += len(points)

    return total
