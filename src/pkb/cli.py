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

from . import braintrust_obs, config as cfg_module
from . import doctor as doctor_module
from . import evals as evals_module
from . import indexer, ingest, qdrant_store, retriever, stats as stats_module, store

app = typer.Typer(no_args_is_help=True, add_completion=False)
ingest_app = typer.Typer(
    no_args_is_help=True,
    help="Prepare, validate, and index a selected book/doc folder.",
)
app.add_typer(ingest_app, name="ingest")
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


@app.command("qdrant-check")
def qdrant_check() -> None:
    """Verify Qdrant connection and ensure the configured collection exists."""
    cfg = cfg_module.load()
    if not qdrant_store.enabled(cfg):
        console.print(
            "[yellow]Qdrant disabled[/yellow] "
            "Set PKB_VECTOR_BACKEND=qdrant to use Qdrant."
        )
        raise typer.Exit(1)
    qdrant_store.ensure_collection(cfg)
    console.print(
        f"[green]Qdrant ready[/green] url={cfg.qdrant_url or 'http://localhost:6333'} "
        f"collection={cfg.qdrant_collection} dim={cfg.embed_dim}"
    )


@app.command("qdrant-backfill")
def qdrant_backfill(
    batch_size: int = typer.Option(256, "--batch-size", min=1),
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Drop and recreate the collection before copying SQLite vectors.",
    ),
) -> None:
    """Copy existing SQLite vectors into Qdrant without re-embedding markdown."""
    cfg = cfg_module.load()
    if not qdrant_store.enabled(cfg):
        console.print(
            "[yellow]Qdrant disabled[/yellow] "
            "Set PKB_VECTOR_BACKEND=qdrant before backfilling."
        )
        raise typer.Exit(1)
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    if recreate:
        qdrant_store.recreate_collection(cfg)
    count = qdrant_store.backfill_from_sqlite(conn, cfg, batch_size=batch_size)
    console.print(
        f"[green]Backfilled[/green] {count} chunks into "
        f"{cfg.qdrant_collection}."
    )


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


@ingest_app.command("annotate")
def ingest_annotate(
    source: Path = typer.Argument(..., exists=True, readable=True),
    domain: str = typer.Option(..., "--domain", help="Filterable domain, e.g. ai."),
    source_type: str = typer.Option(
        "book",
        "--source-type",
        help="book, paper, blog, own-note, ...",
    ),
    trust_tier: int = typer.Option(2, "--trust-tier", min=0, max=3),
    tag: Optional[list[str]] = typer.Option(
        None,
        "--tag",
        help="Repeatable tag to merge into files.",
    ),
    collection: Optional[str] = typer.Option(
        None,
        "--collection",
        help="Book/doc collection name.",
    ),
    freshness_status: str = typer.Option("current", "--freshness-status"),
    last_reviewed: Optional[str] = typer.Option(
        None,
        "--last-reviewed",
        help="YYYY-MM-DD; defaults to today.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing metadata values.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change without writing files.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Add missing editable front matter to a book/doc folder without indexing it."""
    files = ingest.markdown_files(source)
    root = source if source.is_dir() else source.parent
    results = ingest.annotate_files(
        files,
        root=root,
        domain=domain,
        source_type=source_type,
        trust_tier=trust_tier,
        tags=tag,
        collection=collection,
        freshness_status=freshness_status,
        last_reviewed=last_reviewed,
        overwrite=overwrite,
        dry_run=dry_run,
    )
    if json_output:
        console.print_json(json.dumps([result.to_dict() for result in results]))
        return

    changed = [result for result in results if result.changed]
    verb = "Would update" if dry_run else "Updated"
    console.print(f"[green]{verb}[/] {len(changed)} / {len(results)} markdown files.")
    for result in changed:
        console.print(f"[cyan]{result.path}[/cyan] +{', '.join(result.added_keys)}")


@ingest_app.command("check")
def ingest_check(
    source: Path = typer.Argument(..., exists=True, readable=True),
    require_summary_for_tier: Optional[int] = typer.Option(
        None,
        "--require-summary-for-tier",
        min=0,
        max=3,
        help="Fail when files at this tier or higher have no summary.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate required metadata for a selected book/doc folder."""
    cfg = cfg_module.load()
    files = ingest.markdown_files(source)
    root = (
        cfg.kb_root
        if source.resolve().is_relative_to(cfg.kb_root.resolve())
        else (source if source.is_dir() else source.parent)
    )
    domains, source_types = ingest.load_vocab(cfg)
    issues = ingest.validate_metadata(
        files,
        root=root,
        domains=domains,
        source_types=source_types,
        require_summary_for_tier=require_summary_for_tier,
    )
    has_error = any(issue.severity == "error" for issue in issues)
    if json_output:
        console.print_json(json.dumps([issue.to_dict() for issue in issues]))
    else:
        color = "green" if not has_error else "red"
        console.print(
            f"[{color}]metadata ok={not has_error}[/] files={len(files)} "
            f"issues={len(issues)}"
        )
        for issue in issues:
            console.print(
                f"[bold]{issue.severity}[/bold] "
                f"{issue.code} {issue.path}: {issue.message}"
            )

    if has_error:
        raise typer.Exit(1)


@ingest_app.command("index")
def ingest_index(
    source: Path = typer.Argument(..., exists=True, readable=True),
    require_summary_for_tier: Optional[int] = typer.Option(
        None,
        "--require-summary-for-tier",
        min=0,
        max=3,
        help="Fail when files at this tier or higher have no summary.",
    ),
) -> None:
    """Index only this selected book/doc folder after metadata validation passes."""
    cfg = cfg_module.load()
    files = ingest.markdown_files(source)
    ingest.ensure_under_kb(files, cfg)
    domains, source_types = ingest.load_vocab(cfg)
    issues = ingest.validate_metadata(
        files,
        root=cfg.kb_root,
        domains=domains,
        source_types=source_types,
        require_summary_for_tier=require_summary_for_tier,
    )
    for issue in issues:
        console.print(
            f"[bold]{issue.severity}[/bold] "
            f"{issue.code} {issue.path}: {issue.message}"
        )
    if any(issue.severity == "error" for issue in issues):
        raise typer.Exit(1)

    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)
    n_files, n_chunks = indexer._index_files(conn, cfg, files, "Ingesting")
    console.print(f"[green]Indexed[/green] {n_files} files / {n_chunks} chunks.")


@app.command("eval")
def eval_command(
    eval_path: Path = typer.Argument(..., exists=True, readable=True),
    k: int = typer.Option(10, "--k"),
    min_recall: float = typer.Option(1.0, "--min-recall", min=0.0, max=1.0),
    min_mrr: float = typer.Option(0.0, "--min-mrr", min=0.0, max=1.0),
    log_braintrust: bool = typer.Option(
        True,
        "--braintrust/--no-braintrust",
        help="Log aggregate eval results to Braintrust when configured.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the JSON report to a file as well as stdout.",
    ),
) -> None:
    """Run retrieval evals from JSONL fixtures."""
    cfg = cfg_module.load()
    report = evals_module.run_eval(
        cfg,
        eval_path,
        k=k,
        min_recall=min_recall,
        min_mrr=min_mrr,
    )
    payload = report.to_dict()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    console.print_json(json.dumps(payload))
    if log_braintrust:
        braintrust_obs.log_eval_report(
            cfg,
            eval_path=eval_path,
            report=report,
            min_recall=min_recall,
            min_mrr=min_mrr,
        )
        braintrust_obs.flush()
    if not report.ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
