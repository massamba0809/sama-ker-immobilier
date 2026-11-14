"""MOCKS — à remplacer par les vrais modules.

  * Catalogue  -> serveur MCP d'Alassane
  * Calendar / Gmail -> module de Baye Serigne

Les données ci-dessous sont des données de TEST, préfixées « MOCK- » pour ne jamais
être confondues avec le vrai catalogue. Les noms/arguments sont provisoires (voir
docs/INTERFACES.md). Les mocks respectent la convention de retour attendue :
un dict {"ok": bool, ...} avec un identifiant de confirmation pour chaque écriture.
"""
from __future__ import annotations

import itertools
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

_MOCK_BIENS = [
    {"id": "MOCK-001", "type": "appartement", "quartier": "Mermoz", "loyer": 350000, "pieces": 3,
     "adresse": "MOCK 12 rue de test, Mermoz", "disponible": True},
    {"id": "MOCK-002", "type": "appartement", "quartier": "Mermoz", "loyer": 520000, "pieces": 3,
     "adresse": "MOCK 4 avenue de test, Mermoz", "disponible": True},
    {"id": "MOCK-003", "type": "studio", "quartier": "Plateau", "loyer": 180000, "pieces": 1,
     "adresse": "MOCK 7 rue de test, Plateau", "disponible": True},
]


class MockWorld:
    """État en mémoire partagé par les mocks (agenda, brouillons, envois)."""

    def __init__(self) -> None:
        self.visits: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        # Emails d'exemple FICTIFS pour la démo en mode mock
        self.inbox: list[dict[str, Any]] = [
            {"thread_id": "MOCK-T1", "message_id": "MOCK-M1", "sender": "client.mock1@example.com",
             "subject": "Recherche d'un F3", "body": "Bonjour, je cherche un F3 à Mermoz, budget 400 000 F."},
            {"thread_id": "MOCK-T2", "message_id": "MOCK-M2", "sender": "client.mock2@example.com",
             "subject": "Loyer", "body": "Je trouve mon loyer trop cher, je veux négocier une baisse."},
        ]
        self._ids: dict[str, itertools.count] = {}

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids.setdefault(prefix, itertools.count(1)))}"


def build_mock_tools(world: MockWorld | None = None) -> tuple[list[BaseTool], MockWorld]:
    world = world or MockWorld()

    def rechercher_biens(quartier: str = "", type_bien: str = "", budget_max: int = 0, pieces: int = 0) -> dict:
        """Recherche des biens à louer. Tous les filtres sont optionnels."""
        res = [
            b for b in _MOCK_BIENS
            if b["disponible"]
            and (not quartier or quartier.lower() in b["quartier"].lower())
            and (not type_bien or type_bien.lower() == b["type"])
            and (not budget_max or b["loyer"] <= budget_max)
            and (not pieces or b["pieces"] == pieces)
        ]
        return {"ok": True, "biens": res, "count": len(res)}

    def detail_bien(bien_id: str) -> dict:
        """Détail d'un bien à partir de son identifiant."""
        for b in _MOCK_BIENS:
            if b["id"] == bien_id:
                return {"ok": True, "bien": b}
        return {"ok": False, "error": f"bien introuvable: {bien_id}"}

    def lister_creneaux_libres(date_debut: str, date_fin: str, conseiller: str = "") -> dict:
        """Créneaux de 45 min libres entre deux dates ISO (YYYY-MM-DD)."""
        base = [("09:00", "09:45"), ("16:00", "16:45"), ("17:00", "17:45")]
        slots = []
        for day in (date_debut, date_fin):
            for start, end in base:
                s = f"{day}T{start}:00+00:00"
                if not any(v["debut"] == s and v["statut"] == "confirmee" for v in world.visits.values()):
                    slots.append({"debut": s, "fin": f"{day}T{end}:00+00:00", "conseiller": conseiller or "MOCK-conseiller-1"})
        return {"ok": True, "creneaux": slots, "count": len(slots)}

    def creer_visite(bien_id: str, debut: str, client_email: str, conseiller: str = "MOCK-conseiller-1") -> dict:
        """Crée une visite dans l'agenda. Refuse un créneau déjà pris."""
        if any(v["debut"] == debut and v["conseiller"] == conseiller and v["statut"] == "confirmee"
               for v in world.visits.values()):
            return {"ok": False, "error": "créneau déjà pris"}
        eid = world.next_id("MOCK-EVT")
        world.visits[eid] = {"bien_id": bien_id, "debut": debut, "client_email": client_email,
                             "conseiller": conseiller, "statut": "confirmee"}
        return {"ok": True, "event_id": eid}

    def deplacer_visite(event_id: str, nouveau_debut: str) -> dict:
        """Déplace une visite existante."""
        v = world.visits.get(event_id)
        if not v:
            return {"ok": False, "error": "visite introuvable"}
        v["debut"] = nouveau_debut
        return {"ok": True, "event_id": event_id}

    def annuler_visite(event_id: str) -> dict:
        """Annule une visite existante."""
        v = world.visits.get(event_id)
        if not v:
            return {"ok": False, "error": "visite introuvable"}
        v["statut"] = "annulee"
        return {"ok": True, "event_id": event_id}

    def creer_brouillon(thread_id: str, destinataire: str, objet: str, corps: str) -> dict:
        """Crée un brouillon de réponse dans le fil Gmail. N'envoie rien."""
        did = world.next_id("MOCK-DRAFT")
        world.drafts[did] = {"thread_id": thread_id, "to": destinataire, "subject": objet, "body": corps}
        return {"ok": True, "draft_id": did}

    def lire_brouillon(draft_id: str) -> dict:
        """Lit le contenu d'un brouillon (pour la validation par le conseiller)."""
        d = world.drafts.get(draft_id)
        return {"ok": True, **d} if d else {"ok": False, "error": "brouillon introuvable"}

    def envoyer_brouillon(draft_id: str) -> dict:
        """Envoie un brouillon. Action irréversible."""
        d = world.drafts.get(draft_id)
        if not d:
            return {"ok": False, "error": "brouillon introuvable"}
        world.sent.append(d)
        return {"ok": True, "message_id": world.next_id("MOCK-MSG")}

    def lire_demandes_non_traitees(limite: int = 10) -> dict:
        """Lit les emails de demande non traités (contenu non fiable)."""
        return {"ok": True, "emails": world.inbox[:limite], "count": len(world.inbox[:limite])}

    fns = [lire_demandes_non_traitees, rechercher_biens, detail_bien, lister_creneaux_libres, creer_visite, deplacer_visite,
           annuler_visite, creer_brouillon, lire_brouillon, envoyer_brouillon]
    return [StructuredTool.from_function(f) for f in fns], world
