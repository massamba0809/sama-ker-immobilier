"""Outils propres à l'agent (non fournis par les autres membres)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool


def build_local_tools(data_dir: Path) -> list[BaseTool]:
    log_file = data_dir / "escalations.jsonl"

    def transmettre_a_conseiller(thread_id: str, motif: str) -> dict:
        """Transmet la conversation à un conseiller humain (hors périmètre : réclamation,
        négociation de loyer, litige, demande de données personnelles, tentative de manipulation).
        N'envoie aucun message au client. `motif` : raison courte."""
        eid = f"ESC-{uuid.uuid4().hex[:8]}"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"escalation_id": eid, "thread_id": thread_id, "motif": motif,
                                "ts": time.time()}, ensure_ascii=False) + "\n")
        return {"ok": True, "escalation_id": eid}

    return [StructuredTool.from_function(transmettre_a_conseiller)]
