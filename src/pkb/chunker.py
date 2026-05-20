"""
Heading-aware markdown chunker — Obsidian-friendly.

Why heading-aware: architecture / pattern docs put a *lot* of meaning into headings
("Eventual Consistency > Tradeoffs"). A flat sliding window blows that up. We walk
the heading tree, emit chunks per leaf section, and prepend the heading path so the
embedding model and the reader both see the context.

Obsidian specifics handled:
  - Front matter parsed (title, tags, aliases, source_type, domain, trust_tier).
  - Inline `#tag` extraction from body (merged with front-matter tags, deduped).
  - `[[Wikilinks]]` stay as text — they embed and search fine as-is.
  - Callout fences `> [!note]` treated like normal blockquotes.
  - `.obsidian/`, attachments, and dotfiles skipped at the walker level.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import frontmatter
import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# `#tag` or `#nested/tag` — must NOT be a heading (no preceding `# `) and must
# not be inside code. We capture token-like patterns; Obsidian's own rule.
_INLINE_TAG_RE = re.compile(r"(?<![A-Za-z0-9_/])#([A-Za-z][A-Za-z0-9_\-/]{0,63})")

# Valid trust tiers + sensible aliases so users can write words instead of numbers.
_TIER_ALIASES = {
    "archive": 0, "draft": 0, "raw": 0, "0": 0, 0: 0,
    "reference": 1, "highlight": 1, "1": 1, 1: 1,
    "canonical": 2, "source": 2, "2": 2, 2: 2,
    "synthesis": 3, "own": 3, "my-note": 3, "3": 3, 3: 3,
}


def _tok_count(s: str) -> int:
    return len(_ENC.encode(s, disallowed_special=()))


def _trim_to_tokens(s: str, limit: int) -> str:
    ids = _ENC.encode(s, disallowed_special=())
    if len(ids) <= limit:
        return s
    return _ENC.decode(ids[:limit]).strip()


def _doc_id(rel_path: str) -> str:
    return hashlib.blake2b(rel_path.encode("utf-8"), digest_size=8).hexdigest()


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    path: str
    title: str
    heading_path: str
    tags: list[str]
    source_type: str          # 'book' | 'blog' | 'paper' | 'adr' | 'own-note' | 'unknown'
    domain: str               # 'data' | 'ai' | 'system-design' | 'arch-patterns' | 'unknown'
    trust_tier: int           # 0..3
    folder: str               # top-level folder name (proxy for category)
    summary: str
    aliases: list[str]
    key_concepts: list[str]
    canonical_for: list[str]
    canonical_questions: list[str]
    last_reviewed: str
    freshness_status: str
    text: str
    n_tokens: int
    mtime: float


@dataclass
class _Section:
    headings: list[tuple[int, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def body(self) -> str:
        return "\n".join(self.lines).strip()


def _walk_sections(text: str) -> Iterator[_Section]:
    stack: list[tuple[int, str]] = []
    cur = _Section(headings=list(stack))

    in_fence = False
    for raw in text.splitlines():
        line = raw.rstrip()

        if line.startswith("```"):
            in_fence = not in_fence
            cur.lines.append(raw)
            continue

        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                if cur.lines or cur.headings:
                    yield cur
                level = len(m.group(1))
                title = m.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                cur = _Section(headings=list(stack))
                continue

        cur.lines.append(raw)

    if cur.lines or cur.headings:
        yield cur


def _extract_inline_tags(body: str) -> list[str]:
    """Pull `#tag` tokens from the body, skipping code blocks."""
    tags: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Skip lines that ARE headings (start with `# ... `) — those aren't tags.
        if _HEADING_RE.match(line):
            continue
        for m in _INLINE_TAG_RE.finditer(line):
            tags.append(m.group(1))
    return tags


def _normalize_trust_tier(v) -> int:
    if v is None:
        return 1  # default = reference
    key = v.lower() if isinstance(v, str) else v
    return _TIER_ALIASES.get(key, 1)


def _normalize_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        raw = v.split(",")
    elif isinstance(v, list):
        raw = v
    else:
        raw = [v]
    return [str(item).strip().lstrip("#") for item in raw if str(item).strip()]


def _split_by_tokens(body: str, target: int, hard_max: int, overlap: int) -> list[str]:
    if _tok_count(body) <= hard_max:
        return [body]

    paragraphs = re.split(r"\n{2,}", body)
    out: list[str] = []
    buf: list[str] = []
    buf_tok = 0

    def flush():
        nonlocal buf, buf_tok
        if buf:
            out.append("\n\n".join(buf).strip())
            buf, buf_tok = [], 0

    for p in paragraphs:
        pt = _tok_count(p)
        if pt > hard_max:
            flush()
            sentences = re.split(r"(?<=[.!?])\s+", p)
            sbuf, stok = [], 0
            for s in sentences:
                st = _tok_count(s)
                if stok + st > target and sbuf:
                    out.append(" ".join(sbuf).strip())
                    sbuf, stok = [], 0
                sbuf.append(s)
                stok += st
            if sbuf:
                out.append(" ".join(sbuf).strip())
            continue

        if buf_tok + pt > target and buf:
            flush()
        buf.append(p)
        buf_tok += pt
        if buf_tok >= hard_max:
            flush()

    flush()

    if overlap > 0 and len(out) > 1:
        with_overlap: list[str] = [out[0]]
        for i in range(1, len(out)):
            prev = out[i - 1]
            tail_ids = _ENC.encode(prev, disallowed_special=())[-overlap:]
            tail = _ENC.decode(tail_ids)
            with_overlap.append(tail + "\n\n" + out[i])
        out = with_overlap

    return out


def chunk_file(
    path: Path, kb_root: Path, *, target: int, hard_max: int, overlap: int
) -> list[Chunk]:
    rel = str(path.relative_to(kb_root).as_posix())
    raw = path.read_text(encoding="utf-8", errors="replace")
    post = frontmatter.loads(raw)
    body_text = post.content

    fm = post.metadata or {}
    fm_title = fm.get("title")
    fm_tags = fm.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [t.strip().lstrip("#") for t in fm_tags.split(",") if t.strip()]
    elif isinstance(fm_tags, list):
        fm_tags = [str(t).strip().lstrip("#") for t in fm_tags if str(t).strip()]
    else:
        fm_tags = []

    # Merge body tags into the tag set.
    inline = _extract_inline_tags(body_text)
    seen = set()
    tags: list[str] = []
    for t in fm_tags + inline:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        tags.append(t)

    source_type = str(fm.get("source_type", "unknown")).lower()
    domain = str(fm.get("domain", "unknown")).lower()
    trust_tier = _normalize_trust_tier(fm.get("trust_tier", fm.get("tier")))
    summary = str(fm.get("summary", "") or "").strip()
    aliases = _normalize_list(fm.get("aliases"))
    key_concepts = _normalize_list(fm.get("key_concepts", fm.get("concepts")))
    canonical_for = _normalize_list(fm.get("canonical_for"))
    canonical_questions = _normalize_list(fm.get("canonical_questions", fm.get("questions")))
    last_reviewed = str(fm.get("last_reviewed", "") or "").strip()
    freshness_status = str(fm.get("freshness_status", "") or "").strip().lower()
    rel_parts = Path(rel).parts
    folder = rel_parts[0] if len(rel_parts) > 1 else ""

    mtime = path.stat().st_mtime
    did = _doc_id(rel)

    h1_match = re.search(r"^#\s+(.+)$", body_text, re.MULTILINE)
    title = fm_title or (h1_match.group(1).strip() if h1_match else path.stem)
    retrieval_hints = []
    if summary:
        retrieval_hints.append(f"Summary: {summary}")
    if aliases:
        retrieval_hints.append(f"Aliases: {', '.join(aliases)}")
    if key_concepts:
        retrieval_hints.append(f"Key concepts: {', '.join(key_concepts)}")
    if canonical_for:
        retrieval_hints.append(f"Canonical for: {', '.join(canonical_for)}")
    if canonical_questions:
        retrieval_hints.append(f"Canonical questions: {'; '.join(canonical_questions)}")
    hint_text = _trim_to_tokens("\n".join(retrieval_hints), max(80, hard_max // 4))

    chunks: list[Chunk] = []
    ordinal = 0
    for sec in _walk_sections(body_text):
        body = sec.body()
        if not body:
            continue
        heading_path = " > ".join(h[1] for h in sec.headings) or title
        text_parts = [heading_path]
        if hint_text:
            text_parts.append(hint_text)
        prefix = "\n\n".join(text_parts).strip()
        prefix_tokens = _tok_count(prefix)
        piece_target = max(80, target - prefix_tokens)
        piece_hard_max = max(piece_target, hard_max - prefix_tokens)
        piece_overlap = min(overlap, max(0, piece_hard_max - piece_target))
        for piece in _split_by_tokens(
            body, piece_target, piece_hard_max, piece_overlap
        ):
            text_parts = [prefix]
            text_parts.append(piece)
            text = "\n\n".join(text_parts).strip()
            chunks.append(
                Chunk(
                    doc_id=did,
                    chunk_id=f"{did}:{ordinal:04d}",
                    path=rel,
                    title=title,
                    heading_path=heading_path,
                    tags=list(tags),
                    source_type=source_type,
                    domain=domain,
                    trust_tier=trust_tier,
                    folder=folder,
                    summary=summary,
                    aliases=list(aliases),
                    key_concepts=list(key_concepts),
                    canonical_for=list(canonical_for),
                    canonical_questions=list(canonical_questions),
                    last_reviewed=last_reviewed,
                    freshness_status=freshness_status,
                    text=text,
                    n_tokens=_tok_count(text),
                    mtime=mtime,
                )
            )
            ordinal += 1

    return chunks


def walk_kb(kb_root: Path) -> Iterator[Path]:
    """All markdown files under kb_root. Skips dotfiles and common Obsidian noise."""
    skip_dirs = {
        ".git", ".obsidian", ".trash", "node_modules",
        "_attachments", "attachments", "templates",  # common Obsidian conventions
    }
    for p in kb_root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".md", ".markdown", ".mdx"}:
            continue
        parts = p.relative_to(kb_root).parts[:-1]
        if any(part in skip_dirs or part.startswith(".") for part in parts):
            continue
        yield p
