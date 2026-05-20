"""
Indexing pipeline. Two modes:

    build  — full (re)index of kb_root. Drops any docs no longer on disk.
    sync   — incremental; only re-chunk files whose mtime changed since last index.

Both are restartable: writes happen in batched transactions, so Ctrl-C is safe.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import store
from .chunker import Chunk, chunk_file, walk_kb
from .config import Config
from .embed import embed_passages

console = Console()


def _index_files(conn, cfg: Config, paths: list[Path], label: str) -> tuple[int, int]:
    """Chunk + embed + persist. Returns (n_files, n_chunks)."""
    n_files = n_chunks = 0
    if not paths:
        return 0, 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("• {task.fields[chunks]} chunks"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task(label, total=len(paths), chunks=0)

        # Process in file-sized batches so embedding is parallelizable but writes stay atomic.
        BATCH_FILES = 16
        for i in range(0, len(paths), BATCH_FILES):
            file_batch = paths[i : i + BATCH_FILES]
            all_chunks: list[Chunk] = []
            for path in file_batch:
                chs = chunk_file(
                    path,
                    cfg.kb_root,
                    target=cfg.chunk_target_tokens,
                    hard_max=cfg.chunk_max_tokens,
                    overlap=cfg.chunk_overlap_tokens,
                )
                all_chunks.extend(chs)

            if not all_chunks:
                prog.update(task, advance=len(file_batch))
                continue

            embeddings = embed_passages(
                (c.text for c in all_chunks),
                model=cfg.embed_model,
                cache_dir=cfg.cache_dir,
            )

            # Group by doc, write atomically.
            with conn:
                docs: dict[str, list[tuple[Chunk, list[float]]]] = {}
                for ch, vec in zip(all_chunks, embeddings):
                    docs.setdefault(ch.doc_id, []).append((ch, vec))

                for doc_id, pairs in docs.items():
                    head = pairs[0][0]
                    store.delete_doc_chunks(conn, doc_id)
                    store.upsert_document(
                        conn,
                        doc_id=doc_id,
                        path=head.path,
                        title=head.title,
                        tags=head.tags,
                        source_type=head.source_type,
                        domain=head.domain,
                        trust_tier=head.trust_tier,
                        folder=head.folder,
                        mtime=head.mtime,
                        n_chunks=len(pairs),
                    )
                    for idx, (ch, vec) in enumerate(pairs):
                        store.insert_chunk(
                            conn,
                            chunk_id=ch.chunk_id,
                            doc_id=ch.doc_id,
                            ordinal=idx,
                            heading_path=ch.heading_path,
                            text=ch.text,
                            n_tokens=ch.n_tokens,
                            embedding=vec,
                        )

            n_files += len(file_batch)
            n_chunks += len(all_chunks)
            prog.update(task, advance=len(file_batch), chunks=n_chunks)

    return n_files, n_chunks


def build(cfg: Config) -> None:
    """Full reindex."""
    console.print(f"[bold]Indexing[/bold] {cfg.kb_root}  →  {cfg.db_path}")
    if not cfg.kb_root.exists():
        raise SystemExit(f"KB root does not exist: {cfg.kb_root}")

    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)

    paths = list(walk_kb(cfg.kb_root))
    rel_on_disk = {str(p.relative_to(cfg.kb_root).as_posix()) for p in paths}

    # Drop docs that no longer exist on disk.
    indexed = store.all_paths(conn)
    stale = indexed - rel_on_disk
    if stale:
        console.print(f"[yellow]Removing {len(stale)} deleted files from index[/yellow]")
        with conn:
            for rel in stale:
                row = conn.execute("SELECT doc_id FROM documents WHERE path = ?", (rel,)).fetchone()
                if row:
                    store.delete_doc_chunks(conn, row["doc_id"])
                    conn.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))

    nf, nc = _index_files(conn, cfg, paths, "Indexing")
    console.print(f"[green]Done.[/green] {nf} files / {nc} chunks indexed.")


def sync(cfg: Config) -> None:
    """Incremental — only re-index changed files."""
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)

    to_index: list[Path] = []
    for path in walk_kb(cfg.kb_root):
        rel = str(path.relative_to(cfg.kb_root).as_posix())
        old_mtime = store.doc_mtime(conn, rel)
        if old_mtime is None or path.stat().st_mtime > old_mtime + 1e-6:
            to_index.append(path)

    if not to_index:
        console.print("[green]Index up-to-date.[/green]")
        return

    nf, nc = _index_files(conn, cfg, to_index, "Syncing")
    console.print(f"[green]Synced[/green] {nf} files / {nc} chunks.")
