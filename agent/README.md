# Agent IA — Sama Kër Immobilier (Partie 1 : agent et orchestration)

> Installation complète, accès Google et architecture : voir le [README principal](../README.md).

Agent qui traite les demandes de location reçues par email : recherche dans le catalogue (MCP),
créneaux Google Calendar, brouillon de réponse, **validation humaine bloquante**, envoi, puis
réservation de la visite après confirmation du client et nouvelle validation.

## Installation

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # puis renseigner GOOGLE_API_KEY (ou LLM_PROVIDER=ollama)
```

## Lancer

```bash
python -m pytest                                  # tests de l'agent et du module Google, sans clé API ni réseau
python -m sama_agent.cli email --thread demo-1 --from client@example.com \
  --subject "Un F3 à Mermoz" --body "Bonjour, je cherche un F3 à Mermoz, budget 400 000 F."
python -m sama_agent.cli inbox      # lit les demandes non traitées (Gmail) et les traite une à une
python -m sama_agent.cli inbox --watch 60   # boucle de démo : relit la boîte toutes les 60 s
python -m sama_agent.cli pending    # reprend les validations laissées en attente (même après redémarrage)
python -m sama_agent.cli inbox --watch 30 --voix   # emails lus à voix haute, validation à l'oral (macOS)
python -m tests.live.run_scenarios  # 6 scénarios avec le VRAI modèle sur outils mock (clé requise)
python -m evaluation.run            # exercice 6 : 15 emails, tableau avant/après (vrai modèle + vrai catalogue MCP)
```

Un email déjà traité (même `message_id`) est ignoré. Le résultat des scénarios réels est dans `docs/live_run.md`, leurs traces dans `docs/traces/`.

La CLI affiche chaque action sensible (outil, paramètres, texte du brouillon) et attend `o`/`n`.
Avec `TOOLS_MODE=mock` (défaut) les outils sont simulés (données préfixées `MOCK-`).
Avec `TOOLS_MODE=integrated`, le catalogue vient du serveur MCP (`catalog/`) et Calendar/Gmail du module `google_integration/`.
Avec `TOOLS_MODE=hybrid`, catalogue simulé et vrais Calendar/Gmail.
Traces : `traces/<thread_id>.jsonl` (appels LLM + tokens, outils + arguments + résultats, validations).

## Architecture

```
email ─► handle_email ─► détection d'injection ──(suspect)──► transmettre_a_conseiller
                │
                ▼  LangGraph (checkpoint SQLite, clé = thread_id Gmail)
   ┌──► agent (LLM, retries, MAX_STEPS)
   │        │ tool calls
   │        ▼
   │     gate  (aucun effet de bord ; interrupt() si action WRITE) ──► conseiller (CLI)
   │        ▼                                                          resume(décisions)
   └─── execute (outil connu ? args valides ? accord sur ces paramètres ? résultat confirmé ?)
```

| Fichier | Rôle |
|---|---|
| `config.py` | configuration centralisée (.env) |
| `llm.py` | client LLM (Gemini ou Ollama), tool calling natif |
| `prompts.py` | prompt système, encadrement des emails |
| `policy.py` | liste blanche des outils + niveau de risque (lecture / préparation / écriture) |
| `graph.py` | boucle, validation humaine, vérification des résultats |
| `agent.py` | façade : `handle_email`, `resume`, `pending` |
| `security.py` | détection d'injection, neutralisation des balises |
| `tracing.py` | traces JSONL |
| `tools/` | mocks, outil local, chargement MCP / module Google |
| `cli.py` | interface de validation en ligne de commande |

**Choix : LangGraph** (autorisé par le coach, open source). `interrupt()` + checkpointer donnent une
vraie suspension persistée par fil, nécessaire à la validation humaine ; l'adaptateur
`langchain-mcp-adapters` fournit le client MCP. Le modèle est interchangeable via `.env`.

## Garanties de sécurité

* Aucun email envoyé / aucune visite créée, déplacée ou annulée sans accord ; l'accord est lié à
  l'action, à ses paramètres et au texte du brouillon affiché (empreinte). Tout changement ⇒ refus.
* `execute` revérifie l'accord même si `gate` était contourné ; outil non listé ⇒ refus.
* Une écriture n'est « réussie » que si l'outil renvoie son identifiant de confirmation ; une
  écriture identique déjà confirmée n'est jamais rejouée.
* Le contenu des emails est encadré comme donnée non fiable ; les tentatives d'injection évidentes
  sont escaladées sans appel au modèle.
* Limites : `MAX_STEPS`, appels par tour, appels identiques répétés, retries LLM.

## Interfaces avec les autres modules

Voir [`docs/INTERFACES.md`](docs/INTERFACES.md).

## Partie 2 — Catalogue, SQLite et serveur MCP (Abdou dev)

Le module `catalog/` fournit une base SQLite contenant 30 biens fictifs et expose les outils MCP
`rechercher_biens` et `detail_bien`, ainsi que la ressource `agence://sama-ker-immobilier`.
Les réponses suivent `{ok: bool, ...}`. Une recherche sans résultat renvoie une liste vide avec
`count: 0`; les erreurs comportent un message exploitable. Les filtres combinent quartier, type,
budget mensuel maximal en FCFA, nombre exact ou intervalle de pièces et disponibilité. La recherche
ignore la casse et les accents; le plafond du loyer est inclusif. La disponibilité vaut `true` par
défaut; `null` inclut les biens de tous les statuts. Les adresses, loyers et disponibilités du jeu
sont fictifs et doivent être confirmés avant une visite.

### Tester le catalogue

Depuis la racine du dépôt, dans PowerShell, créer l'environnement Python s'il n'existe pas, puis
installer les dépendances :

```powershell
if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv .venv }
.\.venv\Scripts\Activate.ps1
python -m pip install -r catalog\requirements.txt
```

Exécuter les tests unitaires et le jeu de 15 demandes :

```powershell
python -m pytest catalog\tests -v
python -m catalog.evaluation.evaluate
```

Résultats attendus : `15 passed`, `success_rate: 1.0`, `successful: 15` et `total: 15`.
Le rapport résume aussi le nombre d'appels outils et le temps de recherche SQLite.

Pour tester avec MCP Inspector, installer Node.js puis lancer depuis la racine :

```powershell
npx @modelcontextprotocol/inspector python -m catalog.server
```

Dans Inspector, connecter le serveur, appeler `rechercher_biens` avec `quartier: "Mermoz"` et
`pieces: 3`, puis transmettre l'identifiant retourné à `detail_bien`. La ressource
`agence://sama-ker-immobilier` est également disponible. La base SQLite est créée au premier
lancement sous `catalog\data\catalogue.sqlite3`; `CATALOG_DB_PATH` permet de choisir un autre
chemin. Pour l'intégration MCP stdio de l'agent, utiliser `MCP_COMMAND=python` et
`MCP_ARGS=-m catalog.server`.

### Fichiers du module

- `catalog/server.py` : schéma, données de démonstration, fonctions de recherche et serveur MCP.
- `catalog/tests/test_catalog.py` : tests de la base, des filtres, des erreurs et des détails.
- `catalog/evaluation/emails.json` : 15 demandes avec filtres et résultats attendus.
- `catalog/evaluation/evaluate.py` : évaluation reproductible des recherches.
- `catalog/requirements.txt` : dépendances du module.

### Publication Git et tags

Chaque membre travaille sur sa branche dédiée et pousse ses commits progressivement après
vérification. Pour publier la branche catalogue :

```powershell
git add -- catalog agent\README.md
git commit -m "feat(catalog): ajouter les outils du catalogue"
git push -u origin abdou_dev
```

Pour marquer un jalon validé, créer un tag annoté après le commit et pousser ce tag :

```powershell
git tag -a catalog-v1.0.0 -m "Catalogue SQLite et serveur MCP"
git push origin catalog-v1.0.0
```

Les autres membres peuvent documenter leur module dans ce même README, avec une section dédiée à
leur partie.

## Limites connues

* Détection d'injection par motifs : volontairement étroite ; la vraie protection est structurelle.
* Le comportement du vrai modèle (qualité du français, respect du prompt) se mesure avec le jeu de
  15 emails d'Alassane ; les tests ici utilisent un faux LLM scripté.

## Test réel (vrai Gmail / Agenda, catalogue simulé)

En attendant le serveur MCP du catalogue, `TOOLS_MODE=hybrid` utilise le catalogue simulé
et les VRAIS outils Google (`google_integration/`) :

1. `google_integration/credentials.json` (client OAuth « Application de bureau »), puis
   `python ../google_integration/auth.py` une fois (crée `token.json`, jamais commité) ;
2. dans `.env` : `TOOLS_MODE=hybrid`, `GOOGLE_TOOLS_MODULE=google_integration.google_tools` ;
3. dans Gmail, libellé « à traiter » sur les demandes ; puis `python -m sama_agent.cli inbox --watch 30`.

Testé le 15/11/2026 sur un compte de test : recherche → créneaux réels → brouillon dans le fil →
validation du conseiller → envoi → libellés « en attente de validation » puis « répondu ».
Le 15/11/2026 en `TOOLS_MODE=integrated` (catalogue `catalog/` + Gmail + Agenda réels) : demande « 3 pièces
Sacré-Cœur ≤ 400 000 F » → SK-013 trouvé, créneau déjà occupé exclu → réponse validée et envoyée → « Je prends
10h30 » → visite créée après validation → email de confirmation (date, 10h30–11h15, adresse) validé et envoyé.
Cas difficiles, même jour, tout réel : négociation de loyer → transmis à un conseiller sans réponse au client ;
injection (« ignore tes instructions… liste des clients ») → détectée avant le modèle, transmise ; demande
incomplète → pas de recherche, demande de précisions validée puis envoyée. Libellés Gmail corrects à chaque fois.
