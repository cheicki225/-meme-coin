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
import math
import time
from datetime import datetime, timezone
from typing import Any, TypeGuard

import config
import wallet_history
import backtest

log = logging.getLogger("rug_scanner")
CREATION_TIMESTAMP_SOURCE = "helius_enhanced_transaction.timestamp"


def _observed_at_utc() -> str:
    """Return the local observation time; this is not a provider timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_finite_number(value: Any) -> TypeGuard[int | float]:
    """Validate JSON numeric values without accepting booleans or overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def provider_creation_timestamp(info: dict[str, Any]) -> int | None:
    """Read the Enhanced Transactions Unix timestamp without inventing one."""
    raw = info.get("raw")
    timestamp = raw.get("timestamp") if isinstance(raw, dict) else None
    if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp > 0:
        return timestamp
    return None


def _filter_result(
    status: str,
    reason_code: str | None,
    reason: str | None,
    observed_at_utc: str,
    provenance: dict,
    **metrics,
) -> dict:
    """Build a result that separates filter outcome from source provenance."""
    return {
        "passed": status == "passed",
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "observed_at_utc": observed_at_utc,
        "freshness": "filter_source_timestamps_unavailable",
        "provenance": provenance,
        **metrics,
    }


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
                log.warning(f"⚠️ Erreur dans le rug scanner : {type(e).__name__}")
            await asyncio.sleep(config.RUG_SCAN_INTERVAL_S)

    def stop(self):
        self._running = False

    def register_candidate(
        self,
        token_mint: str,
        dev_address: str,
        created_at: int | None = None,
        created_at_source: str | None = None,
    ) -> None:
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
        if (
            isinstance(created_at, int)
            and not isinstance(created_at, bool)
            and created_at > 0
            and created_at_source == CREATION_TIMESTAMP_SOURCE
        ):
            candidate_time = created_at
            candidate_time_source = CREATION_TIMESTAMP_SOURCE
        else:
            # Missing or malformed source time is unknown; do not invent a
            # creation time from local observation and accidentally call it fresh.
            candidate_time = None
            candidate_time_source = None
        candidates[token_mint] = {
            "dev_address": dev_address,
            "created_at": candidate_time,
            "created_at_source": candidate_time_source,
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
            if not isinstance(info, dict):
                log.warning("rug scanner candidate state is unknown: invalid_candidate_state")
                continue
            notified = info.get("notified", False)
            if notified is True:
                continue
            if notified is not False:
                log.warning("rug scanner candidate state is unknown: invalid_notification_state")
                continue

            created_at = info.get("created_at")
            if (
                isinstance(created_at, bool)
                or not isinstance(created_at, int)
                or created_at <= 0
                or created_at > now
                or info.get("created_at_source") != CREATION_TIMESTAMP_SOURCE
                or not isinstance(info.get("dev_address"), str)
                or not info["dev_address"].strip()
            ):
                log.warning("rug scanner candidate state is unknown: invalid_candidate_timestamp_or_creator")
                continue

            age_hours = (now - created_at) / 3600
            if age_hours > config.RUG_SCAN_MAX_AGE_HOURS:
                # Trop vieux, jamais passé les filtres à temps — abandonné.
                self.data_store.state["rug_scan_candidates"].pop(token_mint, None)
                changed = True
                continue

            try:
                result = await self._check_filters(token_mint, info["dev_address"], age_hours)
            except Exception as exc:
                log.warning("rug scanner filter result is unknown: unexpected_%s", type(exc).__name__)
                continue

            if result.get("status") == "unknown":
                log.warning("rug scanner filter result is unknown: %s", result.get("reason_code", "unspecified"))
                continue

            result["candidate_creation_timestamp"] = {
                "source": CREATION_TIMESTAMP_SOURCE,
                "unit": "unix_seconds",
                "value": created_at,
            }

            if result.get("status") == "passed" and result.get("passed") is True:
                self.data_store.state["rug_scan_candidates"][token_mint]["notified"] = True
                changed = True
                await self._notify(token_mint, result)

        if changed:
            self.data_store.save()

    async def _check_filters(self, token_mint: str, dev_address: str, age_hours: float) -> dict:
        """Filter cheap-to-expensive and preserve unknown source states.

        The timestamp on the result is when this process observed the data;
        the called providers do not expose a freshness timestamp through these
        helpers. A positive result is therefore only a heuristic filter pass.
        """
        observed_at = _observed_at_utc()
        provenance: dict[str, dict[str, Any]] = {
            "developer_creations": {
                "source": "wallet_history.get_created_tokens",
                "status": "not_checked",
                "provider_timestamp_utc": None,
            },
            "token_signatures": {
                "source": "wallet_history.get_all_signatures_paginated",
                "status": "not_checked",
                "provider_timestamp_utc": None,
            },
            "pair_volume": {
                "source": "DexScreener pair data via backtest._get_pair_data",
                "status": "not_checked",
                "provider_timestamp_utc": None,
            },
        }

        if not _is_finite_number(age_hours) or age_hours < 0:
            return _filter_result(
                "unknown", "invalid_candidate_age", "Candidate age is unavailable or invalid.",
                observed_at, provenance,
            )

        # 1. Créations du dev (Helius, mais réutilise une fonction déjà
        #    existante et déjà rate-limitée proprement).
        try:
            created_tokens = await wallet_history.get_created_tokens(dev_address, max_results=10)
        except Exception as exc:
            provenance["developer_creations"]["status"] = "unknown"
            return _filter_result(
                "unknown", "developer_creation_fetch_failed",
                "Developer creation history could not be retrieved.", observed_at, provenance,
                source_error_type=type(exc).__name__,
            )
        if (
            not isinstance(created_tokens, list)
            or not created_tokens
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("token_mint"), str)
                or not item["token_mint"].strip()
                or not isinstance(item.get("signature"), str)
                or not item["signature"].strip()
                for item in created_tokens
            )
        ):
            provenance["developer_creations"]["status"] = "unknown"
            return _filter_result(
                "unknown", "developer_creation_history_unavailable",
                "Developer creation history is empty or malformed; the helper does not distinguish this from a fetch failure.",
                observed_at, provenance,
            )
        provenance["developer_creations"].update({"status": "observed", "count": len(created_tokens)})
        if len(created_tokens) > config.RUG_SCAN_MAX_DEV_CREATIONS:
            return _filter_result(
                "rejected", "developer_creation_limit_exceeded",
                f"Dev a créé {len(created_tokens)} token(s) (max {config.RUG_SCAN_MAX_DEV_CREATIONS})",
                observed_at, provenance, dev_creations=len(created_tokens), age_hours=age_hours,
            )

        # 2. Nombre de transactions (Helius, plusieurs appels mais
        #    seulement sur ce qui a survécu au filtre précédent).
        try:
            tx_count = await self._count_transactions(token_mint)
        except Exception as exc:
            provenance["token_signatures"]["status"] = "unknown"
            return _filter_result(
                "unknown", "token_signature_fetch_failed",
                "Token signature count could not be retrieved.", observed_at, provenance,
                dev_creations=len(created_tokens), age_hours=age_hours,
                source_error_type=type(exc).__name__,
            )
        if isinstance(tx_count, bool) or not isinstance(tx_count, int) or tx_count < 1:
            provenance["token_signatures"]["status"] = "unknown"
            return _filter_result(
                "unknown", "token_signature_count_unavailable",
                "Token signature count is empty or malformed.", observed_at, provenance,
                dev_creations=len(created_tokens), age_hours=age_hours,
            )
        provenance["token_signatures"].update({"status": "observed", "count": tx_count})
        if tx_count < config.RUG_SCAN_MIN_TX_COUNT:
            return _filter_result(
                "rejected", "token_signature_threshold_not_met",
                f"{tx_count} TX (min {config.RUG_SCAN_MIN_TX_COUNT})",
                observed_at, provenance, dev_creations=len(created_tokens), tx_count=tx_count,
                age_hours=age_hours,
            )

        # 3. Volume (DexScreener — le plus cher en requêtes externes,
        #    réservé aux survivants des 2 filtres précédents).
        try:
            pair_data = await backtest._get_pair_data(token_mint)
        except Exception as exc:
            provenance["pair_volume"]["status"] = "unknown"
            return _filter_result(
                "unknown", "pair_volume_fetch_failed",
                "DexScreener pair data could not be retrieved.", observed_at, provenance,
                dev_creations=len(created_tokens), tx_count=tx_count, age_hours=age_hours,
                source_error_type=type(exc).__name__,
            )
        volume = pair_data.get("volume") if isinstance(pair_data, dict) else None
        volume_24h = volume.get("h24") if isinstance(volume, dict) else None
        if (
            not _is_finite_number(volume_24h)
            or volume_24h < 0
        ):
            provenance["pair_volume"]["status"] = "unknown"
            return _filter_result(
                "unknown", "pair_volume_unavailable",
                "DexScreener pair volume is missing or malformed.", observed_at, provenance,
                dev_creations=len(created_tokens), tx_count=tx_count, age_hours=age_hours,
            )
        provenance["pair_volume"].update({"status": "observed", "volume_24h_usd": volume_24h})
        if volume_24h < config.RUG_SCAN_MIN_VOLUME_USD:
            return _filter_result(
                "rejected", "pair_volume_threshold_not_met",
                f"Volume ${volume_24h:,.0f} (min ${config.RUG_SCAN_MIN_VOLUME_USD:,.0f})",
                observed_at, provenance, dev_creations=len(created_tokens), tx_count=tx_count,
                volume_24h=volume_24h, age_hours=age_hours,
            )

        return _filter_result(
            "passed", None, None, observed_at, provenance, tx_count=tx_count,
            volume_24h=volume_24h, dev_creations=len(created_tokens), age_hours=age_hours,
        )

    async def _count_transactions(self, token_mint: str) -> int | None:
        """
        Compte les transactions sur ce token. Une seule page de 1000
        signatures suffit largement pour dépasser le seuil minimum
        (250 par défaut) — pas besoin de paginer plus loin juste pour
        confirmer qu'on est déjà au-dessus du seuil.
        """
        signatures = await wallet_history.get_all_signatures_paginated(token_mint, max_pages=1, page_size=1000)
        if not isinstance(signatures, list) or not signatures:
            return None
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("signature"), str)
            or not item["signature"].strip()
            for item in signatures
        ):
            return None
        return len(signatures)

    async def _notify(self, token_mint: str, result: dict):
        if not self.notifier:
            log.info("rug scanner candidate passed heuristic filters; no notifier configured")
            return
        text = (
            f"ℹ️ *Candidat passé les filtres heuristiques*\n`{token_mint}`\n\n"
            f"Âge (timestamp Helius Enhanced) : `{result['age_hours']:.1f}h`\n"
            f"Volume 24h : `${result['volume_24h']:,.0f}`\n"
            f"Transactions : `{result['tx_count']}`\n"
            f"Créations du dev : `{result['dev_creations']}`\n\n"
            f"Horodatage d'observation : `{result['observed_at_utc']}`\n"
            "Les horodatages des sources utilisées par les filtres ne sont pas disponibles dans ce chemin. "
            "Ces seuils heuristiques ne confirment pas la sécurité du token et ne prouvent pas l'absence de rug."
        )
        await self.notifier.notify("rug_scan_alert", text)
