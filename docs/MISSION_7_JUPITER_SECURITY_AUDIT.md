# Mission 7 — audit indépendant de sécurité Jupiter / CPI / DEX

Date : 22 novembre 2026. Branche auditée : codex/mission-7-reliability-2026-11-22, issue du head PR #6 f62ed1d95de2cf824d6b26945a553d50cc1db3a1.

## Verdict

**État fail-closed par défaut : confirmé statiquement et par tests ciblés. LIVE : BLOCKED.**

Aucun identifiant DEX n'a été ajouté à l'allowlist. Aucune transaction n'a été signée ou envoyée dans cette mission. L'audit porte sur les contrôles locaux et les tests isolés; il n'inclut pas une vérification du code déployé des programmes DEX sur chaîne.

## Contrôles vérifiés

- config.py:46-48 fixe PAPER par défaut et exige une seconde option LIVE explicite.
- config.py:128 initialise JUPITER_CPI_PROGRAM_ALLOWLIST à vide.
- jupiter_executor.py:290-314 refuse avant signature une allowlist vide, invalide ou non canonique.
- jupiter_executor.py:316-392 exige une simulation complète, une trace interne valide, des index de comptes résolus et un CPI externe autorisé.
- jupiter_executor.py:395-435 appelle simulateTransaction avant signature avec sigVerify=false, replaceRecentBlockhash=true et innerInstructions=true.
- jupiter_executor.py:782-825 impose les deux options LIVE puis passe par la validation locale, le contrôle de frais et la simulation CPI avant de signer.

Les tests Mission 7 ciblent les allowlists invalides/vides, les traces manquantes ou mal formées, les index hors plage, l'échec RPC, le refus en PAPER avant accès wallet/réseau et les transactions synthétiques. Ils ne signent ni ne soumettent de transaction.

## Constats et limites

| ID | Niveau avant activation | Constat | État |
|---|---|---|---|
| M7-JUP-01 | Élevé | L'allowlist vérifie des identifiants de programmes et des index d'accounts, mais ne décode pas la sémantique de chaque instruction CPI, ne vérifie pas chaque compte selon les invariants du DEX et ne borne pas les variations de solde attendues. | Ouvert avant toute activation d'un DEX. L'allowlist reste vide. |
| M7-JUP-02 | Élevé | Un identifiant autorisé n'est pas une preuve de hash de bytecode, d'absence d'autorité d'upgrade ni d'examen de la version exécutée. | Aucun programme DEX ni autorité de mise à jour n'a été vérifié sur chaîne. |
| M7-JUP-03 | Modéré | La simulation utilise commitment=processed et remplace le blockhash; le code ne lie pas le contrôle à un état immuable ni à une simulation vérifiée indépendamment. L'état de pool/programme peut changer avant une éventuelle exécution. | Limite opérationnelle documentée; pas une preuve de sûreté d'exécution. |
| M7-JUP-04 | Bloquant LIVE | Les tests et audits sont locaux/synthétiques. Aucun test d'ordre en environnement cible ni validation d'exécution sur un programme DEX n'a été fait. | LIVE reste bloqué; aucune signature ni transaction réelle n'est autorisée par cette mission. |

Solana définit un CPI comme l'appel d'une instruction d'un programme à un autre; les privilèges signer et writable peuvent être transmis au programme appelé. Cela explique pourquoi l'identité et les comptes internes comptent, sans certifier la logique du programme. [Documentation CPI Solana](https://solana.com/docs/core/cpi).

La documentation RPC précise que simulateTransaction simule sans diffuser et peut remplacer le recent blockhash si l'option est activée. Une simulation décrit le résultat observé sur l'état disponible au nœud; elle n'est pas une preuve de l'état ultérieur ni un audit de code. [simulateTransaction](https://solana.com/docs/rpc/http/simulatetransaction), [structures JSON RPC](https://solana.com/docs/rpc/json-structures).

Les programmes déployés avec loader-v3 peuvent être mis à jour quand une autorité d'upgrade est définie; définir l'autorité à None rend le programme immuable. Une revue future devrait lier les IDs approuvés au bytecode et à l'état d'autorité vérifiés au moment de l'examen. [Déploiement de programmes Solana](https://solana.com/docs/core/programs/program-deployment).

## Conditions minimales avant de réexaminer LIVE

1. Maintenir l'allowlist vide jusqu'à un audit indépendant par programme.
2. Pour chaque DEX, vérifier l'ID, le loader, le hash du code déployé et l'autorité d'upgrade à une hauteur de slot documentée.
3. Décoder et valider les instructions CPI et leurs comptes selon les invariants propres au DEX; l'identité du programme seule est insuffisante.
4. Valider les deltas attendus de SOL/tokens, les frais et le slippage avant toute signature.
5. Ajouter un examen indépendant des changements et effectuer les validations dans un environnement cible explicitement autorisé, toujours sans fonds réels.

Ces conditions ne sont pas satisfaites par les tests locaux de cette mission. Le statut reste **LIVE BLOCKED**.
