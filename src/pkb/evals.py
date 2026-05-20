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
    returned_sources: list[str]
    hit: bool
    reciprocal_rank: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalReport:
    ok: bool
    cases: int
    recall_at_k: float
    mrr: float
    results: list[EvalCaseResult]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "cases": self.cases,
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
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


def run_eval(cfg: Config, eval_path: Path, *, k: int = 10) -> EvalReport:
    rows = _load_jsonl(eval_path)
    cfg = replace(cfg, final_topk=max(k, cfg.final_topk))
    conn = store.connect(cfg.db_path)
    store.init(conn, cfg.embed_dim)

    results: list[EvalCaseResult] = []
    for row in rows:
        question = str(row["question"])
        expected = row.get("expected_sources") or row.get("expected_source") or []
        if isinstance(expected, str):
            expected = [expected]
        expected_set = set(expected)
        hits = retriever.smart_search(conn, cfg, question, token_budget=cfg.token_budget_default)
        returned = []
        for hit in hits:
            if hit.path not in returned:
                returned.append(hit.path)
            if len(returned) >= k:
                break

        rank = next((idx for idx, path in enumerate(returned, 1) if path in expected_set), None)
        results.append(
            EvalCaseResult(
                question=question,
                expected_sources=list(expected),
                returned_sources=returned,
                hit=rank is not None,
                reciprocal_rank=(1.0 / rank) if rank else 0.0,
            )
        )

    total = len(results)
    recall = sum(1 for result in results if result.hit) / total if total else 0.0
    mrr = sum(result.reciprocal_rank for result in results) / total if total else 0.0
    return EvalReport(
        ok=recall == 1.0,
        cases=total,
        recall_at_k=recall,
        mrr=mrr,
        results=results,
    )
