"""Politique de sécurité des outils : qui est autorisé, avec quel niveau de risque.

C'est le SEUL endroit à modifier quand les noms définitifs des outils d'Alassane
et de Baye Serigne sont connus. Tout outil absent de cette table est REFUSÉ
(liste blanche : refus par défaut).

Niveaux de risque :
  READ     lecture seule, exécutée directement
  PREPARE  prépare quelque chose sans effet réel côté client (brouillon, escalade interne)
  WRITE    effet réel et irréversible (envoi d'email, écriture agenda) -> validation humaine
           bloquante, liée aux paramètres exacts
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    READ = "read"
    PREPARE = "prepare"
    WRITE = "write"


@dataclass(frozen=True)
class ToolPolicy:
    risk: Risk
    # Les résultats contiennent du texte écrit par des tiers (emails) : à encadrer comme donnée non fiable.
    untrusted_output: bool = False
    # False : réservé à l'orchestrateur (code), jamais proposé au modèle.
    llm_visible: bool = True
    # Pour WRITE : clé que le résultat DOIT contenir pour prouver que l'action a eu lieu.
    confirm_key: str | None = None
    # Pour WRITE : outil de lecture (même nom de la table) qui affiche le contenu soumis à validation.
    preview_tool: str | None = None
    preview_arg_map: tuple[tuple[str, str], ...] = ()  # (arg de l'outil WRITE -> arg de l'outil preview)


# NOMS PROVISOIRES : à aligner avec les modules réels (voir docs/INTERFACES.md).
POLICIES: dict[str, ToolPolicy] = {
    # --- Catalogue (Alassane, via MCP) ---
    "rechercher_biens": ToolPolicy(Risk.READ),
    "detail_bien": ToolPolicy(Risk.READ),
    # --- Google Calendar (Baye Serigne) ---
    "lister_creneaux_libres": ToolPolicy(Risk.READ),
    "creer_visite": ToolPolicy(Risk.WRITE, confirm_key="event_id"),
    "deplacer_visite": ToolPolicy(Risk.WRITE, confirm_key="event_id"),
    "annuler_visite": ToolPolicy(Risk.WRITE, confirm_key="event_id"),
    # --- Gmail (Baye Serigne) ---
    "lire_demandes_non_traitees": ToolPolicy(Risk.READ, untrusted_output=True, llm_visible=False),
    "creer_brouillon": ToolPolicy(Risk.PREPARE, confirm_key="draft_id"),
    "lire_brouillon": ToolPolicy(Risk.READ),
    "envoyer_brouillon": ToolPolicy(
        Risk.WRITE,
        confirm_key="message_id",
        preview_tool="lire_brouillon",
        preview_arg_map=(("draft_id", "draft_id"),),
    ),
    # Suivi interne (libellés Gmail) : appelé par l'orchestrateur après chaque traitement, jamais par le modèle.
    "changer_libelle": ToolPolicy(Risk.PREPARE, llm_visible=False, confirm_key="message_id"),
    # --- Local à l'agent ---
    "transmettre_a_conseiller": ToolPolicy(Risk.PREPARE, confirm_key="escalation_id"),
}
