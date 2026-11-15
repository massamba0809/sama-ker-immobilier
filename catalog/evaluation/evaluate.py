"""Évalue les requêtes catalogue du jeu d'emails sans modèle externe ni réseau."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from catalog.server import initialize_database, search_properties


CORPUS = Path(__file__).with_name("emails.json")


def _estimated_tokens(value: Any) -> int:
    """Approximation reproductible (caractères JSON / 4), pas une mesure de tokens LLM."""
    return max(1, (len(json.dumps(value, ensure_ascii=False, sort_keys=True)) + 3) // 4)


def evaluate(corpus_path: Path = CORPUS) -> dict[str, Any]:
    cases = json.loads(corpus_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="sama-catalog-eval-") as temp_dir:
        database = Path(temp_dir) / "catalogue.sqlite3"
        initialize_database(database)
        results = []
        total_tokens = 0
        total_seconds = 0.0
        for case in cases:
            started = time.perf_counter()
            result = search_properties(database_path=database, **case["arguments"])
            elapsed = time.perf_counter() - started
            actual_ids = sorted(item["id"] for item in result.get("biens", []))
            expected_ids = sorted(case["biens_attendus"])
            token_estimate = _estimated_tokens({"arguments": case["arguments"], "result": result})
            results.append({
                "id": case["id"],
                "ok": result.get("ok") is True and actual_ids == expected_ids,
                "expected": expected_ids,
                "actual": actual_ids,
                "tool_calls": 1,
                "estimated_tokens": token_estimate,
                "seconds": round(elapsed, 6),
            })
            total_tokens += token_estimate
            total_seconds += elapsed
    successful = sum(item["ok"] for item in results)
    return {
        "success_rate": successful / len(results) if results else 1.0,
        "successful": successful,
        "total": len(results),
        "tool_calls": sum(item["tool_calls"] for item in results),
        "estimated_tokens": total_tokens,
        "seconds": round(total_seconds, 6),
        "cases": results,
    }


def main() -> None:
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["successful"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
