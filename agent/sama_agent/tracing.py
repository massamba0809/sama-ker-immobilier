"""Traces : un fichier JSONL par conversation (thread), un événement par ligne.

Chaque appel d'outil y apparaît avec ses arguments et son résultat, ainsi que les
appels LLM (tokens), les demandes de validation et les décisions humaines.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("sama_agent")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120] or "unknown"


class Tracer:
    def __init__(self, trace_dir: Path):
        self.dir = trace_dir

    def path_for(self, thread_id: str) -> Path:
        return self.dir / f"{_safe(thread_id)}.jsonl"

    def event(self, thread_id: str, kind: str, **data: Any) -> None:
        record = {"ts": round(time.time(), 3), "thread_id": thread_id, "event": kind, **data}
        try:
            with self.path_for(thread_id).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # une trace ne doit jamais casser le traitement
            log.warning("trace non écrite: %s", exc)
        log.info("[%s] %s %s", thread_id, kind, json.dumps(data, ensure_ascii=False, default=str)[:300])

    def read(self, thread_id: str) -> list[dict]:
        p = self.path_for(thread_id)
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
