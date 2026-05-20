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
from typing import Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import config as cfg_module
from . import stats as stats_module
from . import store
from .sync import sync_now

log = logging.getLogger("pkb.http")


# --- bearer auth -----------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <api_key>` on everything except UNPROTECTED_PATHS."""

    UNPROTECTED_PATHS = {"/healthz"}

    def __init__(self, app, *, api_key: str) -> None:
        super().__init__(app)
        self._key = api_key

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in self.UNPROTECTED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time compare to avoid leaking key length / prefix via timing.
        if not token or not hmac.compare_digest(token, self._key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


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
        Route("/stats", stats, methods=["GET"]),
        Mount("/", app=mcp_sse_app),  # /sse and /messages live here
    ]
    return Starlette(routes=routes, middleware=middleware)
