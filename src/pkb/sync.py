"""
KB sync.

On boot: if PKB_KB_GIT_REMOTE is set and kb_root is empty/non-git, clone it.
                                otherwise, fast-forward pull.
Sync:    git pull (if remote set) → indexer.sync() to re-process changed files only.

Designed to be idempotent and crash-safe — interrupting mid-sync just means the
next sync re-does the files that didn't finish. SQLite writes are transactional.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import indexer, store
from .config import Config

log = logging.getLogger("pkb.sync")
_URL_AUTH_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<auth>[^@\s/]+)@")
_SCP_AUTH_RE = re.compile(r"(?<!\S)(?P<auth>[^@\s]+)@(?P<host>[^:\s]+):")


@dataclass
class SyncResult:
    ok: bool
    pulled: bool
    n_files: int
    n_chunks: int
    message: str


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    safe_cmd = _safe_cmd(cmd)
    log.info("$ %s%s", safe_cmd, f"  (cwd={cwd})" if cwd else "")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        stderr = _redact_secrets(res.stderr.strip())
        raise RuntimeError(f"command failed: {safe_cmd}\nstderr: {stderr}")
    return res.stdout.strip()


def bootstrap_kb(cfg: Config) -> None:
    """
    Called once at server startup. If a git remote is configured:
      - clone if kb_root is empty or not a git repo
      - else `git pull` to fast-forward
    Safe to call when no remote is configured (no-op).
    """
    if not cfg.kb_git_remote:
        cfg.kb_root.mkdir(parents=True, exist_ok=True)
        return

    cfg.kb_root.parent.mkdir(parents=True, exist_ok=True)
    is_repo = (cfg.kb_root / ".git").exists()
    is_empty = (not cfg.kb_root.exists()) or (not any(cfg.kb_root.iterdir()))

    if not is_repo and is_empty:
        log.info("cloning %s → %s", _safe_remote(cfg.kb_git_remote), cfg.kb_root)
        if cfg.kb_root.exists():
            # Empty dir — `git clone` refuses if dir exists, so clone into a temp parent.
            _run(["git", "clone", "--depth=1", "-b", cfg.kb_git_branch,
                  cfg.kb_git_remote, str(cfg.kb_root)])
        else:
            _run(["git", "clone", "--depth=1", "-b", cfg.kb_git_branch,
                  cfg.kb_git_remote, str(cfg.kb_root)])
    elif is_repo:
        log.info("pulling latest in %s", cfg.kb_root)
        _run(["git", "remote", "set-url", "origin", cfg.kb_git_remote], cwd=cfg.kb_root)
        _run(["git", "fetch", "--depth=1", "origin", cfg.kb_git_branch], cwd=cfg.kb_root)
        _run(["git", "reset", "--hard", f"origin/{cfg.kb_git_branch}"], cwd=cfg.kb_root)
    else:
        raise RuntimeError(
            f"kb_root {cfg.kb_root} is non-empty and not a git repo; refusing to clone over it"
        )


def sync_now(cfg: Config) -> SyncResult:
    """Pull (if remote configured) then run incremental index."""
    pulled = False
    msg_parts: list[str] = []
    try:
        if cfg.kb_git_remote and (cfg.kb_root / ".git").exists():
            out = _run(["git", "pull", "--ff-only"], cwd=cfg.kb_root)
            pulled = True
            msg_parts.append(f"git: {out.splitlines()[-1] if out else 'up-to-date'}")
        elif cfg.kb_git_remote:
            bootstrap_kb(cfg)
            pulled = True
            msg_parts.append("git: bootstrapped")
    except Exception as e:
        return SyncResult(ok=False, pulled=False, n_files=0, n_chunks=0,
                          message=f"git step failed: {e}")

    # Incremental index — read-only on files, transactional on the DB.
    try:
        # Re-use the same helper as the CLI; need the file/chunk counts.
        conn = store.connect(cfg.db_path)
        store.init(conn, cfg.embed_dim)
        from .chunker import walk_kb
        paths = list(walk_kb(cfg.kb_root))
        removed = indexer.remove_stale_docs(conn, cfg, paths, announce=False)
        candidates = []
        for path in paths:
            rel = str(path.relative_to(cfg.kb_root).as_posix())
            old = store.doc_mtime(conn, rel)
            old_version = store.doc_index_version(conn, rel)
            if (
                old is None
                or path.stat().st_mtime > old + 1e-6
                or (old_version or 0) < store.INDEX_VERSION
            ):
                candidates.append(path)
        if not candidates:
            index_msg = "index: up-to-date"
            if removed:
                index_msg += f" ({removed} deleted files removed)"
            return SyncResult(ok=True, pulled=pulled, n_files=0, n_chunks=0,
                              message="; ".join(msg_parts + [index_msg]))
        indexer.ensure_metadata_if_required(cfg, candidates)
        nf, nc = indexer._index_files(conn, cfg, candidates, "Syncing")
        index_msg = f"index: {nf} files / {nc} chunks"
        if removed:
            index_msg += f" ({removed} deleted files removed)"
        return SyncResult(ok=True, pulled=pulled, n_files=nf, n_chunks=nc,
                          message="; ".join(msg_parts + [index_msg]))
    except Exception as e:
        return SyncResult(ok=False, pulled=pulled, n_files=0, n_chunks=0,
                          message=f"index step failed: {e}")


def _safe_remote(url: str) -> str:
    """Redact tokens in URLs like https://x:TOKEN@github.com/owner/repo.git for logs."""
    return _redact_secrets(url)


def _safe_cmd(cmd: list[str]) -> str:
    """Return a printable shell command with credential-bearing URLs redacted."""
    return " ".join(_redact_secrets(part) for part in cmd)


def _redact_secrets(text: str) -> str:
    """Redact auth embedded in URL-like strings without touching unrelated text."""
    if "@" not in text:
        return text
    text = _URL_AUTH_RE.sub(r"\g<scheme><redacted>@", text)
    return _SCP_AUTH_RE.sub(r"<redacted>@\g<host>:", text)
