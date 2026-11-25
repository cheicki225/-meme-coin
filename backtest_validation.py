"""Chronological, out-of-sample research over caller-supplied OHLC datasets.

The input dataset digest verifies content integrity only. External provider
origin is not authenticated by this module, so performance metrics are exposed
only for datasets explicitly and structurally identified as synthetic.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from backtest_dataset import BacktestDatasetError, validate_dataset
from historical_backtest import run_backtest


class BacktestResearchError(ValueError):
    """Raised when a chronological research evaluation is malformed."""


SignalRule = Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any]], bool]


def _metrics(result: dict, *, initial_equity_usd: float) -> dict | None:
    """Compute finite, normalized metrics, or return None when no trades exist."""
    trades = result["trades"]
    if not trades:
        return None
    try:
        trade_pnls = [float(trade["net_pnl_usd"]) for trade in trades]
        fees = [float(trade["fees_usd"]) for trade in trades]
        if any(not math.isfinite(value) for value in (*trade_pnls, *fees)):
            raise BacktestResearchError("replay produced non-finite trade metrics")
        net_pnl = math.fsum(trade_pnls)
        gross_wins = math.fsum(max(0.0, value) for value in trade_pnls)
        gross_losses = math.fsum(abs(min(0.0, value)) for value in trade_pnls)
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else None
        drawdown = float(result["max_drawdown_usd"])
        win_rate = float(result["win_rate_pct"])
        total_fees = math.fsum(fees)
    except (KeyError, OverflowError, TypeError, ValueError) as exc:
        if isinstance(exc, BacktestResearchError):
            raise
        raise BacktestResearchError("replay metrics cannot be represented as finite numbers") from exc
    metrics = {
        "trade_count": len(trades),
        "net_pnl_usd": net_pnl,
        "roi_pct": net_pnl / initial_equity_usd * 100.0,
        "max_drawdown_usd": drawdown,
        "max_drawdown_pct": drawdown / initial_equity_usd * 100.0,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "profit_factor_basis": "net_trade_pnl_after_modeled_costs",
        "fees_usd": total_fees,
    }
    numeric_metrics = (
        net_pnl,
        net_pnl / initial_equity_usd * 100.0,
        drawdown,
        drawdown / initial_equity_usd * 100.0,
        win_rate,
        total_fees,
    )
    if any(not math.isfinite(value) for value in numeric_metrics):
        raise BacktestResearchError("replay produced non-finite aggregate metrics")
    if profit_factor is not None and not math.isfinite(profit_factor):
        raise BacktestResearchError("replay produced a non-finite profit factor")
    return metrics


def _validated_candidate(candidate: Mapping[str, Any], index: int) -> dict:
    if not isinstance(candidate, Mapping):
        raise BacktestResearchError(f"candidate {index} must be an object")
    allowed = {
        "take_profit_pct", "stop_loss_pct", "fee_bps", "slippage_bps", "signal_parameters",
    }
    if set(candidate) - allowed or not {"take_profit_pct", "stop_loss_pct"}.issubset(candidate):
        raise BacktestResearchError(f"candidate {index} has unsupported or missing fields")
    signal_parameters = candidate.get("signal_parameters", {})
    if not isinstance(signal_parameters, Mapping):
        raise BacktestResearchError(f"candidate {index} signal_parameters must be an object")
    return {
        "take_profit_pct": candidate["take_profit_pct"],
        "stop_loss_pct": candidate["stop_loss_pct"],
        "fee_bps": candidate.get("fee_bps", 100),
        "slippage_bps": candidate.get("slippage_bps", 100),
        "signal_parameters": dict(signal_parameters),
    }


def _signals_for_indices(
    candles: Sequence[dict],
    indices: Iterable[int],
    rule: SignalRule,
    signal_parameters: Mapping[str, Any],
) -> list[int]:
    signals = []
    immutable_parameters = MappingProxyType(dict(signal_parameters))
    for index in indices:
        # Each signal decision receives only the prefix known at that candle
        # close. It cannot inspect a future OHLC row through this interface.
        prefix = tuple(MappingProxyType(dict(row)) for row in candles[: index + 1])
        decision = rule(prefix, immutable_parameters)
        if type(decision) is not bool:
            raise BacktestResearchError("signal rule must return a bool for each candle close")
        if decision:
            signals.append(index)
    return signals


def optimize_and_evaluate(
    dataset: dict,
    signal_rule: SignalRule,
    candidates: Iterable[Mapping[str, Any]],
    *,
    split_index: int,
    embargo_bars: int = 1,
    initial_equity_usd: float = 1_000.0,
    position_usd: float = 100.0,
) -> dict:
    """Select one parameter set on the chronological training partition.

    Validation signals are generated one close at a time from a prefix that
    ends at the signal candle. Parameters are selected using training net PnL
    only; the validation partition is evaluated once with the selected set.
    Current repository metadata cannot authenticate external data providers,
    so external/unknown datasets return without performance metrics. A
    synthetic dataset may produce metrics, always labeled synthetic and never
    eligible as evidence of real-world profitability.
    """
    try:
        verified_dataset = validate_dataset(dataset)
    except BacktestDatasetError:
        raise
    candles = verified_dataset["candles"]
    synthetic = (
        verified_dataset["source"] == "synthetic"
        and verified_dataset["asset_id"].startswith("synthetic:")
    )
    dataset_summary = {
        "schema_version": verified_dataset["schema_version"],
        "asset_id": verified_dataset["asset_id"],
        "source": verified_dataset["source"],
        "sha256": verified_dataset["sha256"],
    }
    if not synthetic:
        return {
            "status": "UNKNOWN",
            "dataset_classification": "UNVERIFIED_EXTERNAL",
            "dataset": dataset_summary,
            "metrics": None,
            "selected_parameters": None,
            "performance_claim_eligible": False,
            "reason": "provider_origin_and_quote_freshness_not_authenticated",
        }

    if type(split_index) is not int or isinstance(split_index, bool):
        raise BacktestResearchError("split_index must be an integer candle index")
    if not callable(signal_rule):
        raise BacktestResearchError("signal_rule must be callable")
    if type(embargo_bars) is not int or isinstance(embargo_bars, bool) or embargo_bars < 0:
        raise BacktestResearchError("embargo_bars must be a non-negative integer")
    if (
        isinstance(initial_equity_usd, bool)
        or not isinstance(initial_equity_usd, (int, float))
        or not math.isfinite(float(initial_equity_usd))
        or initial_equity_usd <= 0
    ):
        raise BacktestResearchError("initial_equity_usd must be finite and positive")
    if (
        isinstance(position_usd, bool)
        or not isinstance(position_usd, (int, float))
        or not math.isfinite(float(position_usd))
        or position_usd <= 0
        or position_usd > initial_equity_usd
    ):
        raise BacktestResearchError("position_usd must be finite, positive, and within initial equity")
    train_end = split_index - embargo_bars
    if train_end < 3 or train_end > split_index or split_index >= len(candles) - 1:
        raise BacktestResearchError("split leaves too few chronological training or validation candles")

    try:
        candidate_list = [_validated_candidate(candidate, index) for index, candidate in enumerate(candidates)]
    except TypeError as exc:
        raise BacktestResearchError("candidates must be iterable") from exc
    if not candidate_list:
        raise BacktestResearchError("at least one parameter candidate is required")

    train_candles = candles[:train_end]
    validation_candles = candles[split_index:]
    train_best = None
    train_best_result = None
    train_best_score = None
    for index, candidate in enumerate(candidate_list):
        train_signals = _signals_for_indices(
            candles,
            range(0, train_end - 1),
            signal_rule,
            candidate["signal_parameters"],
        )
        train_result = run_backtest(
            train_candles,
            train_signals,
            position_usd=position_usd,
            take_profit_pct=candidate["take_profit_pct"],
            stop_loss_pct=candidate["stop_loss_pct"],
            fee_bps=candidate["fee_bps"],
            slippage_bps=candidate["slippage_bps"],
            expected_interval_s=verified_dataset["interval_s"],
            allowed_missing_candles=verified_dataset["allowed_missing_candles"],
        )
        score = train_result["net_pnl_usd"] if train_result["trade_count"] else None
        if score is not None and (train_best_score is None or score > train_best_score):
            train_best = (index, candidate)
            train_best_result = train_result
            train_best_score = score

    if train_best is None:
        return {
            "status": "NO_TRAINING_TRADES",
            "dataset_classification": "SYNTHETIC",
            "dataset": dataset_summary,
            "split": {"train_end_exclusive": train_end, "validation_start": split_index,
                      "embargo_bars": embargo_bars,
                      "train_start_timestamp": train_candles[0]["timestamp"],
                      "train_end_timestamp": train_candles[-1]["timestamp"],
                      "validation_start_timestamp": validation_candles[0]["timestamp"],
                      "validation_end_timestamp": validation_candles[-1]["timestamp"]},
            "training_metrics": None,
            "validation_metrics": None,
            "metrics": None,
            "selected_parameters": None,
            "performance_claim_eligible": False,
        }

    selected_index, selected = train_best
    assert train_best_result is not None
    validation_signals = _signals_for_indices(
        candles,
        range(split_index, len(candles) - 1),
        signal_rule,
        selected["signal_parameters"],
    )
    local_validation_signals = [signal - split_index for signal in validation_signals]
    validation_result = run_backtest(
        validation_candles,
        local_validation_signals,
        position_usd=position_usd,
        take_profit_pct=selected["take_profit_pct"],
        stop_loss_pct=selected["stop_loss_pct"],
        fee_bps=selected["fee_bps"],
        slippage_bps=selected["slippage_bps"],
        expected_interval_s=verified_dataset["interval_s"],
        allowed_missing_candles=verified_dataset["allowed_missing_candles"],
    )
    validation_metrics = _metrics(validation_result, initial_equity_usd=float(initial_equity_usd))
    return {
        "status": "SYNTHETIC_RESEARCH_ONLY",
        "dataset_classification": "SYNTHETIC",
        "dataset": dataset_summary,
        "split": {
            "train_end_exclusive": train_end,
            "validation_start": split_index,
            "embargo_bars": embargo_bars,
            "train_start_timestamp": train_candles[0]["timestamp"],
            "train_end_timestamp": train_candles[-1]["timestamp"],
            "validation_start_timestamp": validation_candles[0]["timestamp"],
            "validation_end_timestamp": validation_candles[-1]["timestamp"],
            "parameters_selected_from": "training_only",
            "validation_run_count": 1,
        },
        "selected_candidate_index": selected_index,
        "selected_parameters": {
            "take_profit_pct": selected["take_profit_pct"],
            "stop_loss_pct": selected["stop_loss_pct"],
            "fee_bps": selected["fee_bps"],
            "slippage_bps": selected["slippage_bps"],
            "signal_parameters": dict(selected["signal_parameters"]),
        },
        "training_metrics": _metrics(train_best_result, initial_equity_usd=float(initial_equity_usd)),
        "validation_metrics": validation_metrics,
        "metrics": validation_metrics,
        "fills": {
            "entry": "next candle open after close-only signal",
            "same_bar_tp_sl": "stop loss first",
            "fees": "fixed basis-point scenario on both sides",
            "slippage": "fixed adverse basis-point scenario on both sides",
            "network_fees_and_rent": "not modeled",
        },
        "performance_claim_eligible": False,
        "profitability_claim": "not_established_by_synthetic_or_ohlc_replay",
    }
