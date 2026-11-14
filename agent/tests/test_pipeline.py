"""Scénario complet : email -> recherche -> créneaux -> brouillon -> validation -> envoi -> confirmation -> visite."""
from sama_agent.models import Decision

from .conftest import email
from .fakes import say, tool_call

SLOT = "2026-11-19T16:00:00+00:00"


async def test_full_workflow_with_human_validations(make_agent):
    script = [
        # --- 1er email : demande ---
        tool_call("rechercher_biens", {"quartier": "Mermoz", "pieces": 3, "budget_max": 400000}, "a1"),
        tool_call("lister_creneaux_libres", {"date_debut": "2026-11-19", "date_fin": "2026-11-20"}, "a2"),
        tool_call("creer_brouillon", {"thread_id": "t1", "destinataire": "client@example.com", "objet": "Re: F3",
                                      "corps": "Nous avons MOCK-001 (350 000 F). Créneaux: 19/11 à 16h."}, "a3"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "a4"),
        say("Réponse envoyée au client."),
        # --- 2e email : le client confirme ---
        tool_call("creer_visite", {"bien_id": "MOCK-001", "debut": SLOT, "client_email": "client@example.com"}, "b1"),
        tool_call("creer_brouillon", {"thread_id": "t1", "destinataire": "client@example.com", "objet": "Visite confirmée",
                                      "corps": "Votre visite est confirmée le 19/11 à 16h."}, "b2"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-2"}, "b3"),
        say("Visite créée et confirmation envoyée."),
    ]
    agent, llm, world = await make_agent(script)

    # Étape 1-6 : compréhension, recherche, créneaux, brouillon, validation requise
    res = await agent.handle_email(email())
    assert res.status == "pending_approval" and world.sent == []

    # Étape 7 : le conseiller approuve l'envoi
    res = await agent.resume("t1", {"a4": Decision(approved=True)})
    assert res.status == "done" and len(world.sent) == 1 and world.visits == {}

    # Étape 8 : le client confirme (même fil) -> la visite attend une 2e validation
    res = await agent.handle_email(email("Le jeudi à 16h me convient parfaitement.", subject="Re: F3"))
    assert res.status == "pending_approval" and res.pending[0].tool == "creer_visite"
    assert world.visits == {}  # pas de réservation avant accord

    # Étape 9-10 : le conseiller approuve la visite, puis l'email de confirmation
    res = await agent.resume("t1", {"b1": Decision(approved=True)})
    assert len(world.visits) == 1 and res.pending[0].tool == "envoyer_brouillon"
    res = await agent.resume("t1", {"b3": Decision(approved=True)})
    assert res.status == "done" and len(world.sent) == 2

    # la conversation a conservé son historique (2e appel LLM voit le 1er échange)
    assert any("Réponse envoyée" in str(m.content) for m in llm.seen[-1])
    events = [e["event"] for e in agent.tracer.read("t1")]
    assert events.count("approval_decision") == 3


async def test_slot_taken_in_between_is_reported_to_the_llm_and_nothing_is_created(make_agent):
    visit = {"bien_id": "MOCK-001", "debut": SLOT, "client_email": "a@b.c"}
    agent, llm, world = await make_agent([
        tool_call("creer_visite", visit, "c1"),
        tool_call("creer_visite", {**visit, "client_email": "autre@b.c"}, "c2"),
        say("Ce créneau n'est plus disponible."),
    ])
    await agent.handle_email(email())
    await agent.resume("t1", {"c1": Decision(approved=True)})
    res = await agent.resume("t1", {"c2": Decision(approved=True)})
    assert len(world.visits) == 1  # pas de doublon
    assert res.effects == []  # la 2e tentative n'est PAS comptée comme réussie


async def test_out_of_scope_goes_to_advisor(make_agent):
    agent, llm, world = await make_agent([
        tool_call("transmettre_a_conseiller", {"thread_id": "t1", "motif": "demande de baisse de loyer"}, "c1"),
        say("Transmis à un conseiller."),
    ])
    res = await agent.handle_email(email("Je veux négocier mon loyer à la baisse."))
    assert res.status == "escalated" and world.sent == []
