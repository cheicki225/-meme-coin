"""
════════════════════════════════════════════════════════════════
AI ADVISOR — Avis qualitatif Claude/Grok en appui du scoring
════════════════════════════════════════════════════════════════
Le backtest (analysis/backtest.py) et la régularité (analysis/
wallet_history.py) donnent des chiffres. Ce module demande à une IA
de leur donner du sens : est-ce que ce pattern semble exploitable,
y a-t-il un signal d'alerte que les chiffres ne montrent pas ?

IMPORTANT — ce n'est JAMAIS bloquant seul : l'avis IA s'ajoute au
verdict du backtest dans main.py, il ne le remplace pas. Le bot
continue de fonctionner normalement si aucune clé IA n'est configurée
(get_ai_verdict retourne alors None immédiatement, sans appel réseau).

Utilise Claude en priorité si ANTHROPIC_API_KEY est configurée,
sinon Grok si GROK_API_KEY est configurée. Aucune des deux : skip.
"""

import json
import logging
import aiohttp

import config
import rpc_client

log = logging.getLogger("ai_advisor")

SYSTEM_PROMPT = (
    "Tu es un analyste quantitatif spécialisé dans le trading de meme coins sur "
    "Solana (Pump.fun). On te donne les résultats d'un backtest statistique sur "
    "un wallet potentiellement lié à un \"rugger\" (créateur de tokens qui gonfle "
    "le prix puis dump). Ton rôle est de donner un avis QUALITATIF bref sur la "
    "fiabilité du pattern observé — pas de refaire le calcul, juste de repérer "
    "des signaux d'alerte ou de confiance que les chiffres bruts ne montrent pas "
    "forcément (échantillon trop petit, ratio gonflé par un seul trade extrême, "
    "cohérence du schéma de financement, etc.). "
    "Réponds UNIQUEMENT en JSON strict avec ce format exact : "
    '{"verdict": "GO"|"CAUTION"|"AVOID", "reasoning": "une phrase concise en français"}'
)


async def get_ai_verdict(dev_address: str, backtest_result: dict, regularity: dict, scheme: str) -> dict:
    """
    Retourne : {"verdict": "GO"|"CAUTION"|"AVOID", "reasoning": str, "provider": str}
    ou None si aucune clé IA n'est configurée (pas d'appel réseau dans ce cas).
    """
    # DÉSACTIVÉ suite à une demande explicite — coupé ici (point d'entrée
    # unique de ce module) plutôt que dans chaque appelant, pour garantir
    # qu'AUCUN appel à Claude/Grok ne se fait plus nulle part dans le bot,
    # que ce soit automatiquement (main.py, évaluation d'un nouveau dev) ou
    # manuellement (bouton "📈 Score IA" dans Telegram, qui affichera
    # simplement "Aucun avis IA disponible"). Ne dépend plus du toggle
    # IA ON/OFF ni des clés API présentes ou non — retour immédiat, sans
    # appel réseau, sans consommer de quota. Pour réactiver : supprimer ce
    # bloc (les 3 lignes ci-dessous) et redéployer.
    return None

    if not config.ANTHROPIC_API_KEY and not config.GROK_API_KEY:
        return None

    prompt = (
        f"Wallet: {dev_address[:8]}...\n"
        f"Schéma de financement: {scheme}\n"
        f"Nombre de tokens backtestés: {len(backtest_result.get('trades', []))}\n"
        f"Résultat cumulé: {backtest_result.get('total_result_pct', 0):+.1f}%\n"
        f"Ratio gain/perte: {backtest_result.get('ratio', 0):.2f}\n"
        f"Tokens exclus (bundle trop haut): {backtest_result.get('skipped_bundle', 0)}\n"
        f"Score de régularité: {regularity.get('regularity_score', 0):.2f} "
        f"(sur {regularity.get('tokens_analyzed', 0)} tokens)\n"
        f"TP suggéré: {backtest_result.get('suggested_tp_pct', 0):.0f}%"
    )

    if config.ANTHROPIC_API_KEY:
        result = await _call_anthropic(prompt)
        if result:
            result["provider"] = "anthropic"
            return result

    if config.GROK_API_KEY:
        result = await _call_grok(prompt)
        if result:
            result["provider"] = "grok"
            return result

    return None


async def _call_anthropic(prompt: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": config.AI_MODEL_ANTHROPIC,
        "max_tokens": 200,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug(f"Anthropic API statut {resp.status}")
                    return None
                data = await resp.json()
                text = data["content"][0]["text"]
                return _parse_verdict(text)
    except Exception as e:
        log.debug(f"Erreur appel Anthropic: {e}")
        return None


async def _call_grok(prompt: str) -> dict:
    # API xAI Grok — compatible format OpenAI Chat Completions
    url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.AI_MODEL_GROK,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        async with aiohttp.ClientSession(connector=rpc_client.get_http_connector(), connector_owner=False) as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug(f"Grok API statut {resp.status}")
                    return None
                data = await resp.json()
                text = data["choices"][0]["message"]["content"]
                return _parse_verdict(text)
    except Exception as e:
        log.debug(f"Erreur appel Grok: {e}")
        return None


def _parse_verdict(text: str) -> dict:
    """Parse la réponse JSON attendue, avec repli robuste si le modèle dévie du format."""
    text = text.strip()
    # Retire d'éventuels ```json ... ``` autour de la réponse
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(text)
        verdict = parsed.get("verdict", "CAUTION").upper()
        if verdict not in ("GO", "CAUTION", "AVOID"):
            verdict = "CAUTION"
        return {"verdict": verdict, "reasoning": parsed.get("reasoning", "")}
    except (json.JSONDecodeError, AttributeError):
        log.debug(f"Réponse IA non-JSON, repli CAUTION : {text[:100]}")
        return {"verdict": "CAUTION", "reasoning": "Réponse IA non structurée (voir logs)."}
