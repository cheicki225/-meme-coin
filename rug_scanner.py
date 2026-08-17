"""
════════════════════════════════════════════════════════════════
SCANNER DE RUGS — tous les nouveaux tokens Pump.fun, pas juste ceux liés
à un wallet suivi
════════════════════════════════════════════════════════════════
AJOUTÉ suite à une demande explicite. Contrairement au pipeline existant
dans main.py (qui évalue un dev seulement s'il est déjà monitoré ou passe
par _evaluate_new_dev avec ses propres critères — historique, ratio
backtest, régularité), ce module tourne EN PARALLÈLE et filtre TOUS les
tokens détectés selon 4 critères simples et indépendants :

  - Volume minimum        : 20 000 $ (RUG_SCAN_MIN_VOLUME_USD)
  - Créations du dev      : maximum 3, ce token inclus (RUG_SCAN_MAX_DEV_CREATIONS)
  - Âge du token          : maximum 24h (RUG_SCAN_MAX_AGE_HOURS)
  - Nombre de transactions: minimum 250 (RUG_SCAN_MIN_TX_COUNT)

Comme un token FRAÎCHEMENT créé n'a évidemment pas encore 250 TX ni 20K$
de volume, ce scanner NE VÉRIFIE PAS une seule fois à la détection — il
garde chaque token détecté en mémoire et le RE-vérifie PÉRIODIQUEMENT
(RUG_SCAN_INTERVAL_S) jusqu'à ce qu'il passe les 4 filtres (notification
envoyée une seule fois) ou dépasse RUG_SCAN_MAX_AGE_HOURS (abandonné).

Philosophie de filtrage : DU MOINS COÛTEUX AU PLUS COÛTEUX en appels
réseau — un token qui échoue tôt (ex: dev trop prolifique) ne déclenche
jamais les vérifications plus chères (ex: DexScreener pour le volume).

Dépendance importante : ce scanner reçoit ses candidats via
register_candidate(), appelé depuis main.py à CHAQUE token détecté par
le MÊME listener WebSocket que le pipeline principal — pas de deuxième
connexion dédiée. Ça veut dire concrètement que RUG_SCAN_ENABLED=true
seul ne suffit pas : il faut AUSSI DETECTION_ENABLED=true pour que des
tokens soient effectivement détectés et transmis à ce scanner.
"""

import asyncio
import logging
import time

import config
import wallet_history
import backtest

log = logging.getLogger("rug_scanner")


class RugScanner:
    def __init__(self, data_store, notifier=None):
        self.data_store = data_store
        self.notifier = notifier
        self._running = False
        self._task = None

    async def start(self):
        if not config.RUG_SCAN_ENABLED:
            log.info("⏸️  Rug scanner désactivé (config.RUG_SCAN_ENABLED=false).")
            return
        self._running = True
        log.info(
            f"🔎 Rug scanner démarré — filtres : volume≥${config.RUG_SCAN_MIN_VOLUME_USD:,.0f}, "
            f"créations dev≤{config.RUG_SCAN_MAX_DEV_CREATIONS}, âge≤{config.RUG_SCAN_MAX_AGE_HOURS}h, "
            f"TX≥{config.RUG_SCAN_MIN_TX_COUNT}."
        )
        while self._running:
            try:
                await self._scan_all_candidates()
            except Exception as e:
                log.warning(f"⚠️ Erreur dans le rug scanner : {e}")
            await asyncio.sleep(config.RUG_SCAN_INTERVAL_S)

    def stop(self):
        self._running = False

    def register_candidate(self, token_mint: str, dev_address: str, created_at: int = None):
        """
        Appelé depuis main.py à CHAQUE nouveau token détecté (via le
        listener WebSocket existant), AVANT toute logique d'évaluation
        dev — ce scanner est indépendant du reste du pipeline.
        """
        if not config.RUG_SCAN_ENABLED:
            return
        candidates = self.data_store.state.setdefault("rug_scan_candidates", {})
        if token_mint in candidates:
            return  # déjà suivi
        candidates[token_mint] = {
            "dev_address": dev_address,
            "created_at": created_at or int(time.time()),
            "notified": False,
        }
        self.data_store.save()

    async def _scan_all_candidates(self):
        candidates = dict(self.data_store.state.get("rug_scan_candidates", {}))
        if not candidates:
            return

        now = int(time.time())
        changed = False

        for token_mint, info in candidates.items():
            if info.get("notified"):
                continue

            age_hours = (now - info["created_at"]) / 3600
            if age_hours > config.RUG_SCAN_MAX_AGE_HOURS:
                # Trop vieux, jamais passé les filtres à temps — abandonné.
                self.data_store.state["rug_scan_candidates"].pop(token_mint, None)
                changed = True
                continue

            try:
                result = await self._check_filters(token_mint, info["dev_address"], age_hours)
            except Exception as e:
                log.warning(f"⚠️ Erreur vérification filtres pour {token_mint[:12]}...: {e}")
                continue

            if result["passed"]:
                self.data_store.state["rug_scan_candidates"][token_mint]["notified"] = True
                changed = True
                await self._notify(token_mint, result)

        if changed:
            self.data_store.save()

    async def _check_filters(self, token_mint: str, dev_address: str, age_hours: float) -> dict:
        """Filtre du moins cher au plus cher — s'arrête au premier échec,
        pour économiser le quota Helius/DexScreener."""
        # 1. Créations du dev (Helius, mais réutilise une fonction déjà
        #    existante et déjà rate-limitée proprement).
        created_tokens = await wallet_history.get_created_tokens(dev_address, max_results=10)
        if len(created_tokens) > config.RUG_SCAN_MAX_DEV_CREATIONS:
            return {"passed": False, "reason": f"Dev a créé {len(created_tokens)} token(s) "
                                                f"(max {config.RUG_SCAN_MAX_DEV_CREATIONS})"}

        # 2. Nombre de transactions (Helius, plusieurs appels mais
        #    seulement sur ce qui a survécu au filtre précédent).
        tx_count = await self._count_transactions(token_mint)
        if tx_count < config.RUG_SCAN_MIN_TX_COUNT:
            return {"passed": False, "reason": f"{tx_count} TX (min {config.RUG_SCAN_MIN_TX_COUNT})"}

        # 3. Volume (DexScreener — le plus cher en requêtes externes,
        #    réservé aux survivants des 2 filtres précédents).
        pair_data = await backtest._get_pair_data(token_mint)
        volume_24h = float((pair_data.get("volume") or {}).get("h24", 0) or 0)
        if volume_24h < config.RUG_SCAN_MIN_VOLUME_USD:
            return {"passed": False, "reason": f"Volume ${volume_24h:,.0f} "
                                                f"(min ${config.RUG_SCAN_MIN_VOLUME_USD:,.0f})"}

        return {
            "passed": True, "tx_count": tx_count, "volume_24h": volume_24h,
            "dev_creations": len(created_tokens), "age_hours": age_hours,
        }

    async def _count_transactions(self, token_mint: str) -> int:
        """
        Compte les transactions sur ce token. Une seule page de 1000
        signatures suffit largement pour dépasser le seuil minimum
        (250 par défaut) — pas besoin de paginer plus loin juste pour
        confirmer qu'on est déjà au-dessus du seuil.
        """
        signatures = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=1, page_size=1000)
        return len(signatures or [])

    async def _notify(self, token_mint: str, result: dict):
        if not self.notifier:
            log.info(f"🚨 Rug potentiel (pas de notifier configuré) : {token_mint}")
            return
        text = (
            f"🚨 *Rug potentiel détecté*\n`{token_mint}`\n\n"
            f"Âge : `{result['age_hours']:.1f}h`\n"
            f"Volume 24h : `${result['volume_24h']:,.0f}`\n"
            f"Transactions : `{result['tx_count']}`\n"
            f"Créations du dev : `{result['dev_creations']}`\n\n"
            f"_Correspond aux 4 filtres configurés._"
        )
        await self.notifier.notify("rug_scan_alert", text)
