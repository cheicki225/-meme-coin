"""Deterministic OHLC replay with fees, slippage, and conservative fills.

This engine requires caller-supplied historical candles. It never requests live
prices or RPC data, and signals are filled at the next candle open so the
signal candle's future prices cannot leak into the entry decision.
"""
from __future__ import annotations

from math import isfinite
from typing import Iterable


class BacktestInputError(ValueError):
    """Raised when candle data or strategy parameters are invalid."""


def _finite_number(value, field: str) -> float:
    """Accept JSON-style numeric values only, excluding bools and strings."""
    if type(value) not in (int, float):
        raise BacktestInputError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise BacktestInputError(f"{field} must be a finite number") from exc
    if not isfinite(number):
        raise BacktestInputError(f"{field} must be a finite number")
    return number


def _validate_candles(
    candles: list[dict], *, expected_interval_s: int | None = None, allowed_missing_candles: int = 0
) -> list[dict]:
    if expected_interval_s is not None and (
        type(expected_interval_s) is not int or expected_interval_s <= 0
    ):
        raise BacktestInputError("expected_interval_s must be a positive integer")
    if type(allowed_missing_candles) is not int or allowed_missing_candles < 0:
        raise BacktestInputError("allowed_missing_candles must be a non-negative integer")
    previous_time = None
    gaps = []
    for index, candle in enumerate(candles):
        if not isinstance(candle, dict):
            raise BacktestInputError(f"candle {index} must be an object")
        timestamp = candle.get("timestamp")
        if type(timestamp) is not int or timestamp < 0:
            raise BacktestInputError(f"candle {index} timestamp must be a non-negative integer")
        try:
            opened, high, low, close = (
                _finite_number(candle[key], f"candle {index} {key}")
                for key in ("open", "high", "low", "close")
            )
        except KeyError as exc:
            raise BacktestInputError(f"candle {index} is incomplete") from exc
        if not all(isfinite(value) and value > 0 for value in (opened, high, low, close)):
            raise BacktestInputError(f"candle {index} prices must be finite and positive")
        if high < max(opened, close) or low > min(opened, close) or low > high:
            raise BacktestInputError(f"candle {index} has inconsistent OHLC prices")
        if previous_time is not None and timestamp <= previous_time:
            raise BacktestInputError("candle timestamps must be strictly increasing")
        if previous_time is not None and expected_interval_s is not None:
            elapsed = timestamp - previous_time
            if elapsed % expected_interval_s:
                raise BacktestInputError("candle timestamps do not align to expected_interval_s")
            missing = elapsed // expected_interval_s - 1
            if missing > allowed_missing_candles:
                raise BacktestInputError(
                    f"candle gap contains {missing} missing candles; "
                    f"allowed_missing_candles is {allowed_missing_candles}"
                )
            if missing:
                gaps.append({
                    "from_timestamp": previous_time,
                    "to_timestamp": timestamp,
                    "missing_candles": missing,
                })
        previous_time = timestamp
    return gaps


def run_backtest(
    candles: Iterable[dict],
    entry_signal_indices: Iterable[int],
    *,
    position_usd: float = 100.0,
    take_profit_pct: float = 100.0,
    stop_loss_pct: float = 30.0,
    fee_bps: int = 100,
    slippage_bps: int = 100,
    expected_interval_s: int | None = None,
    allowed_missing_candles: int = 0,
) -> dict:
    """Replay single-position entries and return net results after costs.

    Signals refer to candle closes. An entry is filled at the following candle
    open; exit levels are then evaluated from OHLC ranges. If TP and SL both
    trade within one candle, SL is assumed to have happened first. Slippage is
    adverse on both sides and fees are charged on each executed notional.
    """
    rows = list(candles)
    gaps = _validate_candles(
        rows,
        expected_interval_s=expected_interval_s,
        allowed_missing_candles=allowed_missing_candles,
    )
    raw_signals = list(entry_signal_indices)
    if any(not isinstance(index, int) or isinstance(index, bool) for index in raw_signals):
        raise BacktestInputError("entry signals must be integer candle indices")
    signals = sorted(set(raw_signals))
    if any(not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(rows) - 1 for index in signals):
        raise BacktestInputError("entry signals must point to a candle with a following candle")
    position_usd = _finite_number(position_usd, "position_usd")
    take_profit_pct = _finite_number(take_profit_pct, "take_profit_pct")
    stop_loss_pct = _finite_number(stop_loss_pct, "stop_loss_pct")
    if position_usd <= 0:
        raise BacktestInputError("position_usd must be finite and positive")
    if take_profit_pct <= 0:
        raise BacktestInputError("take_profit_pct must be finite and positive")
    if not 0 < stop_loss_pct < 100:
        raise BacktestInputError("stop_loss_pct must be between 0 and 100")
    if any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10_000 for value in (fee_bps, slippage_bps)):
        raise BacktestInputError("fee_bps and slippage_bps must be integers from 0 to 10000")

    fee_rate = fee_bps / 10_000
    slip_rate = slippage_bps / 10_000
    signal_set = set(signals)
    trades = []
    cursor = 0
    while cursor < len(rows) - 1:
        if cursor not in signal_set:
            cursor += 1
            continue
        entry_index = cursor + 1
        entry_candle = rows[entry_index]
        entry_price = float(entry_candle["open"]) * (1 + slip_rate)
        units = position_usd * (1 - fee_rate) / entry_price
        tp = entry_price * (1 + take_profit_pct / 100)
        sl = entry_price * (1 - stop_loss_pct / 100)
        exit_index = entry_index
        exit_price = None
        exit_reason = "end_of_data"

        for index in range(entry_index, len(rows)):
            candle = rows[index]
            # An adverse gap is filled at the open; otherwise use the level.
            if float(candle["open"]) <= sl or float(candle["low"]) <= sl:
                exit_price = min(float(candle["open"]), sl) if float(candle["open"]) <= sl else sl
                exit_reason = "stop_loss"
                exit_index = index
                break
            if float(candle["open"]) >= tp or float(candle["high"]) >= tp:
                exit_price = max(float(candle["open"]), tp) if float(candle["open"]) >= tp else tp
                exit_reason = "take_profit"
                exit_index = index
                break
        if exit_price is None:
            exit_index = len(rows) - 1
            exit_price = float(rows[exit_index]["close"])

        entry_time = int(entry_candle["timestamp"])
        exit_time = int(rows[exit_index]["timestamp"])
        # A trade crosses a data gap only when it was already open at the
        # preceding candle and remains open through the next observed candle.
        # Keep the gap metadata attached to the trade; do not infer missing
        # prices or alter the existing conservative fill rules.
        affected_gaps = [
            gap for gap in gaps
            if gap["from_timestamp"] >= entry_time and gap["to_timestamp"] <= exit_time
        ]

        effective_exit = exit_price * (1 - slip_rate)
        gross_proceeds = units * effective_exit
        net_proceeds = gross_proceeds * (1 - fee_rate)
        entry_fee = position_usd * fee_rate
        exit_fee = gross_proceeds * fee_rate
        pnl = net_proceeds - position_usd
        trades.append({
            "entry_signal_index": cursor,
            "entry_index": entry_index,
            "exit_index": exit_index,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": entry_price,
            "exit_price": effective_exit,
            "position_usd": position_usd,
            "units": units,
            "entry_fee_usd": entry_fee,
            "exit_fee_usd": exit_fee,
            "fees_usd": entry_fee + exit_fee,
            "net_pnl_usd": pnl,
            "return_pct": pnl / position_usd * 100,
            "exit_reason": exit_reason,
            "gap_affected": bool(affected_gaps),
            "gaps": affected_gaps,
        })
        # The exit candle's close is a valid signal observation. Permit a
        # signal there to enter on the following candle, while the preceding
        # position is already closed.
        cursor = exit_index

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade["net_pnl_usd"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    wins = sum(trade["net_pnl_usd"] > 0 for trade in trades)
    return {
        "trades": trades,
        "trade_count": len(trades),
        "win_rate_pct": (wins / len(trades) * 100) if trades else 0.0,
        "net_pnl_usd": sum(trade["net_pnl_usd"] for trade in trades),
        "fees_usd": sum(trade["fees_usd"] for trade in trades),
        "max_drawdown_usd": max_drawdown,
        "data_points": len(rows),
        "data_quality": {
            "expected_interval_s": expected_interval_s,
            "gap_count": len(gaps),
            "missing_candle_count": sum(gap["missing_candles"] for gap in gaps),
            "gaps": gaps,
            "first_timestamp": rows[0]["timestamp"] if rows else None,
            "last_timestamp": rows[-1]["timestamp"] if rows else None,
        },
        "assumptions": {
            "entry": "next candle open after signal close",
            "ambiguous_bar": "stop loss first",
            "slippage_bps_each_side": slippage_bps,
            "fee_bps_each_side": fee_bps,
        },
    }

