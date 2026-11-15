# Catalogue immobilier et serveur MCP

Module de la partie d'Abdou dev. Il fournit une base SQLite locale contenant 30 biens fictifs
et expose les outils MCP attendus par l'agent :

- `rechercher_biens(quartier?, type?, budget_max?, pieces?, pieces_min?, pieces_max?, disponible?, limit?)`
- `detail_bien(bien_id)`
- la ressource `agence://sama-ker-immobilier`

Les réponses suivent le contrat `{ok: bool, ...}` de `agent/docs/INTERFACES.md`. Les recherches
vides réussissent avec `biens: []` et `count: 0`; les erreurs de saisie et de lecture renvoient
`ok: false` et un message exploitable. Les filtres sont combinés, le plafond de loyer est inclusif,
et les quartiers/types ignorent la casse et les accents. `disponible` vaut `true` par défaut; `null`
désactive ce filtre. `count` rapporte le nombre total de correspondances même si `limit` tronque la
liste (maximum 50).

## Arguments et exemples

`type` accepte `Appartement`, `Studio`, `Maison` ou `Villa` (comparaison normalisée). `pieces`
filtre un nombre exact; les bornes `pieces_min` et `pieces_max` sont inclusives et ne se combinent
pas avec `pieces`. `detail_bien` prend l'identifiant retourné par la
recherche. Exemples à saisir dans l'inspecteur MCP :

```json
{"tool": "rechercher_biens", "arguments": {"quartier": "Mermoz", "type": "Appartement", "budget_max": 300000, "pieces": 3}}
```

Retour :

```json
{"ok": true, "biens": [{"id": "SK-001", "type": "Appartement", "quartier": "Mermoz", "loyer": 300000, "pieces": 3, "adresse": "Mermoz, Dakar", "disponible": true}], "count": 1}
```

```json
{"tool": "detail_bien", "arguments": {"bien_id": "SK-001"}}
```

Retour : `{"ok": true, "bien": {"id": "SK-001", "type": "Appartement", "quartier": "Mermoz", "loyer": 300000, "pieces": 3, "adresse": "Mermoz, Dakar", "disponible": true}}`.
Une recherche sans résultat renvoie `{"ok": true, "biens": [], "count": 0}`; un identifiant
inconnu renvoie `{"ok": false, "error": "Aucun bien trouvé pour l'identifiant SK-999."}`.

## Guide de test pas à pas

Les commandes suivantes sont à lancer dans PowerShell depuis la racine du dépôt.

1. Activer l'environnement Python du projet (le créer s'il n'existe pas), puis installer les
   dépendances du module :

   ```powershell
   if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv .venv }
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r catalog\requirements.txt
   ```

2. Lancer les tests unitaires :

   ```powershell
   python -m pytest catalog\tests -v
   ```

   Résultat attendu : `15 passed`.

3. Vérifier les 15 demandes de test :

   ```powershell
   python -m catalog.evaluation.evaluate
   ```

   Le rapport doit afficher `success_rate: 1.0`, `successful: 15` et `total: 15`.

4. Tester les outils dans MCP Inspector. Il faut Node.js et `npx` installés :

   ```powershell
   npx @modelcontextprotocol/inspector python -m catalog.server
   ```

   Ouvrir l'adresse locale affichée dans le terminal, se connecter au serveur, puis essayer
   `rechercher_biens` avec `quartier: "Mermoz"` et `pieces: 3`. Vérifier ensuite
   `detail_bien` avec le `bien_id` retourné. Le serveur expose aussi la ressource
   `agence://sama-ker-immobilier`.

5. Pour vérifier le démarrage du serveur sans Inspector :

   ```powershell
   python -m catalog.server
   ```

   Le processus reste actif et attend les requêtes MCP sur son entrée standard. Arrêter avec
   `Ctrl+C` après le test.

La base est créée automatiquement dans `catalog\data\catalogue.sqlite3`. Pour choisir un autre
chemin avant de lancer les commandes :

```powershell
$env:CATALOG_DB_PATH = "C:\temp\catalogue-test.sqlite3"
```

## Installation et lancement

Suivre d'abord le [guide de test](#guide-de-test-pas-à-pas) pour activer l'environnement et installer
les dépendances. Depuis la racine, le serveur se lance avec `python -m catalog.server`.
La base SQLite est créée automatiquement dans `catalog\data\catalogue.sqlite3`; son chemin peut
être changé avec `CATALOG_DB_PATH`.

Pour connecter l'agent via MCP stdio, depuis la racine du dépôt, configurez `MCP_COMMAND=python`
et `MCP_ARGS=-m catalog.server` dans l'environnement de l'agent (ou adaptez les valeurs à son
fichier `.env`). Le serveur ne requiert ni clé API ni accès réseau externe.

## Schéma SQLite

La table `biens` contient `id` (clé primaire), `type`, `quartier`, `loyer` (FCFA/mois),
`pieces`, `adresse` et `disponible`. Les colonnes `type_key` et `quartier_key` sont des clés
internes normalisées pour la recherche sans casse ni accents. Les requêtes SQL utilisent des
paramètres liés; les identifiants de biens sont validés avant lecture.

## Jeu d'évaluation

`emails.json` contient 15 demandes fictives, les filtres catalogue attendus et les identifiants
attendus. Le script mesure le taux de réussite des résultats, le nombre d'appels outil, le temps
SQLite et une estimation reproductible du volume arguments/résultats (JSON / 4 caractères).

## Publier cette tâche progressivement

Travaille sur la branche dédiée `abdou_dev`. Après chaque étape terminée et testée, ajoute seulement
les fichiers concernés, crée un commit décrivant cette étape, puis pousse la branche :

```powershell
git status
git add -- catalog agent/README.md
git commit -m "feat(catalog): ajouter le catalogue SQLite"
git push -u origin abdou_dev
```

Pour les étapes suivantes de la même branche, répète `git add` avec les fichiers modifiés, puis
`git commit` et `git push`. Ne pousse pas les fichiers de configuration contenant des secrets ni
la base locale générée.

Un tag versionné marque un jalon déjà commité. Par exemple, après validation complète de cette tâche :

```powershell
git tag -a catalog-v1.0.0 -m "Catalogue SQLite et serveur MCP"
git push origin catalog-v1.0.0
```

Pour consulter les tags locaux :

```powershell
git tag --list
```

### Échecs limites examinés et corrections

1. `Sacré-Cœur` pouvait ne pas correspondre à `SACRE COEUR` à cause des accents, de `œ` et du tiret :
   la normalisation ignore désormais casse, diacritiques et ponctuation.
2. Une demande de trois pièces filtrée avec `pieces_min=3` pouvait aussi proposer un F4 :
   le filtre exact `pieces=3` est maintenant disponible et couvert par le jeu.
3. La disponibilité par défaut masquait les biens à confirmer : `disponible=null` désactive le
   filtre, et un test vérifie que les deux statuts sont alors inclus.

Le cas limite du loyer égal au budget est aussi testé : le plafond est inclusif (`<=`).

Ces corrections sont rejouées par `python -m catalog.evaluation.evaluate` et par les tests unitaires.
Les résultats d'inventaire sont fictifs; une disponibilité doit être confirmée par un conseiller.
