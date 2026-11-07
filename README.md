# FleetMind AI

Agent d'exploitation IA local qui surveille les missions de plusieurs systèmes de gestion du transport (TMS).

**Contrainte fondamentale :** les modèles IA, les traitements, les données et les inférences restent sur une infrastructure contrôlée par l'entreprise. Aucun service d'inférence IA externe n'est utilisé.

## Avancement

| Étape | Contenu | État |
|---|---|---|
| 1 | Socle du projet et route `GET /health` | Terminée |
| 2 | TMS Alpha et TMS Beta simulés | Terminée |
| 3 | Modèle canonique et moteur générique d'intégration | Terminée |
| 4 | Moteur de règles et surveillance des missions | Terminée |
| 5 | Alertes, relances et escalades | Terminée |
| 6 | Surveillance automatique | Terminée |
| 7.1 | Chatbot avec LangGraph et Qwen local | Terminée |
| 7.2 | Agents d'incident et de surveillance | Terminée |
| 7.3 | RAG (Qdrant, embeddings locaux) | Terminée |
| 7.4 | Intégration d'un TMS depuis sa documentation OpenAPI | Terminée |
| 7.5 | Modèle de risque de retard | Terminée |
| 8 | Dossier de conception pour Proorga | Terminé, à relire |
| 9 | Interface Streamlit | Terminée |
| 10 | Authentification et droits par TMS | Terminée |

## À faire plus tard

1. **Mémoire de conversation :** permettre les questions de suite, comme « et chez Beta ? », en reprenant le TMS et la période de la question précédente, sans transmettre tout l'historique au LLM.
2. **Docker Compose :** conteneuriser le backend, l'interface, PostgreSQL et Ollama. Docker doit d'abord être installé.

## Journal des étapes

### Étape 1 : socle du projet

- **Réalisé :** arborescence du projet, application FastAPI avec `GET /health`, configuration par `.env`, `.gitignore`.
- **Choix :**
  - environnement virtuel `fleetmind_venv`, placé à côté du dossier du projet ;
  - Python 3.12, pour limiter le risque d'incompatibilité avec les bibliothèques de ML.
- **Vérification :** <http://127.0.0.1:8000/health> renvoie `"status": "ok"`.

### Étape 2 : TMS simulés

- **Réalisé :** deux API indépendantes du backend, chacune avec sa documentation OpenAPI, ses données dans `data/synthetic/` et ses tests.
- **Choix :**
  - API volontairement différentes (voir le tableau plus bas) ;
  - données figées au 07/11/2026 pour des résultats reproductibles ;
  - villes, clients et chauffeurs marocains, et fuseau `Africa/Casablanca` pour le TMS Beta.
- **Scénarios de chaque TMS :** mission normale, retard, incident ouvert, déviation, informations manquantes.
- **Limites :** missions sans évolution dans le temps, API en lecture seule, pannes non simulées.

### Étape 3 : moteur générique d'intégration

- **Réalisé :**
  - modèle canonique des missions, événements et incidents ([canonical.py](backend/schemas/canonical.py)) ;
  - une configuration par TMS dans [config/tms/](config/tms/) : authentification, pagination, endpoints et correspondance des champs ;
  - un client unique ([generic_client.py](backend/integrations/generic_client.py)) et un normaliseur ([normalizer.py](backend/integrations/normalizer.py)), sans code propre à un TMS.
- **Choix :**
  - toutes les dates sont converties en UTC ;
  - un champ omis ou à `null` devient `None` ;
  - une configuration incohérente est refusée au chargement ;
  - les secrets restent dans `.env`, et les fichiers YAML ne contiennent que le nom des variables.
- **En cas de problème :**

| Situation | Comportement |
|---|---|
| Statut ou type inconnu | Valeur `UNKNOWN`, sans deviner |
| Enregistrement illisible | Écarté et signalé avec sa raison, les autres sont conservés |
| Pagination incomplète | Erreur plutôt qu'une liste partielle |
| TMS injoignable ou erreur 5xx | `TmsUnavailableError` |
| Identifiants refusés | `TmsAuthError`, après un seul renouvellement du jeton s'il y en a un |

- **Limites :**
  - configurations écrites à la main : leur génération depuis la documentation OpenAPI viendra avec le LLM ;
  - pas de nouvelle tentative automatique après une panne : elle sera gérée par la surveillance ;

### Étape 4 : moteur de règles et surveillance

- **Réalisé :**
  - règles de chaque TMS dans [config/rules/](config/rules/) ;
  - moteur déterministe ([rules_engine.py](backend/services/rules_engine.py)) ;
  - cycle de surveillance par TMS ([monitoring.py](backend/services/monitoring.py)), qui produit un rapport de constats.
- **Règles disponibles :** retard estimé, arrivée dépassée, déviation d'itinéraire, information manquante, données trop anciennes, incident ouvert.
- **Choix :**
  - le LLM n'intervient pas : les seuils viennent uniquement des fichiers de règles ;
  - chaque constat est classé en fait, risque ou information manquante, et indique la version des règles appliquées ;
  - l'heure de référence est un paramètre, pour rejouer les scénarios sur les données figées.
- **En cas de problème :**

| Situation | Comportement |
|---|---|
| TMS injoignable | Rapport `FAILED`, jamais « aucun problème » |
| TMS sans fichier de règles | Rapport `FAILED`, aucune règle d'un autre TMS n'est utilisée |
| Enregistrement écarté | Rapport `PARTIAL`, avec la raison et sans la donnée reçue |
| Statut de mission inconnu | Mission listée dans le rapport, car aucune règle de statut ne s'y applique |
| Gravité d'incident inconnue | Incident toujours signalé |

- **Limites :**
  - les règles sont des règles de démonstration (`origin: demonstration`) ;
  - la déviation est mesurée par rapport à des lignes droites entre les points de l'itinéraire ;
  - ces cycles étaient lancés à la demande : ils sont devenus automatiques à l'étape 6.

### Étape 5 : alertes, relances et escalades

- **Réalisé :**
  - tables des alertes, de l'historique d'audit et des notifications, créées par migration Alembic ;
  - cycle de vie des alertes ([alerting.py](backend/services/alerting.py)) ;
  - notifications simulées ([notification.py](backend/services/notification.py)) ;
  - routes `POST /monitoring/cycles`, `GET /alerts`, `GET /alerts/{id}`, `POST /alerts/{id}/acknowledge`, `POST /alerts/{id}/resolve` et `GET /missions`.
- **Cycle de vie :** ouverte, prise en charge, résolue. Chaque action est historisée avec son auteur.
- **Choix :**
  - une seule alerte active par problème, garantie par un index unique en base ;
  - chaque notification porte une clé d'idempotence : rejouer un cycle n'envoie rien de plus ;
  - destinataires, délais de relance et escalades sont définis par TMS dans [config/rules/](config/rules/) ;
  - la prise en charge arrête les relances et les escalades de l'alerte ;
  - une alerte n'est fermée automatiquement qu'après un cycle `COMPLETE` ;
  - la panne d'un TMS ouvre une alerte système, refermée dès qu'il répond de nouveau.
- **Limites :**
  - aucun envoi réel : les notifications sont seulement enregistrées, et les destinataires sont des rôles fictifs ;
  - pas d'authentification : l'auteur d'une action est déclaré librement, à corriger avant toute mise en production ;
  - une alerte prise en charge n'est plus relancée, même si elle traîne ;
  - une alerte résolue à la main est rouverte au cycle suivant si le problème est toujours détecté ;
  - les enregistrements écartés et les statuts inconnus figurent dans le rapport du cycle, mais ne créent pas d'alerte ;
  - les tests automatisés utilisent SQLite : le comportement sur PostgreSQL 18 a été vérifié une fois, à la main, et n'est pas rejoué par `pytest`.

### Étape 6 : surveillance automatique

- **Réalisé :**
  - un cycle régulier par TMS, sans action humaine ([scheduler.py](backend/services/scheduler.py)) ;
  - exécution commune au planificateur et au lancement manuel ([cycle_runner.py](backend/services/cycle_runner.py)) ;
  - trace de chaque cycle dans la table `monitoring_runs` ;
  - route `GET /monitoring/status`, qui indique si le planificateur tourne et signale un TMS en retard de surveillance.
- **Choix :**
  - la fréquence est propre à chaque TMS (`polling_interval_seconds` dans [config/tms/](config/tms/)) ;
  - un verrou PostgreSQL par TMS empêche deux cycles simultanés, y compris avec plusieurs processus : un cycle planifié est alors ignoré, un cycle manuel reçoit une erreur 409 ;
  - une panne de TMS ou de base n'arrête pas le planificateur, et le cycle suivant reprend normalement ;
  - un TMS est signalé en retard après 3 intervalles sans cycle réussi ;
  - activation par `MONITORING_ENABLED`, désactivée par défaut dans le code.
- **Horloge de démonstration :** avec `MONITORING_SIMULATED_START` et `MONITORING_TIME_SPEED`, le temps démarre à l'heure choisie et avance en accéléré, ce qui rejoue les données figées.
- **Exemples dans `/docs` :** `POST /monitoring/cycles` et les actions sur les alertes proposent un corps valide par défaut.
- **Limites :**
  - l'horloge simulée reprend au dernier cycle planifié enregistré ; elle ne repart de l'heure configurée qu'après un nettoyage de `monitoring_runs` ;
  - pas de nouvelle tentative avant le cycle suivant après une panne ;
  - un TMS est tenu verrouillé pendant ses appels API, ce qui mobilise une connexion à la base ;
  - la table `monitoring_runs` grandit sans purge ;
  - les tests automatisés utilisent SQLite, où le verrou n'existe pas : il a été vérifié à la main sur PostgreSQL.
- **Vérification sur la base `fleetmind` :** 18 cycles planifiés exécutés sur PostgreSQL 18, sans échec, sans alerte active en double et sans notification en double.

### Étape 7.1 : chatbot

- **Réalisé :**
  - graphe LangGraph : interpréter, router, rapporter, résumer ([orchestrator.py](backend/agents/orchestrator.py)) ;
  - rapports construits par le code ([reporting_agent.py](backend/agents/reporting_agent.py)) ;
  - route `POST /chatbot/ask` et trace de chaque question dans la table `chat_logs`.
- **Questions possibles :** incidents ouverts, alertes actives, bilan des missions, retards, informations manquantes. Tout le reste est refusé, y compris toute demande de modification.
- **Rôle du LLM :** classer la question et rédiger un résumé court. Les faits, risques, informations manquantes, le périmètre et la fraîcheur viennent du code.
- **Protections :**

| Situation | Comportement |
|---|---|
| Résumé avec un identifiant, un nombre ou un autre TMS absent des données | Remplacé par un résumé produit par le code |
| Résumé qui oublie une mission | Remplacé par un résumé produit par le code |
| TMS en panne ou enregistrements illisibles | Le LLM ne résume pas, et la réponse indique « Résultat incomplet » |
| Bilan des missions | Rédigé par le code, sans LLM |
| LLM indisponible pour interpréter | Erreur 503, sans deviner |
| LLM indisponible pour résumer | Les données sont quand même renvoyées |
| Adresse Ollama publique | Refusée au démarrage |

- **Retards :** ils sont calculés avec les règles du TMS concerné, sans créer d'alerte. Les missions livrées en retard y figurent comme des faits.
- **Périodes :** le LLM propose une catégorie ou des dates, le code les corrige et calcule les bornes ([periods.py](backend/agents/periods.py)) dans le fuseau `BUSINESS_TIMEZONE`.
  - Une expression relative (« hier », « semaine dernière ») est décidée par le code, même si le LLM dit autre chose.
  - Si une année, un mois ou une date cités ne tombent pas dans la période, le chatbot demande de préciser, sans lire aucune donnée.
- **Mesures sur la machine de développement (GTX 1650, 4 Go) :** le modèle occupe 3,5 Go, dont 34 % sur le processeur. Les réponses prennent de 12 à 17 secondes avec résumé et moins de 4 secondes sans. Le premier appel ajoute environ 9 secondes de chargement.
- **Évaluation du modèle :** 25 questions sur 25 bien classées, sur un petit jeu qui a aussi servi à régler la consigne. Seuls les champs utilisés par le code sont évalués. À relancer après tout changement de modèle ou de consigne : `$env:RUN_LLM_TESTS="1"; ..\fleetmind_venv\Scripts\python -m pytest tests/test_llm_live.py`.
- **Limites :**
  - pas d'authentification : les droits de l'utilisateur demandés par le dossier Proorga ne sont pas encore appliqués ;
  - les périodes portent sur l'arrivée prévue des missions et sur l'ouverture des incidents et alertes ; elles se limitent à ce que le TMS expose encore ;
  - les retards en cours sont évalués à l'heure de référence, y compris dans une période passée ;
  - le LLM n'a pas de mémoire de conversation : les échanges sont enregistrés et réaffichés, mais chaque question est traitée seule ;
  - le contrôle détecte les inventions et les oublis, pas une formulation trompeuse : les faits détaillés font foi ;
  - les descriptions venant des TMS sont transmises au LLM telles quelles, y compris dans leur langue d'origine ;
  - au-delà de 12 constats, le résumé est produit par le code.

### Étape 7.2 : agents d'incident et de surveillance

- **Réalisé :** [incident_agent.py](backend/agents/incident_agent.py) et [monitoring_agent.py](backend/agents/monitoring_agent.py), deux graphes LangGraph appelés par l'orchestrateur via `POST /chatbot/ask`. Ils ne dialoguent pas entre eux.
- **Agent d'incident :** explique une alerte (« Explique l'alerte 12 ») à partir de la mission, de ses événements et de ses incidents. Le LLM écrit l'explication, contrôlée comme les autres résumés.
- **Brouillon de relance :** écrit par le code et **jamais envoyé**. Le destinataire suggéré vient des règles du TMS et de l'escalade atteinte.
- **Agent de surveillance :** le point de situation (« Que traiter en priorité ? ») et le classement (statut, gravité, escalade, ancienneté) sont produits par le code.
- **Choix issu des essais :** avec Qwen, le point de situation prenait environ 70 secondes et contenait des erreurs de sens. Il n'utilise donc plus le LLM.
- **Limites :**
  - le LLM ne formule aucune recommandation : la procédure liée à l'alerte est jointe telle quelle (voir l'étape 7.3) ;
  - le contrôle ne détecte pas une formulation trompeuse, par exemple une cause suggérée sans chiffre inventé ;
  - la classification prend de 10 à 25 secondes quand la mémoire est saturée (0,7 Go libre pendant les essais) : c'est la principale limite de performance sur cette machine ;
  - l'évaluation complète sur le vrai modèle n'a pas été relancée après la dernière simplification de la consigne.

### Étape 7.3 : RAG

- **Réalisé :**
  - 5 procédures de démonstration dans [data/documentation/procedures/](data/documentation/procedures/), découpées par section ;
  - embeddings via Ollama ([embeddings.py](backend/rag/embeddings.py)) et index Qdrant local, sans serveur ([retrieval.py](backend/rag/retrieval.py)) ;
  - agent documentaire ([knowledge_agent.py](backend/agents/knowledge_agent.py)), appelé pour les questions du type « Que faire en cas de panne ? » ;
  - route `POST /documents/reindex`, à relancer après toute modification d'une procédure.
- **Choix :**
  - Ollama plutôt que Sentence Transformers, pour éviter d'installer PyTorch sur une machine à la mémoire déjà saturée ;
  - le RAG ne sert qu'aux procédures : les chiffres et l'état des missions viennent toujours des TMS ;
  - chaque procédure indique le TMS concerné, et un filtre empêche de l'appliquer à un autre TMS ;
  - le chatbot utilise la procédure entière, car la section la mieux classée n'était pas toujours la première à appliquer ;
  - chaque réponse cite sa source et sa version, reproduit le texte utilisé et signale une procédure de démonstration.
- **En cas de problème :**

| Situation | Comportement |
|---|---|
| Aucune procédure assez proche | Le chatbot le dit, sans demander au LLM de rédiger |
| Procédure propre à un TMS non cité | Non consultée : le chatbot invite à préciser le TMS |
| Réponse du LLM avec un nombre absent des extraits | Remplacée par le passage de la procédure |
| LLM indisponible pour rédiger | Le passage de la procédure est renvoyé |
| Embeddings ou index indisponibles | Le chatbot le dit, sans répondre de mémoire |

- **Lien avec l'agent d'incident :** « Explique l'alerte 12 » joint la procédure liée, dans `sources`, sans passer par le LLM.
  - Le sujet recherché vient du type de règle, ou du code d'incident défini dans `incident_procedure_topics` de [config/rules/](config/rules/).
  - La recherche se limite au TMS de l'alerte. Il n'y a pas de procédure pour une alerte résolue ni pour une panne de la surveillance.
- **Vérification avec le vrai modèle :** 4 questions réelles. Panne de véhicule et panne frigorifique chez Beta trouvent la bonne procédure ; la panne frigorifique chez Alpha et l'accident du travail obtiennent « aucune procédure ».
- **Limites :**
  - le seuil `RAG_MIN_SCORE` (0,65) est réglé sur 11 questions seulement, et les scores des questions pertinentes et hors sujet se recouvrent presque (0,686 contre 0,679) : le LLM confirme donc aussi la pertinence, sauf s'il est indisponible ;
  - le contrôle détecte les nombres inventés, pas une étape ajoutée sans chiffre ni une faute de rédaction : la réponse courte peut aussi omettre des étapes, et le texte complet de la procédure figure donc dans `sources` ;
  - temps mesurés sur la machine de développement : de 10 à 86 secondes, selon le chargement du modèle et la mémoire disponible ;
  - le mode local de Qdrant ne convient qu'à un seul processus : plusieurs serveurs nécessiteront un serveur Qdrant ;
  - les documentations d'API ne sont pas encore indexées.

### Étape 7.4 : intégrer un TMS depuis sa documentation OpenAPI

- **Réalisé :**
  - analyse de la documentation par le code ([openapi_parser.py](backend/integrations/openapi_parser.py)) : authentification, pagination, routes et nature des champs ;
  - génération d'un brouillon ([tms_onboarding.py](backend/integrations/tms_onboarding.py)) : le LLM choisit les routes, associe les champs et traduit les valeurs ;
  - essai réel et écriture du brouillon ([onboard_cli.py](backend/integrations/onboard_cli.py)) dans [config/tms_drafts/](config/tms_drafts/) ;
  - TMS Gamma simulé, inconnu du système, pour le démontrer.
- **Commande :** `..\fleetmind_venv\Scripts\python -m backend.integrations.onboard_cli --id tms_gamma --name "TMS Gamma" --base-url http://127.0.0.1:8103 --key-env MOCK_GAMMA_API_KEY`
- **Garde-fous :**
  - le LLM ne peut choisir que des champs existants et du bon type, et sa réponse est revérifiée ;
  - un champ obligatoire resté vide donne lieu à une seule question de plus, puis le brouillon est refusé ;
  - un champ utilisé deux fois, une valeur non traduite, un enregistrement rejeté ou des dates inversées sont signalés dans le brouillon ;
  - **rien n'est activé automatiquement** : seuls les fichiers de `config/tms/` sont chargés.
- **Pour activer un brouillon après relecture :** le déplacer dans `config/tms/`, créer ses règles dans `config/rules/`, puis redémarrer le backend. Sans règles, ses cycles restent en échec.
- **Résultat sur Gamma :** 28 champs et 11 valeurs correctement associés, 3 routes correctes, 3 missions, 2 incidents et 2 événements lus sans rejet. Ce résultat a demandé deux corrections réglées sur ce même TMS, et ne prouve donc pas la fiabilité sur un autre.
- **Limites :**
  - seule l'authentification par clé d'API dans un en-tête est déduite : TMS Beta, à jeton, n'est pas pris en charge automatiquement ;
  - les dates doivent être au format ISO 8601 : un format local comme celui de Beta n'est pas déduit ;
  - les statuts en texte libre, sans liste de valeurs dans la documentation, restent à compléter à la main ;
  - la qualité dépend de la documentation, notamment des descriptions de champs ;
  - la durée varie fortement : de 64 secondes jusqu'à 24 minutes sur cette machine, sans cause établie ;
  - l'essai réel lit le TMS : il faut donc ses accès, et il ne détecte pas toutes les erreurs de sens, d'où la relecture humaine.

### Étape 7.5 : modèle de risque de retard

- **Réalisé :**
  - variables calculées par [features.py](backend/ml/features.py), avec le même code à l'entraînement et en production ;
  - 3 000 missions synthétiques marocaines ([synthetic.py](backend/ml/synthetic.py)), reproductibles grâce à une graine ;
  - entraînement LightGBM et mesures ([train.py](backend/ml/train.py), [evaluate.py](backend/ml/evaluate.py), [predict.py](backend/ml/predict.py)) ;
  - question au chatbot : « Quelles missions risquent d'arriver en retard ? ».
- **Rôle du modèle :** il informe sans décider. Son estimation ne crée ni alerte ni notification, et ne remplace aucune règle. Elle est classée en risque, avec ses principaux facteurs.
- **Domaine :** une mission dont l'heure d'arrivée est dépassée n'est pas évaluée, car le retard est alors un fait établi par les règles.
- **Définition du retard :** plus de 30 minutes après l'heure prévue.
- **Mesures sur 1 500 observations de test synthétiques, issues de missions absentes de l'entraînement :**

| Mesure | Modèle | Référence |
|---|---|---|
| ROC AUC | 0,931 | 0,778 (écart entre temps écoulé et avancement seul) |
| Score de Brier (plus bas = mieux) | 0,081 | 0,215 (taux moyen, 31 % de retards) |
| Précision à 50 % | 0,905 | |
| Rappel à 50 % | 0,750 | |

- **Limites :**
  - **ces mesures ne prouvent aucune performance réelle** : le modèle retrouve les règles de mon simulateur, et il faudra le réentraîner sur l'historique réel de l'entreprise ;
  - le modèle ne tient pas compte d'une déviation : `AL-1004`, très éloigné de son itinéraire, n'obtient que 20 % à 09 h 30 ;
  - les données de démonstration sont figées : leurs positions peuvent être postérieures à l'heure de référence, ce qui fausse certaines estimations ;
  - l'horloge simulée finit par dépasser toutes les arrivées prévues, et plus aucune mission n'est alors évaluée ;
  - pas de suivi de la dérive du modèle, ni de réentraînement automatique.

### Étape 8 : dossier de conception pour Proorga

- **Réalisé :** [dossier de conception technique](https://claude.ai/artifact/VCi7Y8LXRxMvcVJYBpQagW), avec le schéma d'architecture, le cycle de vie d'une alerte, les deux scénarios réels, le dimensionnement, les coûts et la feuille de route.
- **Limites :** les coûts et le dimensionnement sont des estimations, sans devis ni essai de charge. Les mesures viennent de TMS simulés et de données synthétiques.

### Étape 9 : interface Streamlit

- **Réalisé :** quatre écrans dans [frontend/](frontend/), qui passent uniquement par l'API du backend.
  - **Tableau de bord :** alertes actives, alertes graves à traiter d'abord, état de la surveillance par TMS.
  - **Alertes :** filtres, détail, prise en charge, résolution, explication, historique et notifications simulées.
  - **Missions :** données lues dans chaque TMS, avec leur fraîcheur.
  - **Chatbot :** résumé, faits, risques, informations manquantes, périmètre, brouillon de relance et procédure.
- **Choix :**
  - aucune logique métier dans l'interface ;
  - statistiques d'usage de Streamlit désactivées, et écoute limitée à la machine locale ([config.toml](.streamlit/config.toml)) ;
  - une surveillance arrêtée ou en retard est signalée, et n'est jamais présentée comme « aucun problème » ;
  - un résumé écrit par l'IA est signalé comme tel, et non un résumé produit par le code ;
  - les sources sont en langage courant, avec l'heure de lecture la plus ancienne ;
  - les détails sont repliés, mais les informations manquantes et les TMS indisponibles restent toujours visibles ;
  - navigation latérale avec icônes et page active surlignée, compte et déconnexion en bas ;
  - bouton « Nouveau chat » et conversations enregistrées en base, propres à chaque compte, que chacun peut supprimer après confirmation.
- **Limites :**
  - pas de rafraîchissement automatique des écrans ;
  - tests automatisés sur le comportement des écrans ; le rendu n'est contrôlé qu'à la main, et seuls le tableau de bord et le chatbot l'ont été dans un navigateur ;
  - surveillance des fichiers désactivée, pour éviter le message « File change » : il faut relancer Streamlit après toute modification de `frontend/`.

### Étape 10 : authentification et droits par TMS

- **Réalisé :** connexion, sessions, trois rôles et un périmètre de TMS par compte ([auth.py](backend/services/auth.py)). Toute l'API exige une connexion, sauf `/health` et `/auth/login`.
- **Rôles :**

| Rôle | Consulter et chatbot | Prendre en charge, résoudre | Lancer un cycle, réindexer |
|---|---|---|---|
| Dirigeant (`executive`) | Oui | Non | Non |
| Exploitant (`operator`) | Oui | Oui | Non |
| Administrateur (`admin`) | Oui | Oui | Oui |

- **Choix :**
  - mots de passe hachés avec `scrypt`, jetons aléatoires stockés hachés et valables 8 heures, révocables ;
  - un TMS ou une alerte hors périmètre répond comme s'ils n'existaient pas ;
  - le LLM ne reçoit jamais le nom d'un TMS interdit, et une question qui en cite un est refusée sans l'appeler ;
  - l'auteur d'une action vient du compte connecté, et chaque question au chatbot garde son auteur ;
  - blocage de 15 minutes après 5 échecs, avec le même message d'erreur quelle que soit la cause ;
  - droits relus en base à chaque requête : un compte désactivé perd l'accès aussitôt.
- **Gérer les comptes :**
  - créer : `..\fleetmind_venv\Scripts\python -m backend.core.manage_users create --username fatima --role operator --tms tms_alpha` (plusieurs TMS séparés par des virgules, ou `*` pour tous) ;
  - changer un mot de passe : `..\fleetmind_venv\Scripts\python -m backend.core.manage_users set-password --username fatima` ;
  - désactiver : `..\fleetmind_venv\Scripts\python -m backend.core.manage_users deactivate --username fatima`.
- **Limites :**
  - comptes locaux, sans lien avec un annuaire d'entreprise ni double authentification ;
  - rôles et périmètre modifiables seulement en recréant un compte ;
  - pas de chiffrement réseau (HTTPS), à ajouter avant tout déploiement hors de la machine ;
  - la détection d'un TMS interdit dans une question repose sur son nom : une formulation détournée ne donne accès à aucune donnée, mais peut recevoir une autre réponse que le refus ;
  - la route `/docs` reste visible sans connexion, sans donner accès aux données ;
  - une conversation enregistrée conserve ses réponses, même si les droits du compte sont réduits ensuite ; un compte peut supprimer ses conversations, mais `chat_logs` garde la trace d'audit, sans purge automatique ;
  - le blocage porte sur le compte, ce qui permet à un tiers de le bloquer temporairement.

## Principes d'architecture

- **Moteur d'intégration générique :** une configuration par TMS plutôt qu'un connecteur Python par TMS.
- **Modèle canonique :** les réponses de tous les TMS sont normalisées dans un même modèle de données.
- **Règles déterministes :** les règles métier sont versionnées par TMS et appliquées par un moteur dédié.
- **Rôle limité du LLM :** il interprète les demandes, analyse et synthétise. Il n'invente ni ne modifie aucune règle ; les règles et les services backend contrôlent les décisions et les actions.
- **Exploitation fiable :** confidentialité, journaux d'audit, validation des données, gestion des erreurs et indisponibilités, déduplication des alertes et idempotence des actions.

MCP n'est pas intégré à la première version. L'architecture modulaire permettra de l'ajouter ultérieurement si nécessaire.

## Technologies

| Domaine | Technologie | Statut |
|---|---|---|
| Backend | Python, FastAPI | En place |
| Intégration TMS | httpx, PyYAML, Pydantic | En place |
| Orchestration IA | LangGraph | En place |
| LLM local | `qwen3:4b` via Ollama | En place |
| RAG | Qdrant en mode local, `nomic-embed-text` via Ollama | En place |
| Prédiction des retards | LightGBM, entraîné sur des données synthétiques | En place |
| Base de données | PostgreSQL 18, SQLAlchemy, Alembic | En place |
| Planification | APScheduler | En place |
| Interface | Streamlit | En place |
| Conteneurisation | Docker Compose | Prévu |

## Structure du projet

```text
fleetmind-ai/
├── backend/
│   ├── api/            # Application FastAPI et routes HTTP
│   ├── integrations/   # Moteur générique, lecture OpenAPI et génération de brouillons
│   ├── agents/         # Workflows LangGraph (LLM)
│   ├── services/       # Logique déterministe : surveillance, règles, notifications
│   ├── rag/            # Ingestion, embeddings et recherche documentaire
│   ├── ml/             # Modèle de risque de retard
│   ├── models/         # Tables de la base de données
│   ├── schemas/        # Modèle canonique, configurations, règles et constats
│   └── core/           # Configuration et éléments transverses
├── config/tms/         # Description de chaque TMS actif (YAML versionné)
├── config/tms_drafts/  # Brouillons générés, jamais chargés sans validation humaine
├── config/rules/       # Règles et politique de notification de chaque TMS
├── migrations/         # Évolutions de la base de données (Alembic)
├── mock_tms/           # TMS Alpha, Beta et Gamma simulés
├── frontend/           # Interface Streamlit
├── data/
│   ├── synthetic/      # Données de démonstration
│   ├── documentation/  # Procédures indexées par le RAG
│   └── qdrant/         # Index vectoriel local (non versionné)
├── models/             # Fichiers des modèles IA (non versionnés)
└── tests/              # Tests automatisés
```

`agents/` interprète et synthétise, tandis que `services/` décide et agit.

## Démarrage (Windows, PowerShell)

Prérequis : Python 3.12. Exécuter les commandes depuis la racine du projet. L'environnement virtuel `fleetmind_venv` est créé à côté du dossier du projet, pas à l'intérieur.

```powershell
py -3.12 -m venv ..\fleetmind_venv
..\fleetmind_venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Compléter `DATABASE_URL` dans `.env`, puis créer les tables et un premier compte administrateur :

```powershell
..\fleetmind_venv\Scripts\python -m alembic upgrade head
..\fleetmind_venv\Scripts\python -m backend.core.manage_users create --username admin --role admin --tms *
```

| Service | Commande | Documentation |
|---|---|---|
| Backend | `..\fleetmind_venv\Scripts\python -m uvicorn backend.api.main:app --reload` | <http://127.0.0.1:8000/docs> |
| TMS Alpha | `..\fleetmind_venv\Scripts\python -m uvicorn mock_tms.tms_alpha.app:app --port 8101` | <http://127.0.0.1:8101/docs> |
| TMS Beta | `..\fleetmind_venv\Scripts\python -m uvicorn mock_tms.tms_beta.app:app --port 8102` | <http://127.0.0.1:8102/docs> |
| Interface | `..\fleetmind_venv\Scripts\python -m streamlit run frontend/app.py` | <http://127.0.0.1:8501> |

Le modèle de risque, non versionné, se crée avec `..\fleetmind_venv\Scripts\python -m backend.ml.train`.

Le chatbot nécessite Ollama et deux modèles : `ollama pull qwen3:4b` et `ollama pull nomic-embed-text`. Construire ensuite l'index avec `POST /documents/reindex`.

Tests (les simulateurs n'ont pas besoin d'être lancés) :

```powershell
..\fleetmind_venv\Scripts\python -m pytest
```

## Différences entre les TMS simulés

| | TMS Alpha (port 8101) | TMS Beta (port 8102) |
|---|---|---|
| Authentification | En-tête `X-API-Key` | Jeton obtenu via `POST /auth/token` |
| Missions | `/api/v1/missions` | `/v2/transports` |
| Incidents | `/api/v1/incidents` | `/v2/anomalies` |
| Pagination | `page`, `page_size` | `offset`, `limit` |
| Format JSON | Anglais, `snake_case`, objets imbriqués | Français, `camelCase`, structure plate |
| Statuts | Texte (`IN_TRANSIT`) | Code numérique (`20`) |
| Dates | ISO 8601 en UTC | `07/11/2026 09:30`, heure du Maroc |
| Positions | `{"lat": …, "lon": …}` | Chaîne `"45.76,4.84"` |
| Information absente | Champ omis | Champ à `null` |
