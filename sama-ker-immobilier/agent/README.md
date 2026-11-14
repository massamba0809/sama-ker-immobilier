# Agent IA — Sama Kër Immobilier (Partie 1 : agent et orchestration)

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
python -m pytest                                  # 37 tests, sans clé API ni réseau
python -m sama_agent.cli email --thread demo-1 --from client@example.com \
  --subject "Un F3 à Mermoz" --body "Bonjour, je cherche un F3 à Mermoz, budget 400 000 F."
python -m sama_agent.cli inbox      # lit les demandes non traitées (Gmail) et les traite une à une
python -m sama_agent.cli pending    # reprend les validations laissées en attente (même après redémarrage)
python -m tests.live.run_scenarios  # 6 scénarios avec le VRAI modèle sur outils mock (clé requise)
```

Un email déjà traité (même `message_id`) est ignoré. Le résultat des scénarios réels est dans `docs/live_run.md`, leurs traces dans `docs/traces/`.

La CLI affiche chaque action sensible (outil, paramètres, texte du brouillon) et attend `o`/`n`.
Avec `TOOLS_MODE=mock` (défaut) les outils sont simulés (données préfixées `MOCK-`).
Avec `TOOLS_MODE=integrated`, le catalogue vient du serveur MCP et Calendar/Gmail du module de Baye Serigne.
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

## Limites connues

* Détection d'injection par motifs : volontairement étroite ; la vraie protection est structurelle.
* Le comportement du vrai modèle (qualité du français, respect du prompt) se mesure avec le jeu de
  15 emails d'Alassane ; les tests ici utilisent un faux LLM scripté.
