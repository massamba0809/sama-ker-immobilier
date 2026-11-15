## Analyse de trois échecs

Les trois échecs ont été trouvés en lisant les traces (une ligne JSON par événement : appel LLM avec tokens,
appel d'outil avec arguments et résultat, validation). Les trois correctifs sont regroupés derrière le
réglage `Settings.correctifs` : `avant` les désactive, `après` les active ; tout le reste (modèle, catalogue,
emails, vérifications) est identique entre les deux colonnes.

### Échec 1 — quota du fournisseur : l'agent abandonne un email (E07, évaluation du 15/11)

* **Trace** (`premier_passage/E07_quota_429.jsonl`, tableau complet dans `premier_passage/resultats.md`) :
  un premier 429 est absorbé, puis `rechercher_biens` → `{count: 0}` ; l'appel suivant au modèle reçoit
  `429 RESOURCE_EXHAUSTED` (quota gratuit, requêtes par minute) à la 1re tentative et aux 2 nouvelles
  tentatives (1 s puis 2 s plus tard) ; l'agent s'arrête (`llm_failed`, statut `llm_error`) : aucune
  réponse au client.
* **Cause** : un délai de nouvelle tentative pensé pour une panne réseau (1 s, 2 s), alors que le quota
  est compté sur une fenêtre d'une minute. (La trace tronquait le message d'erreur à 300 caractères ; il
  est désormais conservé jusqu'à 1 500 caractères pour voir le délai éventuellement indiqué par l'API.)
* **Correction** (`graph.retry_delay`) : en cas de 429, attendre le délai indiqué par le fournisseur
  s'il figure dans le message (« retry in N s » / `retryDelay`), sinon 20 s ; 3 nouvelles tentatives au lieu de 2. L'email reste de toute façon
  « à traiter » en cas d'échec final (il sera repris au passage suivant). Test : `test_rate_limit_waits_for_provider_delay`.

### Échec 2 — confirmation de visite fausse (trace réelle `docs/traces_reelles/2_echec_analyse_confirmation.jsonl`)

* **Trace** : `creer_visite` validé par le conseiller → `{ok: true, event_id}` ; puis `creer_brouillon` :
  « Cette demande est désormais **en attente de validation par notre conseiller**. Nous reviendrons vers
  vous… », sans adresse ni heure de fin.
* **Cause** : rien dans le prompt ne disait qu'une visite créée est confirmée ; le modèle a confondu la
  validation déjà donnée avec une étape à venir. La validation humaine a empêché l'envoi.
* **Correction** : règle 9 du prompt (visite créée = confirmée ; date, horaire début–fin, adresse,
  conseiller). Mesurée par le cas **E05** (le client confirme un créneau ; on vérifie la confirmation).

### Échec 3 — refus avec consigne non appliqué (même trace réelle)

* **Trace** : le conseiller refuse l'envoi avec la consigne « la visite est confirmée… indique l'horaire et
  l'adresse » ; le modèle répond « Je dois reformuler la réponse en précisant… » et s'arrête (aucun
  nouveau `creer_brouillon`, statut `done`).
* **Cause** : le message de refus renvoyé au modèle (« REFUSÉ : action non autorisée… ») ne demandait pas
  d'appliquer la consigne.
* **Correction** : le refus transmet la consigne et l'ordre de proposer **maintenant** la version corrigée
  (règle 11 du prompt). Test : `test_refusal_with_instruction_tells_llm_to_apply_it_now`. Non mesuré dans
  le tableau (le conseiller de l'évaluation approuve tout) ; vérifié en réel le 15/11 (trace 1).

### Lecture du tableau

* **12/15 → 15/15.** E05 échoue avant correction pour la raison de l'échec 2 (confirmation sans adresse
  ni heure de fin) et passe après. E09 et E10 échouent avant correction par quota (échec 1) et passent
  après : l'agent attend la fin de la fenêtre au lieu d'abandonner.
* **Les échecs de quota ne sont pas déterministes** : ils dépendent du rythme des requêtes et touchent un
  cas différent d'un passage à l'autre (E07 au premier passage, E09 et E10 ici). Les échecs de contenu
  (E05) se reproduisent, le modèle étant appelé à température 0.
* **Coût de la correction** : temps moyen 14,7 s → 30,0 s par email (attentes sur quota), tokens
  +19 % (règles ajoutées au prompt, et E01 qui a consulté l'agenda sans qu'on le lui demande cette fois).
  Sur une boîte réelle, quelques secondes de plus valent mieux qu'un email abandonné.

### Échec restant connu

Le modèle consulte parfois l'agenda alors que le client n'a pas demandé de visite (premier passage de
E01 : `lister_creneaux_libres` du 16 au 23/11 pour « un 3 pièces à Mermoz, budget 300 000 F »), et
propose des créneaux spontanément. Sans conséquence pour le client (créneaux réellement libres), mais un
appel d'outil et des tokens en plus ; le comportement varie d'un passage à l'autre. Non corrigé.
