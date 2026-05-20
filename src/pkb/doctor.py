"""KB quality checks that do not require embeddings or a populated database."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import frontmatter

from .chunker import chunk_file, walk_kb
from .config import Config


DEFAULT_DOMAINS = {"data", "ai", "system-design", "arch-patterns", "unknown"}
DEFAULT_SOURCE_TYPES = {"book", "blog", "paper", "adr", "own-note", "transcript", "unknown"}
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


@dataclass
class Issue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DoctorReport:
    ok: bool
    files: int
    issues: list[Issue]
    by_severity: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "files": self.files,
            "by_severity": self.by_severity,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        values = v.split(",")
    elif isinstance(v, list):
        values = v
    else:
        values = [v]
    return [str(item).strip().lstrip("#") for item in values if str(item).strip()]


def _parse_date(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    text = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _tier_rank(v) -> int | None:
    aliases = {
        "archive": 0, "draft": 0, "raw": 0, "0": 0,
        "reference": 1, "highlight": 1, "1": 1,
        "canonical": 2, "source": 2, "2": 2,
        "synthesis": 3, "own": 3, "my-note": 3, "3": 3,
    }
    if isinstance(v, int):
        return v if 0 <= v <= 3 else None
    return aliases.get(str(v).lower())


def _missing_frontmatter(fm: dict, key: str) -> bool:
    if key not in fm or fm[key] is None:
        return True
    return isinstance(fm[key], str) and not fm[key].strip()


def run_doctor(
    cfg: Config,
    *,
    stale_days: int = 180,
    domains: Iterable[str] = DEFAULT_DOMAINS,
    source_types: Iterable[str] = DEFAULT_SOURCE_TYPES,
) -> DoctorReport:
    paths = list(walk_kb(cfg.kb_root))
    issues: list[Issue] = []
    titles: Counter[str] = Counter()
    link_targets: defaultdict[str, list[str]] = defaultdict(list)
    known_names: set[str] = set()
    vocab_path = cfg.kb_root / ".pkb-vocab.json"
    if vocab_path.exists():
        vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
        domains = vocab.get("domains", domains)
        source_types = vocab.get("source_types", source_types)
    domain_set = {d.lower() for d in domains}
    source_type_set = {s.lower() for s in source_types}

    parsed: list[tuple[Path, dict, str, str]] = []
    for path in paths:
        rel = str(path.relative_to(cfg.kb_root).as_posix())
        raw = path.read_text(encoding="utf-8", errors="replace")
        post = frontmatter.loads(raw)
        fm = post.metadata or {}
        title = str(fm.get("title") or path.stem).strip()
        titles[title.lower()] += 1
        rel_no_suffix = str(path.relative_to(cfg.kb_root).with_suffix("").as_posix()).lower()
        rel_with_suffix = str(path.relative_to(cfg.kb_root).as_posix()).lower()
        known_names.add(path.stem.lower())
        known_names.add(rel_no_suffix)
        known_names.add(rel_with_suffix)
        known_names.add(title.lower())
        for alias in _as_list(fm.get("aliases")):
            known_names.add(alias.lower())
        parsed.append((path, fm, title, post.content))

        for match in _WIKILINK_RE.finditer(post.content):
            link_targets[match.group(1).strip().lower()].append(rel)

        for key in ("title", "domain", "source_type", "trust_tier"):
            if _missing_frontmatter(fm, key):
                issues.append(Issue("error", "missing-frontmatter", rel, f"Missing `{key}`."))

        domain = str(fm.get("domain", "unknown")).lower()
        if domain not in domain_set:
            issues.append(Issue("warning", "unknown-domain", rel, f"Unknown domain `{domain}`."))

        source_type = str(fm.get("source_type", "unknown")).lower()
        if source_type not in source_type_set:
            issues.append(
                Issue(
                    "warning", "unknown-source-type", rel,
                    f"Unknown source_type `{source_type}`.",
                )
            )

        tier = fm.get("trust_tier", fm.get("tier", 1))
        tier_rank = _tier_rank(tier)
        if tier_rank is None:
            issues.append(
                Issue("warning", "unknown-trust-tier", rel, f"Unknown trust_tier `{tier}`.")
            )

        if tier_rank is not None and tier_rank >= 2:
            if not fm.get("summary"):
                issues.append(
                    Issue(
                        "warning", "missing-summary", rel,
                        "Canonical notes should have `summary`.",
                    )
                )
            if not _as_list(fm.get("canonical_for")):
                issues.append(
                    Issue(
                        "info",
                        "missing-canonical-for",
                        rel,
                        "Add `canonical_for` for stronger topic resolution.",
                    )
                )

        reviewed = _parse_date(fm.get("last_reviewed"))
        if reviewed and (date.today() - reviewed).days > stale_days:
            issues.append(
                Issue(
                    "info", "stale-review", rel,
                    f"Last reviewed more than {stale_days} days ago.",
                )
            )

        if not post.content.strip():
            issues.append(Issue("error", "empty-note", rel, "Note body is empty."))

        try:
            chunks = chunk_file(path, cfg.kb_root, target=700, hard_max=1200, overlap=120)
        except Exception as exc:
            issues.append(Issue("error", "chunk-failed", rel, f"Could not chunk note: {exc}"))
            continue
        if not chunks:
            issues.append(Issue("error", "no-chunks", rel, "Note produced no chunks."))
        for chunk in chunks:
            if chunk.n_tokens > 1400:
                issues.append(
                    Issue(
                        "warning",
                        "large-chunk",
                        rel,
                        f"Chunk {chunk.chunk_id} has {chunk.n_tokens} tokens.",
                    )
                )

    for title, count in titles.items():
        if count > 1:
            for path, _, original, _ in parsed:
                if original.lower() == title:
                    rel = str(path.relative_to(cfg.kb_root).as_posix())
                    issues.append(
                        Issue(
                            "warning", "duplicate-title", rel,
                            f"Title is duplicated {count} times.",
                        )
                    )

    for target, sources in link_targets.items():
        if target not in known_names:
            for rel in sources:
                issues.append(
                    Issue("info", "broken-wikilink", rel, f"Unresolved wikilink `[[{target}]]`.")
                )

    by_severity = dict(Counter(issue.severity for issue in issues))
    return DoctorReport(
        ok=not any(issue.severity == "error" for issue in issues),
        files=len(paths),
        issues=issues,
        by_severity=by_severity,
    )
