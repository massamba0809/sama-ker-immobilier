"""Façade de l'agent : réception d'un email, suspension en attente de validation, reprise.

L'état de chaque conversation est indexé par le `thread_id` Gmail et persisté par le
checkpointer (SQLite en production, mémoire en test) : un second email du même fil
retrouve l'historique, y compris une validation encore en attente.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import datetime
from typing import Awaitable, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .config import Settings
from .graph import build_graph, text_of
from .llm import build_llm
from .models import ApprovalRequest, Decision, InboundEmail, RunResult
from .prompts import render_email
from .graph import normalize_result
from .security import detect_injection
from .store import StateIndex
from .tools.providers import load_tools
from .tracing import Tracer

Decider = Callable[[list[ApprovalRequest]], Awaitable[dict[str, Decision]]]


def _pick(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if d.get(k):
            return str(d[k])
    return default


def parse_inbox(raw: dict) -> list[InboundEmail]:
    """Convertit le retour de `lire_demandes_non_traitees` en emails. Tolérant sur les noms de champs
    (le format exact sera fixé avec Baye Serigne)."""
    items = raw.get("emails") or raw.get("messages") or raw.get("demandes") or raw.get("data") or []
    out: list[InboundEmail] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        thread = _pick(it, "thread_id", "threadId", "fil")
        body = _pick(it, "body", "corps", "text", "snippet")
        if not thread or not body:
            continue  # email inexploitable : ignoré plutôt qu'inventé
        out.append(InboundEmail(thread_id=thread, message_id=_pick(it, "message_id", "id"),
                                sender=_pick(it, "sender", "expediteur", "from", default="inconnu"),
                                subject=_pick(it, "subject", "objet"), body=body,
                                received_at=_pick(it, "received_at", "date")))
    return out


class SamaAgent:
    def __init__(self, graph, tracer: Tracer, tools: list[BaseTool], stack: AsyncExitStack | None = None):
        self.graph = graph
        self.tracer = tracer
        self.tools = {t.name: t for t in tools}
        self._stack = stack

    # ------------------------------------------------------------ construction
    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        llm: BaseChatModel | None = None,
        tools: list[BaseTool] | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        persistent: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ) -> "SamaAgent":
        settings = settings or Settings()
        stack = AsyncExitStack()
        if checkpointer is None:
            if persistent:
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                checkpointer = await stack.enter_async_context(
                    AsyncSqliteSaver.from_conn_string(str(settings.data_path / "state.db")))
            else:
                checkpointer = InMemorySaver()
        if tools is None:
            tools, _ = await load_tools(settings)
        tracer = Tracer(settings.trace_path)
        graph = build_graph(llm or build_llm(settings), tools, settings, tracer, checkpointer, now_fn)
        agent = cls(graph, tracer, tools, stack)
        agent.settings = settings
        agent.index = StateIndex(settings.data_path / "index.db")
        return agent

    async def aclose(self) -> None:
        if self._stack:
            await self._stack.aclose()

    # ------------------------------------------------------------------ public
    @staticmethod
    def _cfg(thread_id: str, settings: Settings) -> dict:
        # recursion_limit : filet de sécurité de dernier recours, en plus de MAX_STEPS
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": settings.max_steps * 4 + 10}

    async def handle_email(self, email: InboundEmail) -> RunResult:
        t = email.thread_id
        if self.index.is_processed(email.message_id):
            self.tracer.event(t, "email_skipped", message_id=email.message_id, reason="déjà traité")
            return RunResult(thread_id=t, status="skipped", final_text="Email déjà traité.")
        self.tracer.event(t, "email_received", sender=email.sender, subject=email.subject, message_id=email.message_id)

        if await self.has_pending(t):
            # une validation est en attente : on n'empile pas de nouveau traitement dessus
            # (non marqué « traité » : l'email sera repris une fois la validation réglée)
            self.tracer.event(t, "email_deferred", reason="validation en attente sur ce fil")
            result = await self._result(t, 0)
            result.deferred = True  # libellé inchangé : reste « à traiter »
            return result

        suspicious = detect_injection(f"{email.subject}\n{email.body}")
        if suspicious:
            result = await self._escalate_without_llm(email, suspicious)
            self.index.mark_processed(email.message_id, t)
            await self._sync_labels(result, [email.message_id])
            return result

        before = await self._effects_count(t)
        state_in = {"messages": [HumanMessage(content=render_email(email))], "steps": 0,
                    "approvals": {}, "call_counts": {}, "outcome": None}
        await self.graph.ainvoke(state_in, self._cfg(t, self.settings))
        result = await self._result(t, before)
        if result.status not in ("llm_error", "max_steps"):  # ces cas-là restent à retraiter
            self.index.mark_processed(email.message_id, t)
        else:
            self.index.mark_processed("", t)  # le fil est connu, pas l'email
        await self._sync_labels(result, [email.message_id])
        return result

    async def process(self, email: InboundEmail, decide: Decider) -> RunResult:
        """Traite un email jusqu'au bout ; chaque action sensible est soumise à `decide` (conseiller)."""
        result = await self.handle_email(email)
        while result.status == "pending_approval":
            result = await self.resume(result.thread_id, await decide(result.pending))
        return result

    async def fetch_inbox(self) -> list[InboundEmail]:
        """Lit les demandes non traitées via l'outil Gmail (appelé par le code, jamais par le modèle)."""
        tool = self.tools.get("lire_demandes_non_traitees")
        if tool is None:
            raise RuntimeError("Outil lire_demandes_non_traitees indisponible.")
        raw = normalize_result(await tool.ainvoke({}))
        if not raw.get("ok"):
            raise RuntimeError(f"Lecture de la boîte impossible: {raw.get('error')}")
        return parse_inbox(raw)

    async def process_inbox(self, decide: Decider) -> list[RunResult]:
        results = []
        for email in await self.fetch_inbox():
            results.append(await self.process(email, decide))
        return results

    async def pending_threads(self) -> dict[str, list[ApprovalRequest]]:
        """Validations en attente, y compris celles laissées par une exécution précédente."""
        out = {}
        for t in self.index.threads():
            p = await self.pending(t)
            if p:
                out[t] = p
        return out

    async def resume(self, thread_id: str, decisions: dict[str, Decision]) -> RunResult:
        """Reprend une exécution suspendue. `decisions` : call_id -> Decision (absent = refusé)."""
        shown = {r.call_id: r for r in await self.pending(thread_id)}
        if not shown:
            raise ValueError(f"Aucune validation en attente pour le fil {thread_id}")
        before = await self._effects_count(thread_id)
        # L'accord est lié à l'empreinte de ce que le conseiller a vu (action + paramètres + aperçu).
        payload = {cid: {**d.model_dump(), "fingerprint": shown[cid].fingerprint}
                   for cid, d in decisions.items() if cid in shown}
        await self.graph.ainvoke(Command(resume=payload), self._cfg(thread_id, self.settings))
        result = await self._result(thread_id, before)
        await self._sync_labels(result, self.index.messages_for_thread(thread_id))
        return result

    async def pending(self, thread_id: str) -> list[ApprovalRequest]:
        snap = await self.graph.aget_state(self._cfg(thread_id, self.settings))
        out: list[ApprovalRequest] = []
        for task in snap.tasks:
            for intr in task.interrupts:
                out.extend(ApprovalRequest.model_validate(r) for r in intr.value)
        return out

    async def has_pending(self, thread_id: str) -> bool:
        return bool(await self.pending(thread_id))

    # ---------------------------------------------------------------- internes
    @staticmethod
    def label_for(result: RunResult) -> str | None:
        """Libellé de suivi Gmail correspondant à l'issue d'un traitement (None = ne pas toucher)."""
        if result.deferred:
            return None
        if result.status == "pending_approval":
            return "en_attente_validation"
        if result.status == "escalated":
            return "transmis"
        if result.status == "done":
            sent = any(e["tool"] == "envoyer_brouillon" for e in result.effects)
            # terminé sans envoi (ex. envoi refusé par le conseiller) : reste « à traiter » pour un humain
            return "repondu" if sent else "a_traiter"
        return None  # llm_error / max_steps : reste « à traiter », sera retraité ; skipped : déjà fait

    async def _sync_labels(self, result: RunResult, message_ids: list[str]) -> None:
        """Met à jour le libellé Gmail. Ne fait jamais échouer le traitement (erreur tracée seulement)."""
        statut = self.label_for(result)
        tool = self.tools.get("changer_libelle")
        if not statut or tool is None:
            return
        for mid in filter(None, message_ids):
            try:
                res = normalize_result(await tool.ainvoke({"message_id": mid, "statut": statut}))
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)}
            self.tracer.event(result.thread_id, "label_updated" if res.get("ok") else "label_error",
                              message_id=mid, statut=statut, **({} if res.get("ok") else {"error": res.get("error")}))

    async def _effects_count(self, thread_id: str) -> int:
        snap = await self.graph.aget_state(self._cfg(thread_id, self.settings))
        return len((snap.values or {}).get("effects", []))

    async def _result(self, thread_id: str, effects_before: int) -> RunResult:
        snap = await self.graph.aget_state(self._cfg(thread_id, self.settings))
        values = snap.values or {}
        pending = await self.pending(thread_id)
        effects = values.get("effects", [])[effects_before:]
        outcome = values.get("outcome")
        last_ai = next((m for m in reversed(values.get("messages", [])) if isinstance(m, AIMessage)), None)
        final = text_of(last_ai) if last_ai else ""
        if pending:
            status = "pending_approval"
            for r in pending:
                self.tracer.event(thread_id, "approval_requested", call_id=r.call_id, tool=r.tool,
                                  args=r.args, preview=r.preview)
        elif outcome in ("max_steps", "llm_error"):
            status = outcome
        elif any(e["tool"] == "transmettre_a_conseiller" for e in effects):
            status = "escalated"
        else:
            status = "done"
        result = RunResult(thread_id=thread_id, status=status, final_text=final, pending=pending,
                           effects=[{k: e[k] for k in ("tool", "args", "result", "risk")} for e in effects])
        self.tracer.event(thread_id, "run_result", status=status, n_effects=len(effects))
        return result

    async def _escalate_without_llm(self, email: InboundEmail, patterns: list[str]) -> RunResult:
        """Email qui tente de modifier les consignes : transmis à un humain, sans passer par le modèle."""
        t = email.thread_id
        self.tracer.event(t, "injection_detected", patterns=patterns)
        tool = self.tools["transmettre_a_conseiller"]
        res = await tool.ainvoke({"thread_id": t, "motif": "Email contenant des instructions suspectes (tentative d'injection)"})
        self.tracer.event(t, "tool_result", tool="transmettre_a_conseiller", result=res)
        eff = [{"tool": "transmettre_a_conseiller", "args": {"thread_id": t}, "result": res, "risk": "prepare"}]
        result = RunResult(thread_id=t, status="escalated",
                           final_text="Demande hors périmètre transmise à un conseiller.", effects=eff)
        self.tracer.event(t, "run_result", status="escalated", n_effects=1)
        return result
