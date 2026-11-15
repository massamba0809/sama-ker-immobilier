# Interfaces attendues par l'agent

Contrat à valider avec Alassane (catalogue MCP) et Baye Serigne (Google). Les noms ci-dessous
sont **provisoires** : ils sont déclarés dans `sama_agent/policy.py`. Si un nom réel diffère,
on modifie une ligne dans ce fichier, pas le moteur. **Un outil absent de `policy.py` est refusé.**

## Convention de retour (tous les outils)

Dict (ou JSON) avec `ok: bool`. En cas d'échec : `ok: false` + `error: "message exploitable"`.
Pour toute écriture, le succès doit être prouvé par un identifiant (clé de confirmation) ;
sans lui, l'agent considère que l'action a échoué.

## Alassane — serveur MCP (lecture)

| Outil | Arguments | Retour attendu |
|---|---|---|
| `rechercher_biens` | filtres optionnels typés (quartier, type, budget max, pièces…) | `{ok, biens:[…], count}` |
| `detail_bien` | `bien_id` | `{ok, bien:{…}}` ou `{ok:false, error}` |

Transport : `streamable_http` (`MCP_URL`) ou `stdio` (`MCP_COMMAND`/`MCP_ARGS`). Les schémas
d'arguments sont lus depuis le serveur : l'agent n'en suppose aucun.
Besoin : un résultat vide doit renvoyer `count: 0` (ou une liste vide), pas une erreur.

## Baye Serigne — module Python (`GOOGLE_TOOLS_MODULE`)

Expose `get_google_tools() -> list[langchain_core.tools.BaseTool]` (ex. `StructuredTool.from_function`,
arguments typés, docstring rédigée pour le modèle).

| Outil | Risque | Arguments minimaux | Retour (clé de confirmation) |
|---|---|---|---|
| `lister_creneaux_libres` | lecture | `date_debut`, `date_fin`, `conseiller?` | `{ok, creneaux:[{debut, fin, conseiller}], count}` |
| `creer_visite` | **écriture** | `bien_id`, `debut`, `client_email`, `conseiller?` | `{ok, event_id}` (refus si créneau pris : `ok:false`) |
| `deplacer_visite` | **écriture** | `event_id`, `nouveau_debut` | `{ok, event_id}` |
| `annuler_visite` | **écriture** | `event_id` | `{ok, event_id}` |
| `lire_demandes_non_traitees` | lecture (contenu non fiable) | — | liste d'emails avec `thread_id`, expéditeur, objet, corps |
| `creer_brouillon` | préparation | `thread_id`, `destinataire`, `objet`, `corps` | `{ok, draft_id}` |
| `lire_brouillon` | lecture | `draft_id` | `{ok, thread_id, to, subject, body}` |
| `envoyer_brouillon` | **écriture** | `draft_id` | `{ok, message_id}` |
| `changer_libelle` | suivi interne (orchestrateur seul) | `message_id`, `statut` | `{ok, message_id, libelle}` |

Points importants :
* **`lire_brouillon` est indispensable** : le conseiller valide le texte exact du brouillon, et
  l'accord est invalidé si ce texte change avant l'envoi.
* Les règles d'agence (45 min, lundi–samedi, 9 h–18 h, Africa/Dakar, adresse du bien dans
  l'événement) sont appliquées **par vos outils** ; l'agent ne les recalcule pas.
* Libellés de suivi : `changer_libelle` (module Google) pose un seul libellé parmi
  `a_traiter` / `en_attente_validation` / `repondu` / `transmis` (créé s'il n'existe pas). C'est
  l'orchestrateur qui l'appelle après chaque traitement, jamais le modèle :

  | Issue | Libellé |
  |---|---|
  | validation en attente | en attente de validation |
  | envoi effectué | répondu |
  | transmis à un conseiller (dont injection) | transmis à un conseiller |
  | terminé sans envoi (ex. envoi refusé) | à traiter (un humain reprend) |
  | erreur LLM / limite d'étapes, email mis de côté | inchangé |

  Un échec de libellé est tracé mais n'interrompt jamais le traitement.
* Aucun outil Google ne doit envoyer/écrire de lui-même : l'agent suspend l'exécution avant.

## Boucle d'intégration (pour la démo)

`python -m sama_agent.cli inbox --watch 60` : reprend les validations en attente, puis relit
`lire_demandes_non_traitees` toutes les 60 s et traite chaque email (`SamaAgent.process`), avec
validation du conseiller dans le terminal. Une erreur de lecture est affichée et la boucle continue.
