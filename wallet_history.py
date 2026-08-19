"""
════════════════════════════════════════════════════════════════
HISTORIQUE WALLET — Retrouver les tokens créés par une adresse
════════════════════════════════════════════════════════════════
Étape 3 de la vidéo : avant d'ajouter un dev au monitoring, on
regarde son historique pour voir s'il a un pattern de rug régulier.

CORRIGÉ suite à 2 tests réels en conditions réelles : l'ancienne version
s'appuyait sur l'API "Enhanced Transactions" de Helius (classification
"human-readable" — source/type/description) pour repérer les créations
Pump.fun. Documentée comme "legacy, en maintenance, ne parse que certains
types" — testée sur 2 devs différents (736 et 51 tokens connus selon un
outil externe), elle retournait 0 dans les deux cas.

Cette version décode directement l'INSTRUCTION BRUTE via le programme
Pump.fun (getTransaction RPC standard, pas l'API Enhanced), en identifiant
l'instruction "create" par son discriminant Anchor — SHA256("global:create")
tronqué à 8 octets, vérifié correspondre à la valeur documentée officielle
([24, 30, 200, 40, 5, 28, 7, 119]). Ne dépend d'aucune classification tierce
potentiellement obsolète, seulement de la structure du programme lui-même.
"""

import asyncio
import hashlib
import logging

import base58

import config
import rpc_client

log = logging.getLogger("wallet_history")

# ── Compteur simple pour visualiser la répartition Mobula vs Helius ──────
# Répond à "pourquoi Helius est toujours sollicité" : montre concrètement
# combien d'adresses ont été résolues via Mobula (0 coût Helius) vs combien
# sont tombées en repli sur Helius (Mobula ne les connaissait pas).
_stats = {"mobula_hits": 0, "helius_fallback": 0}


def reset_stats():
    global _stats
    _stats = {"mobula_hits": 0, "helius_fallback": 0}


def get_stats() -> dict:
    return dict(_stats)

# Vérifiés par calcul indépendant ET confirmés par un vrai test en conditions
# réelles (diagnostic sur un dev avec 51 tokens connus) :
# - "create" (legacy, tokens SPL standard) : mint à l'index 0, créateur à l'index 6
# - "create_v2" (Token-2022, plus récent — la plupart des devs actifs l'utilisent
#   maintenant d'après nos tests) : mint à l'index 0, créateur à l'index 5
_CREATE_DISCRIMINATOR = hashlib.sha256(b"global:create").digest()[:8]
_CREATE_V2_DISCRIMINATOR = hashlib.sha256(b"global:create_v2").digest()[:8]

# CORRIGÉ suite à un vrai diagnostic (debug_wallet_history.py) sur un dev
# réel : l'index 6 pour "create" (legacy) était faux — l'adresse créateur
# attendue apparaissait bien dans la liste des comptes, mais à l'index 7,
# pas 6. Décalage d'un cran qui faisait échouer la comparaison à chaque
# fois, donc get_created_tokens() ne trouvait JAMAIS aucune création
# "legacy" (seul create_v2, index 5, fonctionnait). Structure de comptes
# confirmée sur ce test : [0]=mint [1]=mint_authority [2]=bonding_curve
# [3]=associated_bonding_curve [4]=global [5]=mpl_token_metadata_program
# [6]=metadata_pda [7]=creator/user [8]=system_program ...
_CREATOR_INDEX_BY_DISCRIMINATOR = {
    bytes(_CREATE_DISCRIMINATOR): 7,
    bytes(_CREATE_V2_DISCRIMINATOR): 5,
}


async def is_wallet_fresh_before(address: str, reference_signature: str) -> bool:
    """
    AJOUTÉ pour le filtre "fresh wallet" du cluster (find_dev_cluster.py) —
    n'existait nulle part avant malgré ce que décrivait la documentation
    d'origine du projet. Vérifie qu'AUCUNE activité n'existait pour `address`
    AVANT `reference_signature` (typiquement la transaction de dépôt exchange
    qu'on analyse) — càd que ce wallet a été créé/utilisé pour la première
    fois précisément à ce moment-là, pas un wallet déjà actif pour autre
    chose.

    Un seul appel RPC efficace, plutôt que de paginer tout l'historique du
    wallet : demande à Helius juste 1 signature plus ANCIENNE que
    reference_signature (paramètre standard "before" de
    getSignaturesForAddress). Si rien n'est retourné, reference_signature
    est bien la toute première transaction connue de ce wallet.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"before": reference_signature, "limit": 1}],
    }
    result = await rpc_client.rpc_post(payload)
    prior_activity = result if isinstance(result, list) else []
    return len(prior_activity) == 0


async def has_created_any_token(address: str, max_signatures_checked: int = 200) -> dict:
    """
    Vérification LÉGÈRE et rapide — contrairement à get_created_tokens() ou
    get_all_created_tokens() qui cherchent la liste complète, celle-ci
    s'arrête dès qu'UNE création est trouvée (ou après max_signatures_checked
    signatures sans en trouver). Équivalent des "petites étoiles" du
    transcript — filtre rapide "cette adresse a-t-elle déjà créé un token,
    oui/non" avant d'investir dans un scan complet coûteux sur cette adresse.

    AMÉLIORÉ : essaie d'abord Mobula (wallet/deployer, quota SÉPARÉ de
    Helius) — si configuré et que ça répond, épargne complètement le quota
    Helius pour cette vérification. Repli automatique sur Helius sinon.

    MODIFIÉ : retourne maintenant {"mint": str, "block_time": int | None}
    (au lieu d'un simple bool) — utile pour afficher QUEL token a été créé
    et TRIER par date. getSignaturesForAddress retourne du plus récent au
    plus ancien, donc la première création trouvée en scannant dans cet
    ordre est déjà la PLUS RÉCENTE de ce wallet.
    Retourne None si rien trouvé (équivalent de l'ancien False).
    """
    # ── Essaie Mobula en premier (quota séparé de Helius) ────────────────
    # Contrôlé par config.MOBULA_VERIFY_ON_CHAIN :
    # - False (test actuel demandé) : fait confiance à Mobula directement,
    #   ZÉRO appel Helius pour cette étape.
    # - True : vérifie chaque résultat sur la blockchain avant acceptation
    #   (1-2 appels Helius légers) — plus fiable, à réactiver si le mode
    #   direct montre des faux positifs.
    if config.MOBULA_API_KEY:
        import mobula_client
        mobula_tokens = await mobula_client.get_deployer_tokens(address)

        if not config.MOBULA_VERIFY_ON_CHAIN:
            # Mode direct — confiance aveugle à Mobula, mais garde quand
            # même la protection de type. Utilise maintenant le vrai
            # block_time fourni par Mobula (createdAt confirmé par un vrai
            # test) au lieu de None — permet le tri par date sans appel
            # Helius supplémentaire.
            for candidate in mobula_tokens:
                mint_candidate = candidate.get("token_mint")
                if isinstance(mint_candidate, str) and mint_candidate:
                    _stats["mobula_hits"] += 1
                    return {"mint": mint_candidate, "block_time": candidate.get("block_time")}
        else:
            import find_dev
            for candidate in mobula_tokens:
                mint_candidate = candidate.get("token_mint")
                if not isinstance(mint_candidate, str) or not mint_candidate:
                    continue
                creator_info = await find_dev.find_token_creator(mint_candidate)
                if creator_info and creator_info["verified"] and creator_info["dev_address"] == address:
                    _stats["mobula_hits"] += 1
                    return {"mint": mint_candidate, "block_time": candidate.get("block_time")}
                # Sinon : Mobula s'est trompé (ou l'adresse ne correspond pas
                # vraiment) — on continue sans faire confiance à cette donnée.

        _stats["helius_fallback"] += 1

    if not config.HELIUS_API_KEY:
        return None

    signatures = await _get_signatures(address, limit=max_signatures_checked)
    if not signatures:
        return None

    # Le cadencement est maintenant géré GLOBALEMENT par rpc_client.py
    # (_rate_limiter, partagé par tout le script) — plus besoin de pause
    # locale ici, elle ferait doublon et ralentirait inutilement.
    for sig_info in signatures:
        tx = await _get_raw_transaction(sig_info["signature"])
        if not tx:
            continue

        creation = _find_pump_fun_create(tx, expected_creator=address)
        if creation:
            return creation

    return None


async def get_created_tokens(dev_address: str, max_results: int = 15, max_signatures_scanned: int = 300) -> list:
    """
    Retourne la liste des tokens créés par une adresse, triés du plus
    récent au plus ancien : [{"token_mint": str, "signature": str}, ...]

    max_signatures_scanned : les wallets très actifs (bots, plusieurs
    centaines de tokens créés) peuvent avoir beaucoup de transactions
    entre deux créations — augmenté à 300 par défaut (contre 100
    initialement) pour avoir une meilleure chance de remonter à des
    créations plus anciennes sans avoir à paginer indéfiniment.
    """
    if not config.HELIUS_API_KEY:
        log.warning("Pas de clé Helius — impossible de récupérer l'historique.")
        return []

    signatures = await _get_signatures(dev_address, limit=max_signatures_scanned)
    if not signatures:
        return []

    created = []
    for sig_info in signatures:
        tx = await _get_raw_transaction(sig_info["signature"])
        if not tx:
            continue

        creation = _find_pump_fun_create(tx, expected_creator=dev_address)
        if not creation:
            continue

        created.append({"token_mint": creation["mint"], "signature": sig_info["signature"], "block_time": creation.get("block_time")})

        if len(created) >= max_results:
            break

    return created


async def get_recent_buys(wallet_address: str, max_results: int = 10, max_signatures_scanned: int = 300) -> list:
    """
    Trouve les N derniers ACHATS de tokens (via Pump.fun) faits par ce
    wallet — càd les transactions où son solde d'un token SPL a AUGMENTÉ
    (pas ses créations, voir get_created_tokens pour ça).

    Contrairement au décodage précis de l'instruction "create"/"create_v2"
    (dont on connaît l'index exact du mint et du créateur, confirmé par
    test réel), le format exact des comptes de "buy"/"buy_v2" n'a jamais
    été vérifié avec certitude dans ce projet — risqué de deviner. Cette
    fonction utilise à la place les champs standards de Solana RPC
    (preTokenBalances/postTokenBalances), stables et documentés, plutôt que
    de décoder une instruction dont on ne connaît pas le format exact.

    Retourne une liste de dicts triés du plus récent au plus ancien :
    [{"token_mint": str, "signature": str, "block_time": int | None}, ...]
    """
    if not config.HELIUS_API_KEY:
        log.warning("Pas de clé Helius — impossible de récupérer l'historique d'achats.")
        return []

    signatures = await _get_signatures(wallet_address, limit=max_signatures_scanned)
    if not signatures:
        return []

    buys = []
    for sig_info in signatures:
        tx = await _get_raw_transaction(sig_info["signature"])
        if not tx:
            continue

        if not _transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID):
            continue

        mint, tokens_received = _find_token_balance_increase(tx, wallet_address, return_amount=True)
        if not mint:
            continue

        sol_spent = _find_native_sol_spent(tx, wallet_address)

        # CORRIGÉ suite à un vrai écart trouvé (diagnostic sur un cas réel,
        # comparé à un outil externe) : une augmentation de solde de token
        # ne veut pas dire "achat" — un transfert reçu ou un airdrop AUSSI
        # augmente le solde, sans qu'aucun SOL n'ait été dépensé. On ne
        # compte maintenant que les vrais achats (SOL réellement dépensé),
        # pas les simples réceptions de tokens.
        if not sol_spent or sol_spent <= 0:
            continue

        buys.append({
            "token_mint": mint,
            "signature": sig_info["signature"],
            "block_time": tx.get("blockTime"),
            "tokens_received": tokens_received,
            "sol_spent": sol_spent,
        })

        if len(buys) >= max_results:
            break

    return buys


def _transaction_involves_program(tx: dict, program_id: str) -> bool:
    """Vérifie si une transaction touche un programme donné, instructions
    de premier niveau ET imbriquées (CPI, comme pour la détection create)."""
    try:
        instructions = list(tx["transaction"]["message"]["instructions"])
    except (KeyError, TypeError):
        instructions = []
    try:
        for inner in tx.get("meta", {}).get("innerInstructions", []):
            instructions.extend(inner.get("instructions", []))
    except (KeyError, TypeError, AttributeError):
        pass
    return any(ix.get("programId") == program_id for ix in instructions)


def _find_token_balance_increase(tx: dict, wallet_address: str, return_amount: bool = False):
    """
    Compare preTokenBalances/postTokenBalances (champs standards Solana RPC)
    pour trouver un mint dont le solde de `wallet_address` a AUGMENTÉ dans
    cette transaction — signe d'un achat.

    MODIFIÉ : peut maintenant aussi retourner le MONTANT de tokens reçus
    (return_amount=True), nécessaire pour calculer un prix d'entrée exact —
    retourne alors (mint, amount) au lieu de juste mint. Comportement par
    défaut inchangé (juste le mint) pour ne rien casser des appels existants.
    """
    try:
        meta = tx.get("meta", {})
        pre = meta.get("preTokenBalances", []) or []
        post = meta.get("postTokenBalances", []) or []
    except AttributeError:
        return (None, None) if return_amount else None

    def _amounts_by_mint(balances):
        result = {}
        for b in balances:
            if b.get("owner") != wallet_address:
                continue
            mint = b.get("mint")
            try:
                amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            except (TypeError, ValueError):
                amount = 0
            result[mint] = amount
        return result

    pre_amounts = _amounts_by_mint(pre)
    post_amounts = _amounts_by_mint(post)

    for mint, post_amount in post_amounts.items():
        pre_amount = pre_amounts.get(mint, 0)
        if post_amount > pre_amount:
            if return_amount:
                return mint, (post_amount - pre_amount)
            return mint

    return (None, None) if return_amount else None


def _find_native_sol_spent(tx: dict, wallet_address: str) -> float:
    """
    Calcule le montant EXACT de SOL dépensé par `wallet_address` dans cette
    transaction, via preBalances/postBalances (champs standards Solana RPC,
    en lamports) — pas une approximation, la vraie donnée on-chain de la
    transaction d'achat elle-même.

    Soustrait les frais de transaction (meta.fee) du calcul, pour isoler
    uniquement le montant réellement utilisé pour l'achat du token, pas les
    frais de réseau. Retourne le montant en SOL (pas lamports), ou None si
    introuvable/non calculable.
    """
    try:
        meta = tx.get("meta", {})
        account_keys = tx["transaction"]["message"]["accountKeys"]
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        fee = meta.get("fee", 0)
    except (KeyError, TypeError):
        return None

    wallet_index = None
    for i, acc in enumerate(account_keys):
        # accountKeys peut être une liste de strings ou de dicts {"pubkey": ...}
        # selon l'encodage — gère les deux formats par prudence.
        key = acc.get("pubkey") if isinstance(acc, dict) else acc
        if key == wallet_address:
            wallet_index = i
            break

    if wallet_index is None or wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
        return None

    lamports_decrease = pre_balances[wallet_index] - post_balances[wallet_index]
    lamports_spent_on_token = lamports_decrease - fee

    if lamports_spent_on_token <= 0:
        return None

    return lamports_spent_on_token / 1_000_000_000


async def get_all_created_tokens(dev_address: str, max_pages: int = 20, on_progress=None) -> list:
    """
    Version SANS plafond de résultats de get_created_tokens() — pagine sur
    tout l'historique disponible (jusqu'à max_pages * 1000 signatures) pour
    trouver TOUS les tokens créés, pas juste les plus récents.

    Plus lent que get_created_tokens() (potentiellement des centaines
    d'appels réseau pour un dev très actif) — à utiliser pour une analyse
    manuelle approfondie (find_all_dev_tokens.py), pas dans la boucle
    temps réel du bot qui a besoin de rester rapide.
    """
    if not config.HELIUS_API_KEY:
        log.warning("Pas de clé Helius — impossible de récupérer l'historique.")
        return []

    signatures = await get_all_signatures_paginated(dev_address, max_pages=max_pages, on_progress=on_progress)
    if not signatures:
        return []

    created = []
    for i, sig_info in enumerate(signatures):
        tx = await _get_raw_transaction(sig_info["signature"])
        if not tx:
            continue

        creation = _find_pump_fun_create(tx, expected_creator=dev_address)
        if not creation:
            continue

        created.append({"token_mint": creation["mint"], "signature": sig_info["signature"], "block_time": creation.get("block_time")})

        if on_progress and (i + 1) % 50 == 0:
            on_progress(None, f"{i+1}/{len(signatures)} tx scannées, {len(created)} création(s) trouvée(s)")

    return created


async def _get_signatures(address: str, limit: int = 300, max_retries: int = 4) -> list:
    """
    CORRIGÉ suite à un vrai faux-négatif : aucun réessai ici signifiait
    qu'un seul échec RPC (rate limit après une session de tests intensive)
    retournait silencieusement [] — traité ensuite comme "0 token trouvé"
    alors que c'était en réalité "on n'a pas pu vérifier". Même logique de
    réessai que _get_raw_transaction.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    for attempt in range(max_retries):
        result = await rpc_client.rpc_post(payload, timeout=15)
        if isinstance(result, list):
            return result
        if attempt < max_retries - 1:
            await asyncio.sleep(1.0 * (2 ** attempt))
    return []


async def get_token_creation_time(token_mint: str) -> float:
    """
    AJOUTÉ suite à une demande explicite : détermine l'âge d'un token au
    moment d'un achat copy trading (filtre "n'achète que dans les X
    premières secondes après la création", voir paper_trader.open_position
    et config.DEFAULT_WALLET_SETTINGS["max_token_age_at_buy_s"]).

    Réutilise _get_signatures (même RPC, déjà utilisé ailleurs dans ce
    fichier) avec une petite limite — un token vieux de quelques secondes
    n'a de toute façon que très peu de signatures à ce stade, pas besoin de
    la pagination complète de get_all_signatures_paginated (prévue pour un
    historique long, donc bien plus lente).

    getSignaturesForAddress trie du plus récent au plus ancien — la
    DERNIÈRE entrée de la liste renvoyée est donc la toute première
    transaction ayant touché ce mint, la création elle-même dans l'immense
    majorité des cas.

    Retourne le blockTime Unix (float/int) de cette transaction, ou None
    si indéterminable (RPC injoignable après retries, ou aucune signature
    trouvée). None doit être traité comme "âge inconnu", jamais comme
    "token tout juste créé".
    """
    signatures = await _get_signatures(token_mint, limit=50)
    if not signatures:
        return None
    return signatures[-1].get("blockTime")


async def get_all_signatures_paginated(address: str, max_pages: int = 20, page_size: int = 1000,
                                        on_progress=None) -> list:
    """
    Pagine sur TOUT l'historique de signatures d'une adresse via le curseur
    `before`, au lieu de se limiter aux N plus récentes. Utile pour un dev
    très actif dont les créations de tokens peuvent être noyées loin dans
    l'historique par beaucoup d'autres transactions entre chaque création
    (confirmé par test réel : 51 tokens réels, seulement 11 trouvés dans les
    300 dernières signatures).

    max_pages * page_size = plafond de sécurité (20*1000 = jusqu'à 20 000
    signatures scannées) pour éviter une boucle infinie sur un wallet
    extrêmement actif. on_progress(page_num, total_so_far) est appelé après
    chaque page si fourni, utile pour afficher une progression.
    """
    all_sigs = []
    before = None

    for page in range(max_pages):
        params = [address, {"limit": page_size}]
        if before:
            params[1]["before"] = before

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": params}
        batch = await rpc_client.rpc_post(payload, timeout=20)

        if not batch or not isinstance(batch, list):
            break

        all_sigs.extend(batch)
        before = batch[-1]["signature"]

        if on_progress:
            on_progress(page + 1, len(all_sigs))

        if len(batch) < page_size:
            break  # atteint le début de l'historique du wallet

    return all_sigs


async def _get_raw_transaction(signature: str, max_retries: int = 4) -> dict:
    """
    RPC standard (pas l'API Enhanced) — retourne les instructions brutes,
    nécessaire pour décoder nous-mêmes le discriminant Pump.fun.

    CORRIGÉ suite à un vrai rate-limit (429) déclenché par la vérification
    étoile/neutre parallélisée de find_dev_cluster.py — chaque appel manqué
    était traité comme "rien trouvé" sans jamais réessayer, risquant de
    rater une vraie création. Réessaie maintenant avec backoff exponentiel,
    même logique que pattern_detector.py et find_dev.py. Délai de départ et
    nombre de tentatives augmentés (0.5s→1s, 3→4) suite à des 429 persistants
    même avec le premier réessai — le rate-limit semble nécessiter plus de
    temps de repos avant retenter.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    for attempt in range(max_retries):
        result = await rpc_client.rpc_post(payload, timeout=12)
        if isinstance(result, dict) and result:
            return result
        if attempt < max_retries - 1:
            await asyncio.sleep(1.0 * (2 ** attempt))
    return {}


def _find_pump_fun_create(tx: dict, expected_creator: str) -> dict:
    """
    Cherche dans les instructions de la transaction (de premier niveau ET
    imbriquées/CPI) une instruction "create" OU "create_v2" de Pump.fun dont
    le créateur correspond à expected_creator. Retourne
    {"mint": str, "block_time": int | None} ou None si non trouvé.

    Le mint est TOUJOURS à l'index 0 des comptes, dans les deux versions.
    L'index du créateur diffère : 7 pour "create" (legacy, SPL standard),
    5 pour "create_v2" (Token-2022 — confirmé par test réel : la grande
    majorité des devs actifs utilisent maintenant cette version, "create"
    seul retournait 0 résultat systématiquement sur 2 tests indépendants).

    IMPORTANT : scanne aussi les innerInstructions (CPI) — un token créé
    via un bot/aggregateur/bundler peut invoquer Pump.fun indirectement.
    """
    all_instructions = []

    try:
        all_instructions.extend(tx["transaction"]["message"]["instructions"])
    except (KeyError, TypeError):
        pass

    try:
        for inner in tx.get("meta", {}).get("innerInstructions", []):
            all_instructions.extend(inner.get("instructions", []))
    except (KeyError, TypeError, AttributeError):
        pass

    for ix in all_instructions:
        if ix.get("programId") != config.PUMP_FUN_PROGRAM_ID:
            continue

        raw_data = ix.get("data")
        if not raw_data:
            continue

        try:
            data_bytes = base58.b58decode(raw_data)
        except Exception:
            continue

        creator_index = _CREATOR_INDEX_BY_DISCRIMINATOR.get(bytes(data_bytes[:8]))
        if creator_index is None:
            continue  # ni create, ni create_v2 — probablement buy/sell/autre

        accounts = ix.get("accounts", [])
        if len(accounts) <= creator_index:
            continue

        mint = accounts[0]
        creator = accounts[creator_index]

        if creator != expected_creator:
            continue

        return {"mint": mint, "block_time": tx.get("blockTime")}

    return None


async def check_sell_regularity(dev_address: str, tokens: list) -> dict:
    """
    Vérifie si le dev "rug" de manière régulière — càd si le résultat obtenu
    en achetant fin de 1ère bougie avec les mêmes TP/SL est cohérent d'un
    token à l'autre, plutôt que erratique (comme demandé dans les vidéos :
    "on veut un développeur qui vend quasiment tout le temps au même point").

    Méthode : on backteste chaque token individuellement (même méthode que
    backtest.backtest_wallet) et on mesure la dispersion des résultats.
    Faible dispersion + résultats positifs constants = wallet régulier.

    LIMITE CONNUE : ceci mesure la régularité du RÉSULTAT de trade (TP/SL touché
    de façon cohérente), pas directement "le dev vend toujours au même multiple
    de market cap" comme décrit dans la vidéo — cette dernière mesure demanderait
    l'historique exact du market cap au moment du dump du dev, que DexScreener
    ne fournit pas gratuitement en historique. C'est une approximation raisonnable
    mais pas équivalente.

    Retourne : {"regularity_score": float 0-1, "tokens_analyzed": int, "results": list}
    Un score proche de 1 = très régulier (bon candidat au sniping).
    """
    if not tokens:
        return {"regularity_score": 0.0, "tokens_analyzed": 0, "results": []}

    from backtest import backtest_token

    results = []
    for t in tokens:
        r = await backtest_token(t["token_mint"])
        results.append(r["result_pct"])

    if len(results) < 2:
        return {"regularity_score": 0.0, "tokens_analyzed": len(results), "results": results}

    mean = sum(results) / len(results)
    variance = sum((r - mean) ** 2 for r in results) / len(results)
    std_dev = variance ** 0.5

    if mean <= 0:
        regularity_score = 0.0
    else:
        cv = std_dev / mean
        regularity_score = max(0.0, min(1.0, 1 - (cv / 2)))

    log.info(
        f"check_sell_regularity pour {dev_address[:8]}...: "
        f"{len(results)} tokens, moyenne {mean:+.1f}%, écart-type {std_dev:.1f}, "
        f"score de régularité {regularity_score:.2f}"
    )

    return {"regularity_score": regularity_score, "tokens_analyzed": len(results), "results": results}
