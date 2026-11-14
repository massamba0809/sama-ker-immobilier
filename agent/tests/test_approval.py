"""Validation humaine : bloquante, liée aux paramètres, reprise, refus."""
import json

from langchain_core.messages import ToolMessage

from sama_agent.models import Decision

from .conftest import email
from .fakes import say, tool_call

DRAFT = {"thread_id": "t1", "destinataire": "client@example.com", "objet": "Re: F3", "corps": "Bonjour, voici un bien."}
VISIT = {"bien_id": "MOCK-001", "debut": "2026-11-19T16:00:00+00:00", "client_email": "client@example.com"}


def last_tool_msg(llm):
    return next(m for m in reversed(llm.seen[-1]) if isinstance(m, ToolMessage))


async def test_send_is_blocked_until_approval_and_nothing_sent(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("Envoyé."),
    ])
    res = await agent.handle_email(email())
    assert res.status == "pending_approval"
    assert world.sent == []  # rien n'est parti
    (req,) = res.pending
    assert req.tool == "envoyer_brouillon" and "Bonjour, voici un bien." in req.preview  # le conseiller voit le texte
    assert len(world.drafts) == 1  # le brouillon, lui, existe


async def test_send_after_approval_resumes_and_sends(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("Réponse envoyée."),
    ])
    res = await agent.handle_email(email())
    res = await agent.resume("t1", {"c2": Decision(approved=True)})
    assert res.status == "done" and len(world.sent) == 1
    assert [e["tool"] for e in res.effects] == ["envoyer_brouillon"]
    assert json.loads(last_tool_msg(llm).content)["ok"] is True


async def test_send_refused_by_advisor_is_not_executed_and_llm_is_told(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("Non envoyé."),
    ])
    await agent.handle_email(email())
    res = await agent.resume("t1", {"c2": Decision(approved=False, reason="ton trop familier")})
    assert world.sent == [] and res.effects == []
    body = json.loads(last_tool_msg(llm).content)
    assert body["ok"] is False and "REFUSÉ" in body["error"] and "familier" in body["error"]


async def test_missing_decision_counts_as_refusal(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("ok"),
    ])
    await agent.handle_email(email())
    await agent.resume("t1", {})
    assert world.sent == []


async def test_visit_is_blocked_until_approval(make_agent):
    agent, llm, world = await make_agent([tool_call("creer_visite", VISIT, "c1"), say("Visite créée.")])
    res = await agent.handle_email(email())
    assert res.status == "pending_approval" and world.visits == {}
    res = await agent.resume("t1", {"c1": Decision(approved=True)})
    assert len(world.visits) == 1 and res.status == "done"


async def test_visit_refused_creates_nothing(make_agent):
    agent, llm, world = await make_agent([tool_call("creer_visite", VISIT, "c1"), say("ok")])
    await agent.handle_email(email())
    await agent.resume("t1", {"c1": Decision(approved=False)})
    assert world.visits == {}


async def test_move_and_cancel_also_require_approval(make_agent):
    agent, llm, world = await make_agent([
        tool_call("annuler_visite", {"event_id": "MOCK-EVT-1"}, "c1"), say("ok")])
    res = await agent.handle_email(email())
    assert res.status == "pending_approval" and res.pending[0].tool == "annuler_visite"


async def test_changed_draft_after_approval_requires_new_validation(make_agent):
    """L'accord porte sur le contenu affiché : si le brouillon change ensuite, l'envoi est refusé."""
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("ok"),
    ])
    await agent.handle_email(email())
    world.drafts["MOCK-DRAFT-1"]["body"] = "TEXTE MODIFIÉ APRÈS VALIDATION"
    await agent.resume("t1", {"c2": Decision(approved=True)})
    assert world.sent == []
    assert "paramètres ont changé" in json.loads(last_tool_msg(llm).content)["error"]


async def test_approval_for_one_call_does_not_cover_another(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("ok"),
    ])
    await agent.handle_email(email())
    await agent.resume("t1", {"autre-id": Decision(approved=True)})
    assert world.sent == []


async def test_confirmed_write_is_never_replayed(make_agent):
    agent, llm, world = await make_agent([
        tool_call("creer_visite", VISIT, "c1"),
        tool_call("creer_visite", VISIT, "c2"),  # le modèle redemande exactement la même visite
        say("ok"),
    ])
    await agent.handle_email(email())
    res = await agent.resume("t1", {"c1": Decision(approved=True)})
    assert res.status == "done" and not res.pending  # aucun nouvel accord réclamé pour une action déjà faite
    assert len(world.visits) == 1  # pas de doublon
    assert "déjà effectué" in last_tool_msg(llm).content


async def test_resume_without_pending_raises(make_agent):
    agent, _, _ = await make_agent([say("ok")])
    await agent.handle_email(email())
    try:
        await agent.resume("t1", {})
    except ValueError:
        return
    raise AssertionError("resume sans validation en attente aurait dû échouer")
