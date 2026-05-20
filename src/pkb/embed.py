"""
Thin wrapper around fastembed so the rest of the codebase doesn't import it directly.

fastembed downloads ONNX weights to `cache_dir` on first use (~80MB for bge-small).
Batched encode keeps CPU usage saturated without blowing memory.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

from fastembed import TextEmbedding


@lru_cache(maxsize=2)
def _model(name: str, cache_dir: str) -> TextEmbedding:
    return TextEmbedding(model_name=name, cache_dir=cache_dir)


def embed_passages(
    texts: Iterable[str], *, model: str, cache_dir: Path, batch_size: int = 64
) -> list[list[float]]:
    m = _model(model, str(cache_dir))
    return [list(v) for v in m.embed(list(texts), batch_size=batch_size)]


def embed_query(text: str, *, model: str, cache_dir: Path) -> list[float]:
    m = _model(model, str(cache_dir))
    # bge models recommend a query prefix; fastembed's `query_embed` handles it.
    vecs = list(m.query_embed([text]))
    return list(vecs[0])
