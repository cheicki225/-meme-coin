import pytest

from historical_backtest import BacktestInputError, run_backtest


def _candle(timestamp, opened, high, low, close):
    return {"timestamp": timestamp, "open": opened, "high": high, "low": low, "close": close}


def test_signals_fill_next_open_and_results_include_fees_and_slippage():
    candles = [
        _candle(1, 100, 500, 50, 105),
        _candle(2, 100, 112, 99, 110),
        _candle(3, 110, 111, 109, 110),
    ]
    result = run_backtest(candles, [0], take_profit_pct=10, stop_loss_pct=20,
                          fee_bps=100, slippage_bps=50)
    trade = result["trades"][0]
    assert trade["entry_index"] == 1
    assert trade["exit_reason"] == "take_profit"
    assert trade["fees_usd"] > 0
    assert trade["net_pnl_usd"] < 10
    assert result["fees_usd"] == trade["fees_usd"]


def test_ambiguous_candle_uses_stop_loss_first():
    candles = [
        _candle(1, 100, 101, 99, 100),
        _candle(2, 100, 120, 70, 100),
    ]
    result = run_backtest(candles, [0], take_profit_pct=10, stop_loss_pct=10,
                          fee_bps=0, slippage_bps=0)
    assert result["trades"][0]["exit_reason"] == "stop_loss"
    assert result["trades"][0]["net_pnl_usd"] == pytest.approx(-10)


def test_rejects_bad_ohlc_and_signals_without_next_candle():
    with pytest.raises(BacktestInputError, match="inconsistent"):
        run_backtest([_candle(1, 100, 90, 80, 95)], [])
    with pytest.raises(BacktestInputError, match="following candle"):
        run_backtest([_candle(1, 100, 101, 99, 100)], [0])


def test_malformed_signal_indices_raise_domain_error():
    candles = [_candle(1, 10, 11, 9, 10), _candle(2, 10, 11, 9, 10)]
    with pytest.raises(BacktestInputError, match="integer candle indices"):
        run_backtest(candles, ["0"])
    with pytest.raises(BacktestInputError, match="integer candle indices"):
        run_backtest(candles, [[0]])

