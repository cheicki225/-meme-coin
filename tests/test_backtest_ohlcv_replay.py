from backtest import replay_ohlcv_path


def candle(timestamp, opened, high, low, close):
    return [timestamp, opened, high, low, close, 1]


def test_replay_respects_stop_loss_that_happens_before_a_later_take_profit():
    candles = [
        candle(1, 100, 100, 100, 100),
        candle(2, 100, 105, 85, 95),
        candle(3, 95, 150, 94, 145),
    ]
    result = replay_ohlcv_path(candles, tp_pct=20, sl_pct=10)
    assert result["hit_sl"] is True
    assert result["hit_tp"] is False
    assert result["result_pct"] == -10


def test_replay_uses_stop_loss_when_both_thresholds_are_hit_in_one_candle():
    result = replay_ohlcv_path([
        candle(1, 100, 100, 100, 100),
        candle(2, 100, 125, 80, 110),
    ], tp_pct=20, sl_pct=10)
    assert result["hit_sl"] is True
    assert result["hit_tp"] is False


def test_replay_keeps_take_profit_when_a_later_candle_drops_to_stop():
    result = replay_ohlcv_path([
        candle(1, 100, 100, 100, 100),
        candle(2, 100, 125, 99, 120),
        candle(3, 120, 121, 80, 90),
    ], tp_pct=20, sl_pct=10)
    assert result["hit_tp"] is True
    assert result["hit_sl"] is False
    assert result["result_pct"] == 20


def test_entry_candle_wicks_are_not_used_as_post_entry_exits():
    result = replay_ohlcv_path([
        candle(1, 100, 150, 80, 100),
        candle(2, 100, 105, 95, 102),
    ], tp_pct=20, sl_pct=10)
    assert result["hit_tp"] is False
    assert result["hit_sl"] is False
    assert result["result_pct"] == 2


def test_replay_rejects_unordered_or_malformed_candles():
    assert replay_ohlcv_path([candle(2, 100, 101, 99, 100), candle(1, 100, 101, 99, 100)], 10, 10) is None
    assert replay_ohlcv_path([candle(1, 100, 90, 80, 95), candle(2, 95, 96, 90, 92)], 10, 10) is None
    assert replay_ohlcv_path([candle(1, 100, 101, 99, 100), candle(2, True, 101, 99, 100)], 10, 10) is None
