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
import os
import time
import math

import config
import jupiter_executor
import rpc_client

log = logging.getLogger("live_trader")

from backtest import get_sol_usd_rate
from paper_trader import PaperTrader
from safety_runtime import mark_intent, preflight_buy, reserve_intent


async def _get_token_decimals(mint: str) -> int:
    """Récupère les décimales SPL; une réponse inconnue bloque l'exécution."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
    result = await rpc_client.rpc_post(payload, timeout=10)
    try:
        decimals = result["value"]["decimals"]
    except (KeyError, TypeError):
        raise ValueError(f"Impossible de vérifier les décimales du token {mint[:8]}...") from None
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 18:
        raise ValueError(f"Décimales SPL invalides pour {mint[:8]}...")
    return decimals


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class LiveTrader(PaperTrader):
    def _persist_safety_state(self):
        """Persist a safety-critical LIVE state transition or fail closed.

        DataStore.save_or_raise performs an atomic fsync+replace. The fallback
        keeps lightweight test doubles and external compatible stores usable.
        """
        saver = getattr(self.data_store, "save_or_raise", None)
        if callable(saver):
            saver()
        else:
            self.data_store.save()

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

    async def _get_wallet_balance_sol(self, keypair) -> float:
        """Lit le solde confirmé du wallet d'exécution. Échec = achat bloqué."""
        pubkey = str(keypair.pubkey())
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [pubkey, {"commitment": "confirmed"}],
        }
        result = await rpc_client.rpc_post(payload, timeout=10)
        try:
            lamports = int(result["value"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Unable to verify execution wallet balance") from None
        if lamports < 0:
            raise ValueError("Invalid execution wallet balance")
        return lamports / 1_000_000_000

    async def _build_live_safety_state(self, keypair) -> dict:
        """Construit un snapshot conservateur à partir de l'état + solde RPC."""
        state = self.data_store.state
        balance_sol = await self._get_wallet_balance_sol(keypair)
        open_positions = state.get("open_positions", []) or []

        exposure_sol = 0.0
        for position in open_positions:
            if not isinstance(position, dict):
                continue
            try:
                exposure_sol += max(0.0, float(position.get("total_sol_invested", 0.0)))
            except (TypeError, ValueError):
                raise ValueError("Invalid persisted position exposure") from None

        if "daily_pnl_sol" in state:
            daily_pnl_sol = float(state["daily_pnl_sol"])
        else:
            losses_usd = float(state.get("session_losses_usd", 0.0))
            sol_usd = float(await get_sol_usd_rate())
            if sol_usd <= 0:
                raise ValueError("Unable to verify SOL/USD rate for safety snapshot")
            daily_pnl_sol = -(max(0.0, losses_usd) / sol_usd)

        return {
            "balance_sol": balance_sol,
            "daily_pnl_sol": daily_pnl_sol,
            "open_positions": open_positions,
            "already_exposed_sol": exposure_sol,
        }

    def _emergency_stop_enabled(self, settings: dict) -> bool:
        """Source indépendante via env + config + réglage runtime."""
        return (
            _truthy(os.getenv("BOT_EMERGENCY_STOP", "0"))
            or bool(getattr(config, "EMERGENCY_STOP", False))
            or bool(settings.get("emergency_stop", False))
        )

    def _finalize_buy_accounting(self, token_mint: str, buy_result: dict):
        """Mark confirmed buy intents accounted only after the position is durable."""
        registry = self.data_store.state.get("safety_order_intents", {})
        if not isinstance(registry, dict):
            raise ValueError("Corrupt buy intent registry during accounting finalization")
        intent_ids = buy_result.get("buy_intent_ids") or [buy_result.get("buy_intent_id")]
        intent_ids = [item for item in intent_ids if isinstance(item, str) and item]
        if not intent_ids:
            raise ValueError("Missing buy intent identity during accounting finalization")
        for intent_id in intent_ids:
            record = registry.get(intent_id)
            if (not isinstance(record, dict) or record.get("status") != "confirmed"
                    or record.get("token_mint") != token_mint):
                raise ValueError("Confirmed buy intent mismatch; accounting remains blocked")
            record["accounting_status"] = "accounted"
            record["accounted_at"] = time.time()
        self._persist_safety_state()

    async def _reserve_live_buy_intent(
        self,
        *,
        token_mint: str,
        entry_price: float,
        amount_sol: float,
        settings: dict,
        keypair,
    ):
        """Préflight + persistance AVANT tout envoi réseau vers Jupiter/Jito."""
        source_wallet = str(keypair.pubkey())
        intent_registry = self.data_store.state.get("safety_order_intents", {})
        if not isinstance(intent_registry, dict):
            raise ValueError("Corrupt buy intent registry; refusing LIVE buy")
        if any(
            isinstance(item, dict)
            and item.get("side") == "buy"
            and item.get("token_mint") == token_mint
            and item.get("status") == "confirmed"
            and item.get("accounting_status") != "accounted"
            for item in intent_registry.values()
        ):
            raise ValueError("A confirmed buy for this token still requires position accounting")
        unresolved_buys = self.data_store.state.get("safety_unaccounted_confirmed_buys", {})
        if isinstance(unresolved_buys, dict) and any(
            isinstance(item, dict) and item.get("token_mint") == token_mint
            for item in unresolved_buys.values()
        ):
            raise ValueError("Confirmed buy awaits accounting reconciliation for this token")
        safety_state = await self._build_live_safety_state(keypair)

        # Le prix d'entrée fait partie de l'événement : un retry du même signal
        # produit la même clé, tandis qu'un DCA à un autre prix reste distinct.
        event_id = f"{token_mint}:{entry_price:.12g}"
        intent = preflight_buy(
            token_mint=token_mint,
            amount_sol=amount_sol,
            source_wallet=source_wallet,
            settings=settings,
            state=safety_state,
            event_id=event_id,
            emergency_stop=self._emergency_stop_enabled(settings),
        )
        reserve_intent(self.data_store.state, intent)
        # Fail-closed : si la persistance échoue, aucune transaction n'est envoyée.
        self._persist_safety_state()
        return intent

    def _mark_live_intent(self, intent, status: str, *, signature=None, error=None):
        mark_intent(
            self.data_store.state,
            intent.idempotency_key,
            status,
            signature=signature,
            error=error,
        )
        if status == "confirmed" and getattr(intent, "side", "buy") == "buy":
            self.data_store.state["safety_order_intents"][intent.idempotency_key][
                "accounting_status"
            ] = "awaiting_accounting"
        self._persist_safety_state()

    def _persist_confirmed_buy_reconciliation(self, *, intent, token_mint, source_wallet, result, error):
        """Keep a confirmed swap recoverable when local accounting cannot finish."""
        signature = result.get("signature") if isinstance(result, dict) else None
        key = signature or intent.idempotency_key
        registry = self.data_store.state.setdefault("safety_unaccounted_confirmed_buys", {})
        registry[key] = {
            "status": "needs_accounting_reconciliation",
            "token_mint": token_mint,
            "source_wallet": source_wallet,
            "signature": signature,
            "intent_idempotency_key": intent.idempotency_key,
            "input_amount_raw": result.get("input_amount") if isinstance(result, dict) else None,
            "output_amount_raw": result.get("output_amount") if isinstance(result, dict) else None,
            "price_impact_pct": result.get("price_impact_pct") if isinstance(result, dict) else None,
            "created_at": time.time(),
            "error": str(error)[:500],
        }
        self._persist_safety_state()
        log.critical(
            f"CONFIRMED BUY REQUIRES ACCOUNTING RECONCILIATION: token={token_mint} "
            f"signature={signature}; further buys for this token are blocked."
        )

    async def _execute_buy(self, token_mint: str, entry_price: float, settings: dict) -> dict:
        if settings.get("multibuy_enabled") and settings.get("multibuy_wallet_labels"):
            return await self._execute_multibuy(token_mint, entry_price, settings)

        buy_amount_sol = settings.get("buy_amount_sol", 0.1)
        amount_lamports = int(buy_amount_sol * 1_000_000_000)
        slippage_bps = int(settings.get("buy_slippage_pct", 50) * 100)
        keypair = self._get_execution_keypair()
        use_jito = settings.get("use_jito", False)

        try:
            intent = await self._reserve_live_buy_intent(
                token_mint=token_mint,
                entry_price=entry_price,
                amount_sol=buy_amount_sol,
                settings=settings,
                keypair=keypair,
            )
        except (ValueError, TypeError) as e:
            log.error(f"🛑 Achat LIVE bloqué par le safety preflight sur {token_mint[:8]}...: {e}")
            if self.notifier:
                await self.notifier.notify(
                    "buy_failed",
                    f"🛑 *Buy Blocked* (LIVE safety)\nToken: `{token_mint[:8]}...`\nRaison: {e}",
                )
            return None

        try:
            result = await jupiter_executor.execute_swap(
                config.SOL_MINT, token_mint, amount_lamports, slippage_bps, keypair=keypair, use_jito=use_jito
            )
        except jupiter_executor.SwapOutcomeUnknown as e:
            self._mark_live_intent(intent, "unknown", error=str(e))
            log.critical(f"🛑 Achat LIVE à réconcilier sur {token_mint[:8]}...; aucun nouvel essai automatique: {e}")
            return None
        except jupiter_executor.SwapError as e:
            self._mark_live_intent(intent, "failed", error=str(e))
            log.error(f"❌ Achat LIVE échoué sur {token_mint[:8]}...: {e}")
            if self.notifier:
                await self.notifier.notify(
                    "buy_failed",
                    f"❌ *Buy Failed* (LIVE)\nToken: `{token_mint[:8]}...`\nErreur: {e}",
                )
            return None
        except Exception as e:
            # Ambigu : on ne sait pas si le provider a reçu/envoyé la tx.
            # Garder l'intent bloqué empêche un double achat après redémarrage.
            self._mark_live_intent(intent, "unknown", error=str(e))
            log.exception(f"🛑 État d'achat LIVE ambigu sur {token_mint[:8]}... — retry automatique bloqué.")
            return None

        # La confirmation on-chain est durablement enregistrée AVANT toute
        # lecture de métadonnées/prix. Une panne d'indexation ne doit jamais
        # faire disparaître un achat confirmé ni autoriser un nouvel achat.
        self._mark_live_intent(intent, "confirmed", signature=result.get("signature"))
        try:
            decimals = await _get_token_decimals(token_mint)
            raw_output = result["output_amount"]
            raw_input = result["input_amount"]
            if (not isinstance(raw_output, int) or isinstance(raw_output, bool) or raw_output <= 0
                    or not isinstance(raw_input, int) or isinstance(raw_input, bool) or raw_input <= 0):
                raise ValueError("Montants bruts du swap confirmé invalides")
            units_received = raw_output / (10 ** decimals)
            sol_spent = raw_input / 1_000_000_000
            sol_usd = float(await get_sol_usd_rate())
            if not math.isfinite(sol_usd) or sol_usd <= 0:
                raise ValueError("Taux SOL/USD indisponible ou invalide après confirmation")
            cost_basis_usd = sol_spent * sol_usd
            if not math.isfinite(units_received) or units_received <= 0 or not math.isfinite(cost_basis_usd):
                raise ValueError("Comptabilité invalide pour le swap confirmé")
            actual_price = cost_basis_usd / units_received
            if not math.isfinite(actual_price) or actual_price <= 0:
                raise ValueError("Prix d'entrée calculé invalide")
        except Exception as e:
            self._persist_confirmed_buy_reconciliation(
                intent=intent, token_mint=token_mint, source_wallet=str(keypair.pubkey()),
                result=result, error=e,
            )
            return None

        log.info(
            f"💰 [LIVE] Achat réel confirmé : {sol_spent:.4f} SOL -> {units_received:.2f} tokens "
            f"(impact prix {result['price_impact_pct']:.2f}%) — tx {result['signature']}"
        )

        return {
            "units": units_received,
            "cost_basis_usd": cost_basis_usd,
            "actual_price": actual_price,
            "signature": result["signature"],
            "buy_intent_id": intent.idempotency_key,
            "buy_intent_ids": [intent.idempotency_key],
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

        try:
            max_position_sol = float(settings.get("max_position_sol", 0.5))
            if total_amount_sol <= 0 or total_amount_sol > max_position_sol:
                raise ValueError("MultiBuy total exceeds max position size")
        except (TypeError, ValueError) as e:
            log.error(f"🛑 MultiBuy bloqué par le safety preflight: {e}")
            return None

        split_amount_sol = total_amount_sol / n
        amount_lamports = int(split_amount_sol * 1_000_000_000)

        total_units = 0.0
        total_sol_spent = 0.0
        signatures = []
        confirmed_legs = []
        decimals = await _get_token_decimals(token_mint)

        for i, label in enumerate(wallet_labels):
            try:
                keypair = wm.get_keypair(label)
            except ValueError as e:
                log.warning(f"MultiBuy : wallet '{label}' introuvable ({e}), sous-achat sauté.")
                continue

            try:
                intent = await self._reserve_live_buy_intent(
                    token_mint=token_mint,
                    entry_price=entry_price,
                    amount_sol=split_amount_sol,
                    settings=settings,
                    keypair=keypair,
                )
            except (ValueError, TypeError) as e:
                log.error(f"🛑 [MultiBuy {i+1}/{n}] '{label}' bloqué par safety: {e}")
                continue

            result = None
            try:
                result = await jupiter_executor.execute_swap(
                    config.SOL_MINT, token_mint, amount_lamports, slippage_bps,
                    keypair=keypair, use_jito=settings.get("use_jito", False),
                )
                self._mark_live_intent(intent, "confirmed", signature=result.get("signature"))
                confirmed_legs.append((intent, str(keypair.pubkey()), result))
                units = result["output_amount"] / (10 ** decimals)
                sol_spent = result["input_amount"] / 1_000_000_000
                total_units += units
                total_sol_spent += sol_spent
                signatures.append(result["signature"])
                log.info(f"💰 [MultiBuy {i+1}/{n}] '{label}' : {sol_spent:.4f} SOL -> {units:.2f} tokens")
            except jupiter_executor.SwapOutcomeUnknown as e:
                self._mark_live_intent(intent, "unknown", error=str(e))
                log.critical(f"🛑 [MultiBuy {i+1}/{n}] '{label}' à réconcilier; aucun nouvel essai: {e}")
            except jupiter_executor.SwapError as e:
                self._mark_live_intent(intent, "failed", error=str(e))
                log.error(f"❌ [MultiBuy {i+1}/{n}] échec sur '{label}': {e}")
            except Exception as e:
                record = self.data_store.state.get("safety_order_intents", {}).get(intent.idempotency_key, {})
                if isinstance(record, dict) and record.get("status") == "confirmed":
                    self._persist_confirmed_buy_reconciliation(
                        intent=intent, token_mint=token_mint, source_wallet=str(keypair.pubkey()),
                        result=result, error=e,
                    )
                else:
                    self._mark_live_intent(intent, "unknown", error=str(e))
                log.exception(
                    f"🛑 [MultiBuy {i+1}/{n}] échec post-exécution/état ambigu sur '{label}' — aucune relance."
                )

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

        try:
            sol_usd = float(await get_sol_usd_rate())
            if not math.isfinite(sol_usd) or sol_usd <= 0:
                raise ValueError("Taux SOL/USD indisponible ou invalide après MultiBuy confirmé")
            cost_basis_usd = total_sol_spent * sol_usd
            actual_price = cost_basis_usd / total_units
            if not math.isfinite(cost_basis_usd) or not math.isfinite(actual_price) or actual_price <= 0:
                raise ValueError("Comptabilité invalide pour MultiBuy confirmé")
        except Exception as e:
            for confirmed_intent, source_wallet, confirmed_result in confirmed_legs:
                self._persist_confirmed_buy_reconciliation(
                    intent=confirmed_intent, token_mint=token_mint, source_wallet=source_wallet,
                    result=confirmed_result, error=e,
                )
            return None

        log.info(
            f"✅ [MultiBuy] Terminé sur {token_mint[:8]}... — {len(signatures)}/{n} sous-achats "
            f"réussis, total {total_sol_spent:.4f} SOL -> {total_units:.2f} tokens"
        )

        return {
            "units": total_units,
            "cost_basis_usd": cost_basis_usd,
            "actual_price": actual_price,
            "signature": signatures[0] if signatures else None,
            "buy_intent_ids": [leg[0].idempotency_key for leg in confirmed_legs],
        }

    async def _execute_sell(self, token_mint: str, units: float, current_price: float, settings: dict) -> dict:
        try:
            units = float(units)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(units) or units <= 0:
            return None

        unresolved_sells = self.data_store.state.get("safety_unresolved_sell_tokens", {})
        unaccounted_sells = self.data_store.state.get("safety_unaccounted_confirmed_sells", {})
        pending_sells = self.data_store.state.get("safety_pending_sell_tokens", {})
        if (not isinstance(unresolved_sells, dict) or not isinstance(unaccounted_sells, dict)
                or not isinstance(pending_sells, dict)):
            log.critical("Registre de ventes corrompu — vente bloquée par sécurité.")
            return None
        if (token_mint in unresolved_sells or token_mint in pending_sells
                or any(isinstance(item, dict) and item.get("token_mint") == token_mint
                       for item in unaccounted_sells.values())):
            log.critical(
                f"🛑 Vente bloquée pour {token_mint[:8]}...: une vente précédente est en cours, ambiguë "
                "ou confirmée mais non comptabilisée; réconciliation requise."
            )
            return None

        decimals = await _get_token_decimals(token_mint)
        amount_raw = int(units * (10 ** decimals))
        if amount_raw <= 0:
            log.warning(f"Montant à vendre trop petit après conversion decimals pour {token_mint[:8]}...")
            return None

        slippage_bps = int(settings.get("buy_slippage_pct", 50) * 100)
        keypair = self._get_execution_keypair()

        # Persist a sell intent before any network submission. A restart after
        # this point must reconcile the signature/balance before another sell.
        pending_registry = self.data_store.state.setdefault("safety_pending_sell_tokens", {})
        if not isinstance(pending_registry, dict) or token_mint in pending_registry:
            log.critical("Registre de ventes en cours invalide ou déjà réservé; vente bloquée.")
            return None
        sell_intent_id = f"{token_mint}:{time.time_ns()}"
        pending_registry[token_mint] = {
            "intent_id": sell_intent_id,
            "status": "submitting",
            "units": units,
            "amount_raw": amount_raw,
            "source_wallet": str(keypair.pubkey()),
            "created_at": time.time(),
        }
        self._persist_safety_state()

        try:
            result = await jupiter_executor.execute_swap(
                token_mint, config.SOL_MINT, amount_raw, slippage_bps,
                keypair=keypair, use_jito=settings.get("use_jito", False),
            )
        except jupiter_executor.SwapOutcomeUnknown as e:
            registry = self.data_store.state.setdefault("safety_unresolved_sell_tokens", {})
            registry[token_mint] = {
                "status": "unknown",
                "intent_id": sell_intent_id,
                "units": units,
                "created_at": time.time(),
                "error": str(e)[:500],
            }
            pending_registry[token_mint]["status"] = "unknown"
            pending_registry[token_mint]["error"] = str(e)[:500]
            self._persist_safety_state()
            log.critical(
                f"🛑 Vente LIVE ambiguë sur {token_mint[:8]}...; token bloqué jusqu'à réconciliation: {e}"
            )
            return None
        except jupiter_executor.SwapError as e:
            # SwapError denotes a definite rejected/failed execution; only this
            # explicit outcome releases the pre-send reservation.
            pending_registry.pop(token_mint, None)
            self._persist_safety_state()
            log.error(f"❌ Vente LIVE échouée sur {token_mint[:8]}...: {e}")
            return None
        except Exception as e:
            # Toute erreur inattendue après le début de l'envoi est ambiguë :
            # bloquer le token plutôt que risquer une seconde vente au prochain poll.
            registry = self.data_store.state.setdefault("safety_unresolved_sell_tokens", {})
            if not isinstance(registry, dict):
                log.critical("Registre de ventes ambiguës corrompu; arrêt fail-closed.")
                return None
            registry[token_mint] = {
                "status": "unknown",
                "intent_id": sell_intent_id,
                "units": units,
                "created_at": time.time(),
                "error": str(e)[:500],
            }
            pending_registry[token_mint]["status"] = "unknown"
            pending_registry[token_mint]["error"] = str(e)[:500]
            self._persist_safety_state()
            log.exception(
                f"🛑 Vente LIVE à résultat inconnu sur {token_mint[:8]}...; "
                "nouvel envoi bloqué jusqu'à réconciliation."
            )
            return None

        # Le swap est confirmé. Si le prix ou les montants ne peuvent pas être
        # comptabilisés, conserver la preuve et bloquer toute nouvelle vente
        # de ce mint : le prochain poll ne doit jamais renvoyer les mêmes tokens.
        signature = result.get("signature")
        pending_registry[token_mint].update({
            "status": "confirmed_awaiting_accounting",
            "signature": signature,
            "input_amount_raw": result.get("input_amount"),
            "output_amount_raw": result.get("output_amount"),
            "confirmed_at": time.time(),
        })
        self._persist_safety_state()
        try:
            raw_output = result.get("output_amount")
            raw_input = result.get("input_amount")
            if (not isinstance(raw_output, int) or isinstance(raw_output, bool) or raw_output <= 0
                    or not isinstance(raw_input, int) or isinstance(raw_input, bool) or raw_input <= 0):
                raise ValueError("Montants bruts du swap de vente confirmée invalides")
            sol_received = raw_output / 1_000_000_000
            sol_usd = float(await get_sol_usd_rate())
            if not math.isfinite(sol_usd) or sol_usd <= 0:
                raise ValueError("Taux SOL/USD indisponible après vente confirmée")
            proceeds_usd = sol_received * sol_usd
            if not math.isfinite(proceeds_usd) or proceeds_usd <= 0:
                raise ValueError("Produit de vente calculé invalide")
        except Exception as e:
            registry = self.data_store.state.setdefault("safety_unaccounted_confirmed_sells", {})
            key = signature or f"{token_mint}:{time.time_ns()}"
            registry[key] = {
                "status": "needs_accounting_reconciliation",
                "token_mint": token_mint,
                "source_wallet": str(keypair.pubkey()),
                "signature": signature,
                "units_requested": units,
                "input_amount_raw": result.get("input_amount"),
                "output_amount_raw": result.get("output_amount"),
                "token_decimals": decimals,
                "created_at": time.time(),
                "error": str(e)[:500],
            }
            self._persist_safety_state()
            log.critical(
                f"CONFIRMED SELL REQUIRES ACCOUNTING RECONCILIATION: token={token_mint} "
                f"signature={signature}; further sells for this token are blocked."
            )
            return None

        log.info(
            f"💰 [LIVE] Vente réelle confirmée : tokens -> {sol_received:.4f} SOL "
            f"(impact prix {result.get('price_impact_pct', 0):.2f}%) — tx {signature}"
        )

        return {
            "proceeds_usd": proceeds_usd,
            "sol_received": sol_received,
            "signature": signature,
            "sell_intent_id": sell_intent_id,
        }

    def _finalize_sell_intent(self, token_mint: str, sell_intent_id: str) -> bool:
        """Release a confirmed sell reservation only after position accounting is saved."""
        registry = self.data_store.state.get("safety_pending_sell_tokens", {})
        if not isinstance(registry, dict):
            log.critical("Pending sell registry corrupted; reservation retained.")
            return False
        record = registry.get(token_mint)
        if not isinstance(record, dict) or record.get("intent_id") != sell_intent_id:
            log.critical("Sell intent mismatch during accounting finalization; reservation retained.")
            return False
        if record.get("status") != "confirmed_awaiting_accounting":
            log.critical("Sell intent is not confirmed; reservation retained.")
            return False
        registry.pop(token_mint, None)
        self._persist_safety_state()
        return True
