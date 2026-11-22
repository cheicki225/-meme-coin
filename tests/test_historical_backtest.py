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


def test_max_drawdown_includes_unrealized_loss_before_take_profit():
    candles = [
        _candle(1, 100, 100, 100, 100),
        _candle(2, 100, 105, 99, 100),
        _candle(3, 100, 111, 90, 110),
    ]
    result = run_backtest(
        candles, [0], take_profit_pct=10, stop_loss_pct=20,
        position_usd=100, fee_bps=0, slippage_bps=0,
    )

    assert result["trades"][0]["exit_reason"] == "take_profit"
    assert result["trades"][0]["net_pnl_usd"] == pytest.approx(10)
    # The previous candle's high marks +$5, then the TP candle's low marks
    # -$10 before the modeled take-profit fill: a $15 peak-to-trough drawdown.
    assert result["max_drawdown_usd"] == pytest.approx(15)
    assert result["assumptions"]["drawdown_path"] == "conservative_mark_to_market_from_OHLC"


def test_signal_on_exit_candle_can_enter_on_the_following_candle():
    candles = [
        _candle(1, 100, 100, 100, 100),
        _candle(2, 100, 105, 95, 100),
        _candle(3, 100, 111, 99, 110),
        _candle(4, 105, 106, 104, 105),
        _candle(5, 105, 106, 104, 105),
    ]
    result = run_backtest(
        candles, [0, 2], take_profit_pct=10, stop_loss_pct=20,
        fee_bps=0, slippage_bps=0,
    )
    assert [trade["entry_signal_index"] for trade in result["trades"]] == [0, 2]
    assert [trade["entry_index"] for trade in result["trades"]] == [1, 3]


def test_rejects_bad_ohlc_and_signals_without_next_candle():
    with pytest.raises(BacktestInputError, match="inconsistent"):
        run_backtest([_candle(1, 100, 90, 80, 95)], [])
    with pytest.raises(BacktestInputError, match="following candle"):
        run_backtest([_candle(1, 100, 101, 99, 100)], [0])


def test_expected_interval_rejects_gaps_unless_explicitly_allowed():
    candles = [
        _candle(60, 100, 101, 99, 100),
        _candle(180, 100, 101, 99, 100),
    ]
    with pytest.raises(BacktestInputError, match="1 missing candles"):
        run_backtest(candles, [], expected_interval_s=60)

    result = run_backtest(
        candles, [], expected_interval_s=60, allowed_missing_candles=1
    )
    assert result["data_quality"]["gap_count"] == 1
    assert result["data_quality"]["missing_candle_count"] == 1
    assert result["data_quality"]["gaps"] == [{
        "from_timestamp": 60,
        "to_timestamp": 180,
        "missing_candles": 1,
    }]


def test_trade_spanning_an_allowed_gap_is_marked_without_inventing_prices():
    candles = [
        _candle(60, 100, 100, 100, 100),
        _candle(120, 100, 101, 99, 100),
        _candle(240, 100, 111, 99, 110),
        _candle(300, 110, 111, 109, 110),
    ]

    result = run_backtest(
        candles, [0], take_profit_pct=10, stop_loss_pct=20,
        fee_bps=0, slippage_bps=0,
        expected_interval_s=60, allowed_missing_candles=1,
    )

    trade = result["trades"][0]
    assert trade["exit_reason"] == "take_profit"
    assert trade["gap_affected"] is True
    assert trade["gaps"] == [{
        "from_timestamp": 120,
        "to_timestamp": 240,
        "missing_candles": 1,
    }]
    assert "price" not in trade["gaps"][0]


def test_trade_not_spanning_an_allowed_gap_is_not_marked():
    candles = [
        _candle(60, 100, 100, 100, 100),
        _candle(120, 100, 111, 99, 110),
        _candle(240, 110, 111, 109, 110),
        _candle(300, 110, 111, 109, 110),
    ]

    result = run_backtest(
        candles, [0], take_profit_pct=10, stop_loss_pct=20,
        fee_bps=0, slippage_bps=0,
        expected_interval_s=60, allowed_missing_candles=1,
    )

    trade = result["trades"][0]
    assert trade["exit_index"] == 1
    assert trade["gap_affected"] is False
    assert trade["gaps"] == []


@pytest.mark.parametrize("expected_interval_s", [True, 0, -60, 1.5, "60"])
def test_expected_interval_requires_a_positive_integer(expected_interval_s):
    candles = [_candle(60, 100, 101, 99, 100)]
    with pytest.raises(BacktestInputError, match="expected_interval_s"):
        run_backtest(candles, [], expected_interval_s=expected_interval_s)


def test_expected_interval_rejects_timestamps_off_interval_grid():
    candles = [_candle(60, 100, 101, 99, 100), _candle(181, 100, 101, 99, 100)]
    with pytest.raises(BacktestInputError, match="do not align"):
        run_backtest(candles, [], expected_interval_s=60)


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

