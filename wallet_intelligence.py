"""
Wallet Intelligence Engine: scoring explicable et fail-closed.
Module pur: aucune dépendance RPC. Les métriques absentes restent inconnues.
Le PnL réalisé doit provenir d'un coût de revient on-chain fiable, pas d'un backtest approximatif.
"""
from __future__ import annotations
import math
from typing import Any

_COMPONENTS = {
    "realized_pnl_pct": (0.30, -50.0, 150.0),
    "win_rate": (0.20, 0.0, 1.0),
    "profit_factor": (0.15, 0.0, 3.0),
    "consistency": (0.15, 0.0, 1.0),
    "early_entry_rate": (0.10, 0.0, 1.0),
    "sell_discipline": (0.10, 0.0, 1.0),
}

def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def _normalize(metric: str, value: float) -> float:
    _weight, low, high = _COMPONENTS[metric]
    return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))

def score_wallet(metrics: dict[str, Any] | None, relationships: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score 0-100, couverture, confiance et preuves; les relations restent descriptives.

    metrics.sample_counts doit donner le nombre d'observations par métrique.
    Une métrique sans échantillon positif est ignorée. Un financeur commun ou
    des achats groupés ne prouvent pas à eux seuls une activité malveillante.
    """
    metrics = metrics if isinstance(metrics, dict) else {}
    relationships = relationships if isinstance(relationships, dict) else {}
    sample_counts = metrics.get("sample_counts")
    if not isinstance(sample_counts, dict):
        sample_counts = {}

    components = {}
    weighted_total = available_weight = weighted_samples = 0.0
    for name, (weight, _low, _high) in _COMPONENTS.items():
        value = _finite_number(metrics.get(name))
        samples_value = _finite_number(sample_counts.get(name, metrics.get("sample_size", 0)))
        samples = max(0, int(samples_value or 0))
        if value is None or samples <= 0:
            components[name] = {"status": "unknown", "score": None, "samples": samples}
            continue
        normalized = _normalize(name, value)
        weighted_total += normalized * weight
        available_weight += weight
        weighted_samples += samples * weight
        components[name] = {
            "status": "measured", "value": value,
            "score": round(normalized, 2), "samples": samples,
        }

    if available_weight == 0:
        score, coverage, confidence = None, 0.0, "none"
    else:
        score = round(weighted_total / available_weight, 2)
        coverage = round(available_weight, 4)
        average_samples = weighted_samples / available_weight
        confidence_value = coverage * min(1.0, math.sqrt(average_samples / 20.0))
        confidence = "high" if confidence_value >= 0.75 else "medium" if confidence_value >= 0.40 else "low"

    measured = [item for item in components.values() if item["status"] == "measured"]
    total_samples = sum(item["samples"] for item in measured)
    if score is None or coverage < 0.40 or total_samples < 3:
        recommendation = "insufficient_data"
    elif score >= 70 and confidence in ("medium", "high"):
        recommendation = "follow"
    elif score >= 45:
        recommendation = "watch"
    else:
        recommendation = "avoid"

    funders = relationships.get("shared_funders")
    co_buy = relationships.get("co_buy_wallets")
    risk_flags = []
    if isinstance(funders, list) and funders:
        risk_flags.append("shared_funder_relationship")
    if isinstance(co_buy, list) and co_buy:
        risk_flags.append("co_buy_relationship")
    known_malicious_link = relationships.get("known_malicious_link") is True
    if known_malicious_link:
        risk_flags.append("known_malicious_link")
        # A confirmed malicious relationship must override any performance score.
        recommendation = "avoid"

    return {
        "wallet_address": metrics.get("wallet_address"),
        "score": score,
        "recommendation": recommendation,
        "confidence": confidence,
        "coverage": coverage,
        "measured_components": len(measured),
        "total_samples": total_samples,
        "components": components,
        "relationships": {
            "shared_funders": funders if isinstance(funders, list) else [],
            "co_buy_wallets": co_buy if isinstance(co_buy, list) else [],
        },
        "risk_flags": risk_flags,
        "scoring_version": 1,
    }
