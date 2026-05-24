"""Central config — env-overridable so the same code works locally, in Docker, on Railway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(key: str, default: Path) -> Path:
    v = os.environ.get(key)
    return Path(v).expanduser().resolve() if v else default


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # --- paths
    kb_root: Path                  # where your markdown lives
    db_path: Path                  # single-file SQLite DB
    cache_dir: Path                # fastembed model cache
    data_dir: Path                 # parent for everything writeable

    # --- chunking
    chunk_target_tokens: int
    chunk_max_tokens: int
    chunk_overlap_tokens: int

    # --- embedding model
    embed_model: str
    embed_dim: int

    # --- vector backend
    vector_backend: str             # "sqlite" | "qdrant"
    qdrant_url: str | None
    qdrant_api_key: str | None
    qdrant_collection: str
    qdrant_timeout: float

    # --- retrieval
    bm25_topk: int
    vec_topk: int
    rrf_k: int
    rerank_enabled: bool
    rerank_model: str
    final_topk: int
    token_budget_default: int

    # --- metadata-aware retrieval (soft boost on documents.trust_tier)
    # tier → multiplier. 0=archive, 1=reference, 2=canonical, 3=your own synthesis.
    tier_boost_0: float
    tier_boost_1: float
    tier_boost_2: float
    tier_boost_3: float

    # --- transport / hosting
    transport: str                 # "stdio" | "sse"
    host: str
    port: int
    api_key: str | None            # bearer token; required for SSE / webhooks

    # --- git-backed KB
    kb_git_remote: str | None      # e.g. "https://x:TOKEN@github.com/you/notes.git"
    kb_git_branch: str             # default "main"
    require_metadata: bool         # fail build/sync when required front matter is missing

    # --- MCP tool exposure
    mcp_profile: str               # "agent" | "admin" | "legacy" | "full"

    @property
    def tier_boost(self) -> dict[int, float]:
        return {
            0: self.tier_boost_0,
            1: self.tier_boost_1,
            2: self.tier_boost_2,
            3: self.tier_boost_3,
        }


def load() -> Config:
    # On Railway we point everything under /data (the persistent volume).
    # Locally we fall back to ./data inside the repo.
    default_data = Path(os.environ.get("PKB_DATA_DIR", "")) if os.environ.get("PKB_DATA_DIR") \
        else Path(__file__).resolve().parents[2] / "data"
    data_dir = Path(str(default_data)).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    kb_root = _env_path("PKB_KB_ROOT", data_dir / "notes")

    return Config(
        kb_root=kb_root,
        data_dir=data_dir,
        db_path=_env_path("PKB_DB_PATH", data_dir / "kb.db"),
        cache_dir=_env_path("PKB_CACHE_DIR", data_dir / ".fastembed_cache"),
        chunk_target_tokens=_env_int("PKB_CHUNK_TARGET", 700),
        chunk_max_tokens=_env_int("PKB_CHUNK_MAX", 1200),
        chunk_overlap_tokens=_env_int("PKB_CHUNK_OVERLAP", 120),
        embed_model=os.environ.get("PKB_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        embed_dim=_env_int("PKB_EMBED_DIM", 384),
        vector_backend=os.environ.get("PKB_VECTOR_BACKEND", "sqlite").lower(),
        qdrant_url=os.environ.get("PKB_QDRANT_URL") or os.environ.get("QDRANT_URL"),
        qdrant_api_key=os.environ.get("PKB_QDRANT_API_KEY") or os.environ.get("QDRANT_API_KEY"),
        qdrant_collection=os.environ.get("PKB_QDRANT_COLLECTION", "pkb_chunks"),
        qdrant_timeout=_env_float("PKB_QDRANT_TIMEOUT", 20.0),
        bm25_topk=_env_int("PKB_BM25_TOPK", 50),
        vec_topk=_env_int("PKB_VEC_TOPK", 50),
        rrf_k=_env_int("PKB_RRF_K", 60),
        rerank_enabled=_env_bool("PKB_RERANK", True),  # ON by default now
        rerank_model=os.environ.get("PKB_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"),
        final_topk=_env_int("PKB_FINAL_TOPK", 12),
        token_budget_default=_env_int("PKB_TOKEN_BUDGET", 4000),
        tier_boost_0=_env_float("PKB_TIER_BOOST_0", 0.6),
        tier_boost_1=_env_float("PKB_TIER_BOOST_1", 1.0),
        tier_boost_2=_env_float("PKB_TIER_BOOST_2", 1.2),
        tier_boost_3=_env_float("PKB_TIER_BOOST_3", 1.5),
        transport=os.environ.get("PKB_TRANSPORT", "stdio"),
        host=os.environ.get("PKB_HOST", "0.0.0.0"),
        port=_env_int("PORT", _env_int("PKB_PORT", 8000)),  # Railway sets PORT
        api_key=os.environ.get("PKB_API_KEY"),
        kb_git_remote=os.environ.get("PKB_KB_GIT_REMOTE"),
        kb_git_branch=os.environ.get("PKB_KB_GIT_BRANCH", "main"),
        require_metadata=_env_bool("PKB_REQUIRE_METADATA", False),
        mcp_profile=os.environ.get("PKB_MCP_PROFILE", "agent").lower(),
    )
