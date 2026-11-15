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


@pytest.mark.parametrize("timestamp", [True, 1.0, "1", "1.0", -1, float("nan")])
def test_candles_reject_non_integer_or_negative_timestamps(timestamp):
    candles = [_candle(timestamp, 10, 11, 9, 10), _candle(2, 10, 11, 9, 10)]
    with pytest.raises(BacktestInputError, match="timestamp must be a non-negative integer"):
        run_backtest(candles, [])


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad_value", [
    True, "10", "NaN", None, float("nan"), float("inf"), float("-inf"),
])
def test_candles_reject_non_numeric_or_non_finite_prices(field, bad_value):
    candle = _candle(1, 10, 11, 9, 10)
    candle[field] = bad_value
    with pytest.raises(BacktestInputError):
        run_backtest([candle], [])


@pytest.mark.parametrize("field,bad_value", [
    ("position_usd", True),
    ("position_usd", "100"),
    ("position_usd", float("nan")),
    ("position_usd", float("inf")),
    ("position_usd", float("-inf")),
    ("take_profit_pct", True),
    ("take_profit_pct", "10"),
    ("take_profit_pct", float("nan")),
    ("take_profit_pct", float("inf")),
    ("take_profit_pct", float("-inf")),
    ("stop_loss_pct", True),
    ("stop_loss_pct", "10"),
    ("stop_loss_pct", float("nan")),
    ("stop_loss_pct", float("inf")),
    ("stop_loss_pct", float("-inf")),
])
def test_numeric_strategy_parameters_reject_bools_strings_and_non_finite(field, bad_value):
    candles = [_candle(1, 10, 11, 9, 10), _candle(2, 10, 11, 9, 10)]
    with pytest.raises(BacktestInputError):
        run_backtest(candles, [], **{field: bad_value})


@pytest.mark.parametrize("field", ["fee_bps", "slippage_bps"])
@pytest.mark.parametrize("bad_value", [True, 1.5, "100", float("nan"), float("inf")])
def test_cost_parameters_require_finite_integer_basis_points(field, bad_value):
    candles = [_candle(1, 10, 11, 9, 10), _candle(2, 10, 11, 9, 10)]
    with pytest.raises(BacktestInputError, match="fee_bps and slippage_bps must be integers"):
        run_backtest(candles, [], **{field: bad_value})

