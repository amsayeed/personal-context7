"""Book/document ingestion helpers.

The source of truth stays as editable Markdown front matter in the vault. The
index is a derived cache, so ingestion focuses on making metadata explicit before
selected files are allowed into the KB database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import frontmatter

from .chunker import walk_kb
from .config import Config
from .doctor import DEFAULT_DOMAINS, DEFAULT_SOURCE_TYPES, _tier_rank

REQUIRED_KEYS = ("title", "domain", "source_type", "trust_tier")


@dataclass
class IngestIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnnotateResult:
    path: str
    changed: bool
    added_keys: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def markdown_files(source: Path) -> list[Path]:
    """Return markdown files for a single file or directory."""
    source = source.expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() not in {".md", ".markdown", ".mdx"}:
            raise ValueError(f"not a markdown file: {source}")
        return [source]
    if source.is_dir():
        return sorted(walk_kb(source))
    raise FileNotFoundError(str(source))


def validate_metadata(
    files: Iterable[Path],
    *,
    root: Path,
    domains: Iterable[str] = DEFAULT_DOMAINS,
    source_types: Iterable[str] = DEFAULT_SOURCE_TYPES,
    require_summary_for_tier: int | None = None,
) -> list[IngestIssue]:
    domain_set = {d.lower() for d in domains}
    source_type_set = {s.lower() for s in source_types}
    issues: list[IngestIssue] = []

    for path in files:
        rel = _rel(path, root)
        post = frontmatter.loads(path.read_text(encoding="utf-8", errors="replace"))
        fm = post.metadata or {}

        for key in REQUIRED_KEYS:
            if _missing(fm, key):
                issues.append(
                    IngestIssue("error", "missing-frontmatter", rel, f"Missing `{key}`.")
                )

        domain = str(fm.get("domain", "unknown")).lower()
        if domain not in domain_set:
            issues.append(
                IngestIssue("warning", "unknown-domain", rel, f"Unknown domain `{domain}`.")
            )

        source_type = str(fm.get("source_type", "unknown")).lower()
        if source_type not in source_type_set:
            issues.append(
                IngestIssue(
                    "warning",
                    "unknown-source-type",
                    rel,
                    f"Unknown source_type `{source_type}`.",
                )
            )

        tier = _tier_rank(fm.get("trust_tier", fm.get("tier")))
        if tier is None:
            issues.append(
                IngestIssue(
                    "warning",
                    "unknown-trust-tier",
                    rel,
                    f"Unknown trust_tier `{fm.get('trust_tier', fm.get('tier'))}`.",
                )
            )
        elif require_summary_for_tier is not None and tier >= require_summary_for_tier:
            if _missing(fm, "summary"):
                issues.append(
                    IngestIssue(
                        "error",
                        "missing-summary",
                        rel,
                        f"Missing `summary` for trust_tier >= {require_summary_for_tier}.",
                    )
                )

    return issues


def annotate_files(
    files: Iterable[Path],
    *,
    root: Path,
    domain: str,
    source_type: str,
    trust_tier: int,
    tags: list[str] | None = None,
    collection: str | None = None,
    freshness_status: str = "current",
    last_reviewed: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[AnnotateResult]:
    today = date.today().isoformat()
    review_date = last_reviewed or today
    results: list[AnnotateResult] = []

    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        post = frontmatter.loads(raw)
        fm = dict(post.metadata or {})
        added: list[str] = []

        def set_default(key: str, value) -> None:
            if overwrite or _missing(fm, key):
                if fm.get(key) != value:
                    fm[key] = value
                    added.append(key)

        set_default("title", _title_for(path, fm, post.content))
        set_default("domain", domain)
        set_default("source_type", source_type)
        set_default("trust_tier", int(trust_tier))
        set_default("freshness_status", freshness_status)
        set_default("last_reviewed", review_date)

        if collection:
            set_default("collection", collection)

        if _missing(fm, "summary") and fm.get("description"):
            set_default("summary", str(fm["description"]).strip())

        if tags:
            merged = _merge_tags(fm.get("tags"), tags)
            if overwrite or merged != _normalize_tags(fm.get("tags")):
                fm["tags"] = merged
                added.append("tags")

        changed = bool(added)
        if changed and not dry_run:
            post.metadata = fm
            path.write_text(frontmatter.dumps(post), encoding="utf-8")

        results.append(AnnotateResult(path=_rel(path, root), changed=changed, added_keys=added))

    return results


def ensure_under_kb(files: Iterable[Path], cfg: Config) -> None:
    kb_root = cfg.kb_root.resolve()
    for path in files:
        if not path.resolve().is_relative_to(kb_root):
            raise ValueError(
                f"{path} is outside PKB_KB_ROOT={kb_root}. Move/copy it into the vault first."
            )


def load_vocab(cfg: Config) -> tuple[list[str], list[str]]:
    vocab_path = cfg.kb_root / ".pkb-vocab.json"
    if not vocab_path.exists():
        return sorted(DEFAULT_DOMAINS), sorted(DEFAULT_SOURCE_TYPES)

    import json

    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    return (
        [str(v).lower() for v in vocab.get("domains", DEFAULT_DOMAINS)],
        [str(v).lower() for v in vocab.get("source_types", DEFAULT_SOURCE_TYPES)],
    )


def _missing(fm: dict, key: str) -> bool:
    if key not in fm or fm[key] is None:
        return True
    return isinstance(fm[key], str) and not fm[key].strip()


def _rel(path: Path, root: Path) -> str:
    path = path.resolve()
    root = root.resolve()
    try:
        return str(path.relative_to(root).as_posix())
    except ValueError:
        return str(path)


def _title_for(path: Path, fm: dict, body: str) -> str:
    if fm.get("title"):
        return str(fm["title"]).strip()
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _normalize_tags(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, list):
        raw = value
    else:
        raw = [value]
    return [str(item).strip().lstrip("#") for item in raw if str(item).strip()]


def _merge_tags(existing, additions: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in _normalize_tags(existing) + additions:
        tag = tag.strip().lstrip("#")
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out
