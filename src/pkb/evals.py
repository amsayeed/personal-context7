"""Retrieval evals for hand-written question/source fixtures."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import retriever, store
from .config import Config


@dataclass
class EvalCaseResult:
    question: str
    expected_sources: list[str]
    expected_terms: list[str]
    returned_sources: list[str]
    returned_titles: list[str]
    hit: bool
    reciprocal_rank: float
    source_coverage: float
    expected_terms_found: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalReport:
    ok: bool
    cases: int
    k: int
    recall_at_k: float
    mrr: float
    avg_source_coverage: float
    avg_term_coverage: float
    results: list[EvalCaseResult]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cases": self.cases,
            "k": self.k,
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
            "avg_source_coverage": self.avg_source_coverage,
            "avg_term_coverage": self.avg_term_coverage,
            "results": [result.to_dict() for result in self.results],
        }


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            rows.append(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return rows


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def run_eval(
    cfg: Config,
    eval_path: Path,
    *,
    k: int = 10,
    min_recall: float = 1.0,
    min_mrr: float = 0.0,
) -> EvalReport:
    rows = _load_jsonl(eval_path)
    cfg = replace(cfg, final_topk=max(k, cfg.final_topk))
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)

    results: list[EvalCaseResult] = []
    for row in rows:
        question = str(row["question"])
        expected = _as_list(row.get("expected_sources") or row.get("expected_source"))
        expected_terms = _as_list(row.get("expected_terms") or row.get("expected_term"))
        expected_set = set(expected)
        hits = retriever.smart_search(conn, cfg, question, token_budget=cfg.token_budget_default)
        returned = []
        returned_titles = []
        returned_text = []
        for hit in hits:
            if hit.path not in returned:
                returned.append(hit.path)
                returned_titles.append(hit.title)
            if len(returned) >= k:
                break
        for hit in hits[:k]:
            returned_text.append(f"{hit.title}\n{hit.heading_path}\n{hit.text}".lower())

        rank = next((idx for idx, path in enumerate(returned, 1) if path in expected_set), None)
        found_terms = [
            term for term in expected_terms
            if any(term.lower() in text for text in returned_text)
        ]
        source_coverage = (
            len(set(returned) & expected_set) / len(expected_set)
            if expected_set else 1.0
        )
        results.append(
            EvalCaseResult(
                question=question,
                expected_sources=list(expected),
                expected_terms=expected_terms,
                returned_sources=returned,
                returned_titles=returned_titles,
                hit=rank is not None,
                reciprocal_rank=(1.0 / rank) if rank else 0.0,
                source_coverage=source_coverage,
                expected_terms_found=found_terms,
            )
        )

    total = len(results)
    recall = sum(1 for result in results if result.hit) / total if total else 0.0
    mrr = sum(result.reciprocal_rank for result in results) / total if total else 0.0
    avg_source_coverage = (
        sum(result.source_coverage for result in results) / total if total else 0.0
    )
    term_cases = [result for result in results if result.expected_terms]
    avg_term_coverage = (
        sum(len(result.expected_terms_found) / len(result.expected_terms) for result in term_cases)
        / len(term_cases)
        if term_cases else 1.0
    )
    return EvalReport(
        ok=recall >= min_recall and mrr >= min_mrr,
        cases=total,
        k=k,
        recall_at_k=recall,
        mrr=mrr,
        avg_source_coverage=avg_source_coverage,
        avg_term_coverage=avg_term_coverage,
        results=results,
    )
