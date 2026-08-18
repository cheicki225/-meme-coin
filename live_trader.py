"""
════════════════════════════════════════════════════════════════
LIVE TRADER — Exécution réelle via Jupiter (argent réel)
════════════════════════════════════════════════════════════════
Hérite de PaperTrader pour réutiliser toute la logique de filtres
(market cap, sécurité GoPlus, comportement du dev, Max Loss Counter,
Buy The Dip, Trailing SL, etc.) et les notifications — déjà testées.
Seules les deux primitives d'exécution (_execute_buy / _execute_sell)
sont remplacées par de vrais appels Jupiter.

Si une vente échoue (réseau, slippage dépassé, etc.), la position reste
INCHANGÉE — aucune modification d'état sans confirmation on-chain réelle.
Le prochain cycle de _monitor_position retentera automatiquement.
"""

import logging
import asyncio

import config
import jupiter_executor
import rpc_client

log = logging.getLogger("live_trader")

from backtest import get_sol_usd_rate
from paper_trader import PaperTrader


async def _get_token_decimals(mint: str) -> int:
    """Récupère le nombre de décimales du token. Défaut 6 si indisponible —
    c'est la valeur la plus courante pour les tokens Pump.fun, mais PAS une
    garantie universelle SPL (le standard est 9 ailleurs) : à vérifier si un
    token se comporte de façon inattendue."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
    result = await rpc_client.rpc_post(payload, timeout=10)
    try:
        return int(result["value"]["decimals"])
    except (KeyError, TypeError, ValueError):
        log.warning(f"Décimales introuvables pour {mint[:8]}..., valeur par défaut 6 utilisée.")
        return 6


class LiveTrader(PaperTrader):
    def _get_execution_keypair(self):
        """
        Retourne le keypair à utiliser pour une exécution simple (non-MultiBuy) :
        priorité au wallet actif de wallet_manager.py (multi-wallet géré depuis
        Telegram) s'il y en a un, sinon retombe sur le wallet unique historique
        de wallet.py (SOLANA_PRIVATE_KEY).
        """
        try:
            from wallet_manager import WalletManager
            wm = WalletManager(self.data_store)
            if wm.get_active_label():
                return wm.get_active_keypair()
        except Exception as e:
            log.debug(f"Pas de wallet géré actif ({e}), retombe sur wallet.py")
        import wallet
        return wallet.load_keypair()

    async def _execute_buy(self, token_mint: str, entry_price: float, settings: dict) -> dict:
        if settings.get("multibuy_enabled") and settings.get("multibuy_wallet_labels"):
            return await self._execute_multibuy(token_mint, entry_price, settings)

        buy_amount_sol = settings.get("buy_amount_sol", 0.1)
        amount_lamports = int(buy_amount_sol * 1_000_000_000)
        slippage_bps = int(settings.get("buy_slippage_pct", 50) * 100)
        keypair = self._get_execution_keypair()
        use_jito = settings.get("use_jito", False)

        try:
            result = await jupiter_executor.execute_swap(
                config.SOL_MINT, token_mint, amount_lamports, slippage_bps, keypair=keypair, use_jito=use_jito
            )
        except jupiter_executor.SwapError as e:
            log.error(f"❌ Achat LIVE échoué sur {token_mint[:8]}...: {e}")
            if self.notifier:
                await self.notifier.notify(
                    "buy_failed",
                    f"❌ *Buy Failed* (LIVE)\nToken: `{token_mint[:8]}...`\nErreur: {e}",
                )
            return None

        decimals = await _get_token_decimals(token_mint)
        units_received = result["output_amount"] / (10 ** decimals)
        sol_spent = result["input_amount"] / 1_000_000_000
        cost_basis_usd = sol_spent * await get_sol_usd_rate()

        if units_received <= 0:
            log.error(f"❌ Achat LIVE sur {token_mint[:8]}... a renvoyé 0 token reçu — anomalie.")
            return None

        actual_price = cost_basis_usd / units_received

        log.info(
            f"💰 [LIVE] Achat réel confirmé : {sol_spent:.4f} SOL -> {units_received:.2f} tokens "
            f"(impact prix {result['price_impact_pct']:.2f}%) — tx {result['signature']}"
        )

        return {
            "units": units_received,
            "cost_basis_usd": cost_basis_usd,
            "actual_price": actual_price,
            "signature": result["signature"],
        }

    async def _execute_multibuy(self, token_mint: str, entry_price: float, settings: dict) -> dict:
        """
        Répartit l'achat total sur plusieurs wallets gérés (wallet_manager.py),
        avec un délai entre chaque, pour réduire le price impact/la détectabilité
        (méthode montrée dans les vidéos : diviser un gros achat en petits ordres
        depuis plusieurs wallets plutôt qu'un seul gros ordre visible).

        LIMITE : chaque sous-achat est indépendant (pas de coordination on-chain
        atomique) — si un sous-achat échoue en cours de route, les précédents
        restent acquis (partiels), ce qui est reflété fidèlement dans le résultat
        cumulé retourné plutôt que de tout annuler.
        """
        from wallet_manager import WalletManager
        wm = WalletManager(self.data_store)

        wallet_labels = settings.get("multibuy_wallet_labels", [])
        total_amount_sol = settings.get("buy_amount_sol", 0.1)
        delay_s = settings.get("multibuy_delay_s", 3)
        slippage_bps = int(settings.get("buy_slippage_pct", 50) * 100)

        n = len(wallet_labels)
        if n == 0:
            log.warning("MultiBuy activé mais aucun wallet configuré — achat annulé.")
            return None

        split_amount_sol = total_amount_sol / n
        amount_lamports = int(split_amount_sol * 1_000_000_000)

        total_units = 0.0
        total_sol_spent = 0.0
        signatures = []
        decimals = await _get_token_decimals(token_mint)

        for i, label in enumerate(wallet_labels):
            try:
                keypair = wm.get_keypair(label)
            except ValueError as e:
                log.warning(f"MultiBuy : wallet '{label}' introuvable ({e}), sous-achat sauté.")
                continue

            try:
                result = await jupiter_executor.execute_swap(
                    config.SOL_MINT, token_mint, amount_lamports, slippage_bps,
                    keypair=keypair, use_jito=settings.get("use_jito", False),
                )
                units = result["output_amount"] / (10 ** decimals)
                sol_spent = result["input_amount"] / 1_000_000_000
                total_units += units
                total_sol_spent += sol_spent
                signatures.append(result["signature"])
                log.info(f"💰 [MultiBuy {i+1}/{n}] '{label}' : {sol_spent:.4f} SOL -> {units:.2f} tokens")
            except jupiter_executor.SwapError as e:
                log.error(f"❌ [MultiBuy {i+1}/{n}] échec sur '{label}': {e}")

            if i < n - 1:
                await asyncio.sleep(delay_s)

        if total_units <= 0:
            log.error(f"❌ MultiBuy sur {token_mint[:8]}... — tous les sous-achats ont échoué.")
            if self.notifier:
                await self.notifier.notify(
                    "buy_failed",
                    f"❌ *Buy Failed* (MultiBuy, LIVE)\nToken: `{token_mint[:8]}...`\nTous les sous-achats ont échoué.",
                )
            return None

        cost_basis_usd = total_sol_spent * await get_sol_usd_rate()
        actual_price = cost_basis_usd / total_units

        log.info(
            f"✅ [MultiBuy] Terminé sur {token_mint[:8]}... — {len(signatures)}/{n} sous-achats "
            f"réussis, total {total_sol_spent:.4f} SOL -> {total_units:.2f} tokens"
        )

        return {
            "units": total_units,
            "cost_basis_usd": cost_basis_usd,
            "actual_price": actual_price,
            "signature": signatures[0] if signatures else None,  # signature principale pour l'affichage
        }

    async def _execute_sell(self, token_mint: str, units: float, current_price: float, settings: dict) -> dict:
        if units <= 0:
            return None

        decimals = await _get_token_decimals(token_mint)
        amount_raw = int(units * (10 ** decimals))
        if amount_raw <= 0:
            log.warning(f"Montant à vendre trop petit après conversion decimals pour {token_mint[:8]}...")
            return None

        slippage_bps = int(settings.get("buy_slippage_pct", 50) * 100)
        keypair = self._get_execution_keypair()

        try:
            result = await jupiter_executor.execute_swap(
                token_mint, config.SOL_MINT, amount_raw, slippage_bps,
                keypair=keypair, use_jito=settings.get("use_jito", False),
            )
        except jupiter_executor.SwapError as e:
            log.error(f"❌ Vente LIVE échouée sur {token_mint[:8]}...: {e}")
            return None

        sol_received = result["output_amount"] / 1_000_000_000
        proceeds_usd = sol_received * await get_sol_usd_rate()

        log.info(
            f"💰 [LIVE] Vente réelle confirmée : tokens -> {sol_received:.4f} SOL "
            f"(impact prix {result['price_impact_pct']:.2f}%) — tx {result['signature']}"
        )

        return {"proceeds_usd": proceeds_usd, "signature": result["signature"]}
