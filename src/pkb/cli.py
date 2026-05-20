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
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config as cfg_module
from . import doctor as doctor_module
from . import evals as evals_module
from . import indexer, retriever, stats as stats_module, store

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
    hits = retriever.search(
        conn,
        cfg,
        query,
        filt=retriever.Filters(tags=tag),
        token_budget=tokens,
    )
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
def smart(
    query: str = typer.Argument(...),
    tokens: int = typer.Option(2000, "--tokens", "-t"),
    tag: Optional[list[str]] = typer.Option(None, "--tag"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Expanded search with JSON output option for agent debugging."""
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    hits = retriever.smart_search(
        conn,
        cfg,
        query,
        filt=retriever.Filters(tags=tag),
        token_budget=tokens,
    )
    if json_output:
        console.print_json(json.dumps([retriever.hit_record(hit) for hit in hits]))
        return
    if not hits:
        console.print(f"[yellow]No matches for[/yellow] {query!r}")
        return
    for hit in hits:
        console.print(f"[bold]{hit.title}[/bold] [dim]{hit.path}[/dim] {hit.score:.4f}")


@app.command()
def topic(query: str = typer.Argument(...), limit: int = 8) -> None:
    """Resolve a query to candidate documents (topic_ids)."""
    cfg = cfg_module.load()
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    topics = retriever.resolve_topic(conn, cfg, query, limit=limit)
    console.print_json(
        json.dumps(
            [retriever.topic_record(t) for t in topics]
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
    console.print(stats_module.collect(conn, cfg))


@app.command()
def doctor(
    stale_days: int = typer.Option(180, "--stale-days"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check vault metadata, stale reviews, chunks, duplicate titles, and wikilinks."""
    report = doctor_module.run_doctor(cfg_module.load(), stale_days=stale_days)
    if json_output:
        console.print_json(json.dumps(report.to_dict()))
        return
    color = "green" if report.ok else "red"
    console.print(
        f"[{color}]doctor ok={report.ok}[/] files={report.files} "
        f"issues={len(report.issues)}"
    )
    for issue in report.issues:
        console.print(f"[bold]{issue.severity}[/bold] {issue.code} {issue.path}: {issue.message}")


@app.command("eval")
def eval_command(
    eval_path: Path = typer.Argument(..., exists=True, readable=True),
    k: int = typer.Option(10, "--k"),
) -> None:
    """Run retrieval evals from JSONL fixtures."""
    report = evals_module.run_eval(cfg_module.load(), eval_path, k=k)
    console.print_json(json.dumps(report.to_dict()))


if __name__ == "__main__":
    app()
