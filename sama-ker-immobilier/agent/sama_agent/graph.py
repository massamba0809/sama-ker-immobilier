"""Boucle de l'agent (LangGraph) : agent -> gate -> execute -> agent ...

  agent    appelle le LLM (avec retries), compte les étapes
  gate     SANS effet de bord : liste les actions WRITE valides et suspend l'exécution
           (interrupt) jusqu'à la décision d'un conseiller
  execute  valide chaque appel (outil connu ? arguments valides ? autorisé ?), exécute,
           vérifie le résultat réellement renvoyé, produit les ToolMessage

Garanties structurelles (indépendantes du comportement du modèle) :
  * un outil absent de policy.POLICIES est refusé ;
  * une action WRITE ne s'exécute que si un accord humain portant sur ses paramètres EXACTS
    (empreinte) existe ; execute revérifie l'accord même si gate est contourné ;
  * une écriture n'est déclarée réussie que si le résultat contient la clé de confirmation ;
  * une écriture identique déjà confirmée n'est jamais rejouée (pas de doublon).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import operator
from datetime import datetime
from typing import Annotated, Any, Callable, TypedDict
from zoneinfo import ZoneInfo

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as json_validate
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel
from pydantic import ValidationError as PydanticError

from .config import Settings
from .models import ApprovalRequest
from .policy import POLICIES, Risk, ToolPolicy
from .prompts import SYSTEM_PROMPT
from .security import wrap_untrusted
from .tracing import Tracer


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    approvals: dict[str, dict]  # call_id -> {approved, reason, fingerprint}
    call_counts: dict[str, int]  # empreinte d'appel -> nb d'occurrences (anti-boucle)
    effects: Annotated[list[dict], operator.add]  # actions confirmées par les outils (durable)
    outcome: str | None  # None | "max_steps" | "llm_error"


def fingerprint(tool: str, args: dict, preview: str | None = None) -> str:
    raw = json.dumps({"tool": tool, "args": args, "preview": preview}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def text_of(msg: AnyMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)


def normalize_result(raw: Any) -> dict:
    """Ramène n'importe quel retour d'outil (dict, str JSON, blocs MCP, liste) à un dict."""
    if isinstance(raw, tuple) and len(raw) == 2:  # (contenu, artefact)
        raw = raw[0]
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if isinstance(raw, list) and raw and all(isinstance(b, dict) and "text" in b for b in raw):
        raw = "".join(b["text"] for b in raw)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {"ok": True, "data": raw, "empty": not raw.strip()}
    if isinstance(raw, dict):
        out = dict(raw)
        out.setdefault("ok", "error" not in out)
        out["empty"] = bool(out.get("count") == 0 or (set(out) <= {"ok", "empty"}))
        return out
    if isinstance(raw, list):
        return {"ok": True, "data": raw, "empty": len(raw) == 0}
    if raw is None:
        return {"ok": True, "data": None, "empty": True}
    return {"ok": True, "data": raw, "empty": False}


def validate_args(tool: BaseTool, args: dict) -> str | None:
    """Retourne un message d'erreur, ou None si les arguments sont valides."""
    schema = tool.args_schema
    try:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema.model_validate(args)
        elif isinstance(schema, dict):
            json_validate(args, schema)
    except (PydanticError, JsonSchemaError) as exc:
        return str(exc).splitlines()[0][:300] if isinstance(exc, JsonSchemaError) else \
            "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:300]
    return None


def build_graph(
    llm: BaseChatModel,
    tools: list[BaseTool],
    settings: Settings,
    tracer: Tracer,
    checkpointer: BaseCheckpointSaver | None = None,
    now_fn: Callable[[], datetime] | None = None,
):
    registry: dict[str, BaseTool] = {t.name: t for t in tools if t.name in POLICIES and POLICIES[t.name].llm_visible}
    allowed = [registry[n] for n in registry]  # seuls les outils listés dans la politique sont exposés au modèle
    llm_tools = llm.bind_tools(allowed)
    tz = ZoneInfo(settings.timezone)
    now = now_fn or (lambda: datetime.now(tz))

    def system_message() -> SystemMessage:
        n = now()
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        return SystemMessage(
            SYSTEM_PROMPT + f"\n# Contexte\nDate actuelle : {jours[n.weekday()]} {n:%d/%m/%Y %H:%M} ({settings.timezone})."
        )

    def tid(config) -> str:
        return config["configurable"]["thread_id"]

    # ------------------------------------------------------------------ agent
    async def agent_node(state: AgentState, config) -> dict:
        t = tid(config)
        steps = state.get("steps", 0)
        if steps >= settings.max_steps:
            tracer.event(t, "max_steps_reached", steps=steps)
            return {"outcome": "max_steps"}
        last_err: Exception | None = None
        for attempt in range(settings.llm_retries + 1):
            try:
                ai: AIMessage = await asyncio.wait_for(
                    llm_tools.ainvoke([system_message(), *state["messages"]]), settings.llm_timeout_seconds)
                if not text_of(ai).strip() and not ai.tool_calls and not ai.invalid_tool_calls:
                    raise ValueError("réponse vide du modèle")
                usage = getattr(ai, "usage_metadata", None) or {}
                tracer.event(t, "llm_response", step=steps + 1, tokens=usage,
                             tool_calls=[{"name": c["name"], "args": c["args"]} for c in ai.tool_calls],
                             text=text_of(ai)[:500])
                return {"messages": [ai], "steps": steps + 1}
            except Exception as exc:  # erreurs fournisseur : quota, réseau, réponse vide...
                last_err = exc
                tracer.event(t, "llm_error", attempt=attempt + 1, error=f"{type(exc).__name__}: {exc}"[:300])
                if attempt < settings.llm_retries:
                    await asyncio.sleep(settings.llm_backoff_seconds * (2 ** attempt))
        tracer.event(t, "llm_failed", error=str(last_err)[:300])
        return {"outcome": "llm_error"}

    def after_agent(state: AgentState) -> str:
        if state.get("outcome"):
            return END
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls):
            return "gate"
        return END

    # --------------------------------------------------------- helpers d'appel
    async def preview_for(name: str, policy: ToolPolicy, args: dict) -> tuple[str | None, str | None]:
        """(aperçu, erreur). L'aperçu montre au conseiller ce qu'il valide (ex. le texte du brouillon)."""
        if not policy.preview_tool:
            return None, None
        ptool = registry.get(policy.preview_tool)
        if ptool is None:
            return None, f"aperçu indisponible (outil {policy.preview_tool} absent)"
        pargs = {dst: args.get(src) for src, dst in policy.preview_arg_map}
        try:
            res = normalize_result(await ptool.ainvoke(pargs))
        except Exception as exc:
            return None, f"aperçu indisponible: {exc}"[:300]
        if not res.get("ok"):
            return None, f"aperçu indisponible: {res.get('error', 'inconnu')}"
        res.pop("ok", None)
        res.pop("empty", None)
        return json.dumps(res, ensure_ascii=False, sort_keys=True), None

    def precheck(call: dict) -> tuple[ToolPolicy | None, str | None]:
        name = call["name"]
        if name not in registry:
            return None, f"Outil inconnu ou non autorisé: {name}"
        err = validate_args(registry[name], call["args"])
        if err:
            return None, f"Arguments invalides pour {name}: {err}"
        return POLICIES[name], None

    # ------------------------------------------------------------------- gate
    async def gate_node(state: AgentState, config) -> dict:
        t = tid(config)
        ai: AIMessage = state["messages"][-1]
        requests: list[ApprovalRequest] = []
        done = {e["fingerprint"] for e in state.get("effects", []) if e.get("risk") == "write"}
        for call in ai.tool_calls[: settings.max_tool_calls_per_turn]:
            policy, err = precheck(call)
            if err or policy is None or policy.risk is not Risk.WRITE:
                continue
            prev, perr = await preview_for(call["name"], policy, call["args"])
            if perr:
                continue  # execute refusera avec ce même motif
            if fingerprint(call["name"], call["args"], prev) in done:
                continue  # déjà effectué et confirmé : on ne redemande pas d'accord, execute ne rejouera pas
            requests.append(ApprovalRequest(call_id=call["id"], tool=call["name"], args=call["args"],
                                            preview=prev, fingerprint=fingerprint(call["name"], call["args"], prev)))
        if not requests:
            return {"approvals": {}}
        # (la demande est tracée par SamaAgent._result : ce nœud est rejoué à la reprise)
        decisions = interrupt([r.model_dump() for r in requests])  # ← suspend jusqu'à la reprise
        approvals: dict[str, dict] = {}
        for r in requests:
            d = (decisions or {}).get(r.call_id) or {}
            # L'empreinte vient de la demande telle que le conseiller l'a VUE (renvoyée par resume()),
            # pas d'un recalcul : sinon un contenu modifié entre-temps serait couvert par l'accord.
            approvals[r.call_id] = {"approved": d.get("approved") is True and bool(d.get("fingerprint")),
                                    "reason": str(d.get("reason", "")), "fingerprint": d.get("fingerprint", "")}
            tracer.event(t, "approval_decision", call_id=r.call_id, tool=r.tool,
                         approved=approvals[r.call_id]["approved"], reason=approvals[r.call_id]["reason"])
        return {"approvals": approvals}

    # ---------------------------------------------------------------- execute
    def envelope(name: str, ok: bool, policy: ToolPolicy | None, payload: Any = None, error: str | None = None) -> str:
        body = {"ok": ok, **({"error": error} if error else {}), **({"result": payload} if payload is not None else {})}
        text = json.dumps(body, ensure_ascii=False, default=str)
        if policy and policy.untrusted_output:
            text = wrap_untrusted(text, "donnee_non_fiable")
        return text

    async def execute_node(state: AgentState, config) -> dict:
        t = tid(config)
        ai: AIMessage = state["messages"][-1]
        approvals = state.get("approvals", {})
        counts = dict(state.get("call_counts", {}))
        prior = {e["fingerprint"]: e for e in state.get("effects", []) if e.get("risk") == "write"}
        out: list[ToolMessage] = []
        new_effects: list[dict] = []

        def reply(call_id: str, name: str, text: str, ok: bool) -> None:
            out.append(ToolMessage(content=text, tool_call_id=call_id, name=name, status="success" if ok else "error"))

        for bad in ai.invalid_tool_calls:
            reply(bad.get("id") or "invalid", bad.get("name") or "inconnu",
                  envelope("?", False, None, error="Appel d'outil mal formé (arguments non décodables)."), False)

        for i, call in enumerate(ai.tool_calls):
            name, args, cid = call["name"], call["args"], call["id"]
            if i >= settings.max_tool_calls_per_turn:
                reply(cid, name, envelope(name, False, None, error="Trop d'appels d'outils dans un même tour."), False)
                continue
            policy, err = precheck(call)
            if err or policy is None:
                tracer.event(t, "tool_rejected", tool=name, args=args, reason=err)
                reply(cid, name, envelope(name, False, None, error=err), False)
                continue

            # anti-boucle : même appel répété
            key = fingerprint(name, args)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= settings.repeat_call_limit and policy.risk is Risk.READ:
                tracer.event(t, "repeat_call_blocked", tool=name, args=args, n=counts[key])
                reply(cid, name, envelope(name, False, policy, error="Appel identique répété trop de fois : "
                                          "change d'approche ou appelle transmettre_a_conseiller."), False)
                continue

            preview: str | None = None
            if policy.risk is Risk.WRITE:
                preview, perr = await preview_for(name, policy, args)
                if perr:
                    tracer.event(t, "tool_rejected", tool=name, args=args, reason=perr)
                    reply(cid, name, envelope(name, False, policy, error=f"REFUSÉ: {perr}"), False)
                    continue
                fp = fingerprint(name, args, preview)
                if fp in prior:  # déjà fait et confirmé : jamais de doublon
                    tracer.event(t, "duplicate_write_skipped", tool=name, args=args)
                    reply(cid, name, envelope(name, True, policy, {**prior[fp]["result"], "note": "déjà effectué, non rejoué"}), True)
                    continue
                appr = approvals.get(cid)
                if not appr or not appr["approved"]:
                    reason = (appr or {}).get("reason") or "aucun accord"
                    tracer.event(t, "write_refused", tool=name, args=args, reason=reason)
                    reply(cid, name, envelope(name, False, policy, error=(
                        f"REFUSÉ: action non autorisée par un conseiller ({reason}). Elle n'a PAS été exécutée.")), False)
                    continue
                if appr["fingerprint"] != fp:
                    tracer.event(t, "write_refused", tool=name, args=args, reason="paramètres modifiés après accord")
                    reply(cid, name, envelope(name, False, policy, error=(
                        "REFUSÉ: les paramètres ont changé depuis l'accord. Nouvelle validation requise.")), False)
                    continue

            tracer.event(t, "tool_call", tool=name, args=args, risk=policy.risk.value)
            try:
                res = normalize_result(await registry[name].ainvoke(args))
            except Exception as exc:
                tracer.event(t, "tool_error", tool=name, error=f"{type(exc).__name__}: {exc}"[:300])
                reply(cid, name, envelope(name, False, policy, error=f"Erreur de l'outil: {exc}"[:300]), False)
                continue
            tracer.event(t, "tool_result", tool=name, result=res)

            ok = bool(res.get("ok"))
            if ok and policy.confirm_key and not res.get(policy.confirm_key):
                ok = False
                res = {**res, "ok": False, "error": f"Succès non confirmé par l'outil (clé '{policy.confirm_key}' absente)."}
                tracer.event(t, "unconfirmed_result", tool=name)
            if ok and policy.confirm_key:
                new_effects.append({"tool": name, "args": args, "result": res, "risk": policy.risk.value,
                                    "fingerprint": fingerprint(name, args, preview)})
            if not ok:
                reply(cid, name, envelope(name, False, policy, error=str(res.get("error", "échec")), payload=res), False)
            else:
                reply(cid, name, envelope(name, True, policy, res), True)
        return {"messages": out, "effects": new_effects, "call_counts": counts, "approvals": {}}

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("gate", gate_node)
    g.add_node("execute", execute_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, {"gate": "gate", END: END})
    g.add_edge("gate", "execute")
    g.add_edge("execute", "agent")
    return g.compile(checkpointer=checkpointer)
