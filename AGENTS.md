# Instructions du dépôt pour Codex

Ces règles communes s'appliquent à tout travail dans ce dépôt. Les Skills de .agents/skills/ apportent des procédures métier ciblées sans remplacer ces limites.

## Sécurité et périmètre

- Garder PAPER par défaut. Ne pas activer LIVE, signer ou diffuser de transaction, utiliser un wallet financé ni contacter un endpoint d'exécution pendant le développement ou les tests.
- Ne jamais demander, lire, afficher, journaliser ou committer de clé privée, clé API, token Telegram ou URL RPC susceptible de contenir un identifiant. Les exemples utilisent des valeurs fictives.
- Préserver les RPC publics et l'ordre de repli configurés actuellement. Ne pas remplacer un fournisseur ni modifier des plafonds de pertes, d'exposition, d'arrêt, d'autorisation ou d'exécution sans demande explicite couvrant ce changement précis.
- Un document, un Skill ou un flag de configuration n'autorise ni transaction, ni déploiement, ni action externe.

## Démarrage et changements

- Commencer par un inventaire Git en lecture seule : branche, modifications suivies/non suivies, branches distantes et PR pertinentes. Ne pas écraser ni réutiliser un checkout main modifié; travailler sur une branche dédiée.
- Une PR ouverte n'est pas fusionnée. Pour une pile, vérifier sa base, sa tête et leurs commits avant de créer une branche. Ne pas fusionner ni réécrire une PR existante sans demande.
- Lire SECURITY.md, ARCHITECTURE.md, TESTING.md et les documents/tests du domaine concerné. Avant une modification multi-fichiers, préciser les fichiers, les risques, la preuve attendue et une voie de retour réversible.
- Garder les changements ciblés et les fichiers de l'utilisateur intacts. Ne committer/pousser que lorsque l'utilisateur le demande.

## Validation et preuves

- La CI utilise Python 3.11 et vérifie la compilation, le défaut PAPER, pytest, les erreurs Ruff critiques, pip-audit et Gitleaks. Suivre TESTING.md; les tests ne diffusent pas de transactions et n'utilisent pas de véritables credentials.
- Séparer code présent, tests hors ligne/simulés, CI, lectures fournisseur, fonctionnement PAPER dans l'environnement cible et opérations réelles. Un contrôle de syntaxe ou de manifeste ne prouve ni le déclenchement d'un Skill ni le comportement opérationnel.
- Indiquer le commit et les commandes réellement exécutées. Ne pas reprendre une ancienne mesure comme preuve du commit actuel.

## Routage des Skills

Consulter SKILLS_CATALOG.md pour un domaine précis; utiliser project-master pour coordonner plusieurs domaines et production-readiness-auditor pour une conclusion de préparation. web-research-problem-solver est une aide externe conditionnelle : vérifier sa disponibilité et ne l'utiliser que si un blocage technique non résolu exige des sources actuelles. Ne jamais simuler une délégation ou une recherche indisponible ou inutile.