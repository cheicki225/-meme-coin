import backtest


def test_wallet_backtest_module_exposes_candle_replay():
    candles = [
        {"timestamp": 1, "open": 10, "high": 11, "low": 9, "close": 10},
        {"timestamp": 2, "open": 10, "high": 12, "low": 9, "close": 11},
    ]
    result = backtest.replay_historical_ohlc(
        candles, [0], take_profit_pct=10, stop_loss_pct=20, fee_bps=0, slippage_bps=0
    )
    assert result["trade_count"] == 1
    assert result["trades"][0]["entry_index"] == 1
