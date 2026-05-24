"""
HTTP entrypoint for hosted mode (Railway / Fly / any container PaaS).

Mounts three things behind bearer-token auth:

    GET  /healthz       → unauthenticated, returns {"ok": true}
    *    /sse, /messages → MCP over SSE  (auth required)
    POST /webhook/sync  → trigger git-pull + incremental reindex (auth required)
    GET  /stats         → small JSON about the index (auth required)

Auth: Authorization: Bearer <PKB_API_KEY>. The same key works for SSE and webhooks.
On Railway, set PKB_API_KEY in service variables; rotate by changing it.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config as cfg_module
from . import qdrant_store
from . import stats as stats_module
from . import store
from .sync import sync_now

log = logging.getLogger("pkb.http")


# --- bearer auth -----------------------------------------------------------

class BearerAuthMiddleware:
    """Require `Authorization: Bearer <api_key>` on everything except UNPROTECTED_PATHS."""

    UNPROTECTED_PATHS = {"/healthz"}

    def __init__(self, app: ASGIApp, *, api_key: str) -> None:
        self.app = app
        self._key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in self.UNPROTECTED_PATHS:
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time compare to avoid leaking key length / prefix via timing.
        if not token or not hmac.compare_digest(token, self._key):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# --- route handlers --------------------------------------------------------

async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def webhook_sync(_: Request) -> JSONResponse:
    cfg = cfg_module.load()
    result = sync_now(cfg)
    payload = {
        "ok": result.ok,
        "pulled": result.pulled,
        "n_files": result.n_files,
        "n_chunks": result.n_chunks,
        "message": result.message,
    }
    return JSONResponse(payload, status_code=200 if result.ok else 500)


async def stats(_: Request) -> JSONResponse:
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    return JSONResponse(stats_module.collect(conn, cfg))


async def webhook_qdrant_backfill(request: Request) -> JSONResponse:
    cfg = cfg_module.load()
    if not qdrant_store.enabled(cfg):
        return JSONResponse(
            {"ok": False, "message": "Qdrant is disabled"},
            status_code=400,
        )

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    except Exception:
        payload = {}

    recreate = bool(payload.get("recreate", False))
    batch_size = int(payload.get("batch_size", 256))

    try:
        conn = store.connect(cfg.db_path)
        store.init(conn, cfg.embed_dim)
        if recreate:
            qdrant_store.recreate_collection(cfg)
        count = qdrant_store.backfill_from_sqlite(
            conn,
            cfg,
            batch_size=batch_size,
        )
    except Exception as exc:
        log.exception("qdrant backfill failed")
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=500,
        )

    return JSONResponse(
        {
            "ok": True,
            "chunks": count,
            "collection": cfg.qdrant_collection,
            "recreated": recreate,
        }
    )


# --- app factory -----------------------------------------------------------

def build_app(mcp_sse_app) -> Starlette:
    """
    `mcp_sse_app` is the Starlette app returned by FastMCP.sse_app(). We mount it
    under /  so the SSE endpoints come out at /sse and /messages (MCP defaults).
    """
    cfg = cfg_module.load()
    api_key = cfg.api_key
    if not api_key:
        # Generate one and surface it in the logs once. Better to fail-loud than fail-open.
        api_key = secrets.token_urlsafe(32)
        log.warning(
            "PKB_API_KEY not set — generated an ephemeral key: %s\n"
            "Set PKB_API_KEY in your environment to persist it.",
            api_key,
        )

    middleware = [Middleware(BearerAuthMiddleware, api_key=api_key)]

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/webhook/sync", webhook_sync, methods=["POST"]),
        Route("/webhook/qdrant-backfill", webhook_qdrant_backfill, methods=["POST"]),
        Route("/stats", stats, methods=["GET"]),
        Mount("/", app=mcp_sse_app),  # /sse and /messages live here
    ]
    return Starlette(routes=routes, middleware=middleware)
