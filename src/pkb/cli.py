"""
CLI for indexing, searching, and serving. Useful for local debugging without MCP.

    pkb build           # full reindex
    pkb sync            # incremental
    pkb search "..."    # hybrid query
    pkb topic "..."     # list candidate documents
    pkb serve           # run the MCP server
    pkb stats
"""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config as cfg_module
from . import indexer, retriever, store

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def build() -> None:
    """Full (re)index of the KB root."""
    indexer.build(cfg_module.load())


@app.command()
def sync() -> None:
    """Incremental index — only re-process changed files."""
    indexer.sync(cfg_module.load())


@app.command()
def search(
    query: str = typer.Argument(...),
    tokens: int = typer.Option(2000, "--tokens", "-t"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
) -> None:
    """Hybrid search across the KB."""
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    hits = retriever.search(conn, cfg, query, tags=tag, token_budget=tokens)
    if not hits:
        console.print(f"[yellow]No matches for[/yellow] {query!r}")
        return

    table = Table(title=f"Results for: {query!r}", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Score", width=8)
    table.add_column("Title")
    table.add_column("Heading")
    table.add_column("Path", style="cyan")
    table.add_column("Via", style="magenta")
    for i, h in enumerate(hits, 1):
        table.add_row(
            str(i),
            f"{h.score:.4f}",
            h.title[:40],
            h.heading_path[:50],
            h.path,
            ",".join(h.sources),
        )
    console.print(table)


@app.command()
def topic(query: str = typer.Argument(...), limit: int = 8) -> None:
    """Resolve a query to candidate documents (topic_ids)."""
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    topics = retriever.resolve_topic(conn, cfg, query, limit=limit)
    console.print_json(
        json.dumps(
            [{"topic_id": t.topic_id, "title": t.title, "tags": t.tags, "snippet": t.snippet} for t in topics]
        )
    )


@app.command()
def serve() -> None:
    """Run the MCP server (stdio transport)."""
    from . import mcp_server

    mcp_server.main()


@app.command()
def stats() -> None:
    """Print index stats."""
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    n_docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    console.print(
        {
            "kb_root": str(cfg.kb_root),
            "db_path": str(cfg.db_path),
            "documents": n_docs,
            "chunks": n_chunks,
            "embed_model": cfg.embed_model,
            "embed_dim": cfg.embed_dim,
            "rerank_enabled": cfg.rerank_enabled,
        }
    )


if __name__ == "__main__":
    app()
