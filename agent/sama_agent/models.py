"""Structures de données échangées entre l'agent et le reste du système."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class InboundEmail(BaseModel):
    """Email entrant. `thread_id` (fil Gmail) identifie la conversation et son état."""

    thread_id: str
    message_id: str = ""
    sender: str
    subject: str = ""
    body: str
    received_at: str = ""


class ApprovalRequest(BaseModel):
    """Action sensible en attente d'un conseiller. `fingerprint` lie l'accord aux paramètres exacts."""

    call_id: str
    tool: str
    args: dict[str, Any]
    preview: str | None = None
    fingerprint: str


class Decision(BaseModel):
    approved: bool
    reason: str = ""
    # Si le conseiller veut changer les paramètres, il refuse avec une consigne :
    # l'agent re-propose une action modifiée qui sera revalidée.


RunStatus = Literal[
    "done",  # l'agent a terminé (réponse finale sans nouvel appel d'outil)
    "pending_approval",  # exécution suspendue, en attente d'un conseiller
    "escalated",  # transmis à un conseiller humain (hors périmètre / injection)
    "max_steps",  # limite d'étapes atteinte
    "llm_error",  # fournisseur LLM indisponible après retries
    "skipped",  # email déjà traité, ignoré (anti-doublon)
]


class RunResult(BaseModel):
    thread_id: str
    status: RunStatus
    final_text: str = ""
    pending: list[ApprovalRequest] = Field(default_factory=list)
    effects: list[dict[str, Any]] = Field(default_factory=list)  # actions réellement confirmées par les outils
    # Email mis de côté car une validation est déjà en attente sur ce fil (sera repris ensuite)
    deferred: bool = False
