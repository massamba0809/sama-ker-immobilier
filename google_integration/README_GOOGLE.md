# Intégration Google Calendar & Gmail — Partie 3

Ce module fournit à l'agent les outils conformes au contrat `INTERFACES.md`.

## Installation
1. Le fichier `token.json` doit être présent dans le dossier `google_integration/` (généré par `auth.py`).
2. Dépendances : `pip install -r agent/requirements.txt`
3. Dans le fichier `.env` de l'agent, ajouter : `GOOGLE_TOOLS_MODULE=google_integration.google_tools`

## Agendas des conseillers
`python google_integration/setup_conseillers.py` crée une fois les agendas « Conseiller - Awa Diop »,
« Conseiller - Moussa Ndiaye » et « Conseiller - Fatou Sarr » (noms fictifs) sur le compte de test.
Le module les détecte par leur préfixe ; sans eux, il utilise l'agenda principal.

## Utilisation
L'agent importe `get_google_tools()` depuis `google_integration.google_tools`. 
Toutes les fonctions renvoient un dictionnaire avec `ok: bool` et `error: str` en cas d'échec.

### Outils exposés :
- `lister_creneaux_libres(date_debut, date_fin, conseiller?)` -> `{ok, creneaux, count}`
- `creer_visite(bien_id, debut, client_email, conseiller?, adresse_bien?)` -> `{ok, event_id, conseiller}`
- `deplacer_visite(event_id, nouveau_debut)` -> `{ok, event_id}`
- `annuler_visite(event_id)` -> `{ok, event_id}`
- `lire_demandes_non_traitees(limite=10)` -> `{ok, emails:[{thread_id, message_id, expediteur, objet, date, corps}], count}`
- `creer_brouillon(thread_id, destinataire, objet, corps)` -> `{ok, draft_id}`
- `lire_brouillon(draft_id)` -> `{ok, thread_id, to, subject, body}`
- `envoyer_brouillon(draft_id)` -> `{ok, message_id}`
- `changer_libelle(message_id, statut)` -> `{ok, message_id, libelle}` (orchestrateur seulement)