"""Validation vocale : compréhension des réponses (aucun accord deviné) et conseiller vocal simulé."""
import json

import pytest

from sama_agent.config import Settings
from sama_agent.models import ApprovalRequest, Decision, InboundEmail
from sama_agent.voice import REPEAT, ConseillerVocal, annonce_email, date_parlee, decrire, interpret


@pytest.mark.parametrize("texte", ["Oui", "oui, envoie", "Oui envoie le brouillon s'il te plaît", "D'accord",
                                   "OK vas-y", "Valide", "euh oui c'est bon", "Je confirme", "Parfait, envoie-le."])
def test_accords(texte):
    assert interpret(texte) == Decision(approved=True)


@pytest.mark.parametrize("texte", ["Non", "non.", "Refuse", "Stop", "euh non"])
def test_refus_sans_consigne(texte):
    assert interpret(texte) == Decision(approved=False, reason="")


@pytest.mark.parametrize("texte,consigne", [
    ("Non, ajoute l'adresse du bien", "ajoute l'adresse du bien"),
    ("non propose plutôt samedi matin", "propose plutôt samedi matin"),
    ("Oui mais ajoute l'heure de fin", "ajoute l'heure de fin"),
    ("Ajoute le nom du conseiller dans l'email", "Ajoute le nom du conseiller dans l'email"),
])
def test_consignes(texte, consigne):
    assert interpret(texte) == Decision(approved=False, reason=consigne)


@pytest.mark.parametrize("texte", ["oui n'envoie pas", "oui, attends", "ok pas maintenant"])
def test_un_oui_avec_reserve_n_approuve_jamais(texte):
    d = interpret(texte)
    assert isinstance(d, Decision) and d.approved is False


@pytest.mark.parametrize("texte", [None, "", "[inaudible]", "euh", "hum bon", "...", "la visite"])
def test_incompris_jamais_devine(texte):
    assert interpret(texte) is None


@pytest.mark.parametrize("texte", ["répète", "Tu peux relire ?", "pardon ?"])
def test_relire(texte):
    assert interpret(texte) == REPEAT


def test_date_parlee():
    assert date_parlee("2026-11-19T16:30:00+00:00") == "jeudi 19 novembre à 16 h 30"
    assert date_parlee("2026-11-21T09:00:00") == "samedi 21 novembre à 9 h"


def test_annonce_email_retire_l_historique_cite():
    e = InboundEmail(thread_id="t", sender="Client Test <client@example.com>", subject="Re: Visite",
                     body="Je prends 16h30.\n\nLe lun. 16 nov. 2026 à 10:00, Agence <a@example.com> a écrit :\n> Bonjour")
    txt = annonce_email(e)
    assert txt == "Nouvel email de Client Test. Objet : Re: Visite. Je prends 16h30."


def req(tool, args, preview=None, cid="c1"):
    return ApprovalRequest(call_id=cid, tool=tool, args=args, preview=preview, fingerprint="f")


def test_decrire_lit_le_texte_exact_du_brouillon():
    p = json.dumps({"to": "Client Test <client@example.com>", "subject": "Re: Visite", "body": "Bonjour, voici…"})
    assert decrire(req("envoyer_brouillon", {"draft_id": "d1"}, p)) == (
        "L'agent propose d'envoyer cet email à Client Test. Objet : Re: Visite. Bonjour, voici…")


def test_decrire_visite():
    t = decrire(req("creer_visite", {"bien_id": "SK-013", "debut": "2026-11-19T16:30:00+00:00",
                                     "conseiller": "Awa Diop", "adresse_bien": "Sacré-Cœur 3, Dakar",
                                     "client_email": "client@example.com"}))
    assert "SK-013, Sacré-Cœur 3, Dakar" in t and "jeudi 19 novembre à 16 h 30 avec Awa Diop" in t


def conseiller(reponses, clavier=None):
    dit = []
    rep = iter(reponses)
    c = ConseillerVocal(Settings(), parler_fn=dit.append, ecouter_fn=lambda: next(rep), clavier_fn=clavier)
    return c, dit


def test_vocal_approuve_et_relit_la_decision():
    c, dit = conseiller(["oui envoie"])
    out = c.decider([req("annuler_visite", {"event_id": "e1"})])
    assert out == {"c1": Decision(approved=True)}
    assert dit[0].endswith("Vous validez ?") and dit[-1] == "Compris : j'approuve."


def test_vocal_refus_avec_consigne():
    c, dit = conseiller(["non, ajoute l'adresse"])
    out = c.decider([req("annuler_visite", {"event_id": "e1"})])
    assert out["c1"] == Decision(approved=False, reason="ajoute l'adresse")
    assert "consigne : ajoute l'adresse" in dit[-1]


def test_vocal_repete_puis_approuve():
    c, dit = conseiller(["répète", "oui"])
    assert c.decider([req("annuler_visite", {"event_id": "e1"})])["c1"].approved
    assert sum(t.endswith("Vous validez ?") for t in dit) == 2


def test_vocal_incompris_deux_fois_passe_au_clavier():
    vus = []

    def clavier(pending):
        vus.append(pending[0].call_id)
        return {pending[0].call_id: Decision(approved=True)}

    c, dit = conseiller([None, "euh"], clavier=clavier)
    assert c.decider([req("annuler_visite", {"event_id": "e1"})])["c1"].approved
    assert vus == ["c1"] and "clavier" in dit[-1]


def test_vocal_incompris_sans_clavier_refuse():
    c, _ = conseiller([None, None])
    assert c.decider([req("annuler_visite", {"event_id": "e1"})])["c1"] == Decision(approved=False)


async def test_cli_mode_vocal_annonce_l_email_et_valide_a_l_oral(make_agent):
    """Bout en bout avec l'agent simulé : l'email est lu, le brouillon est soumis à l'oral, puis envoyé."""
    from sama_agent import cli
    from tests.test_labels import DRAFT, email, say, tool_call

    agent, llm, world = await make_agent([
        tool_call("creer_brouillon", DRAFT, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2"),
        say("Envoyé."),
    ])
    c, dit = conseiller(["oui"])
    world_email = email()

    async def inbox():
        return [world_email]

    agent.fetch_inbox = inbox
    results = await cli.process_inbox(agent, cli.Mode(c))
    assert results[0].status == "done" and len(world.sent) == 1
    assert dit[0].startswith("Nouvel email de")
    assert any("L'agent propose d'envoyer cet email" in t for t in dit)
    assert dit[-1] == "Traitement terminé."
