import pytest

from backtest_dataset import build_dataset
from backtest_validation import BacktestResearchError, _metrics, optimize_and_evaluate


def _candle(timestamp, price):
    return {
        "timestamp": timestamp,
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
    }


def _dataset(*, source="synthetic", asset_id="synthetic:toy", prices=None):
    prices = prices or [100, 101, 104, 102, 105, 108, 107, 111, 114, 113, 117, 120]
    candles = [_candle(index * 60, price) for index, price in enumerate(prices)]
    return build_dataset(
        candles,
        asset_id=asset_id,
        source=source,
        interval_s=60,
        fetched_at=candles[-1]["timestamp"] + 60,
    )


def _up_close_rule(prefix, _parameters):
    return len(prefix) > 1 and prefix[-1]["close"] > prefix[-2]["close"]


def _candidates():
    return [
        {
            "take_profit_pct": 4,
            "stop_loss_pct": 20,
            "fee_bps": 25,
            "slippage_bps": 25,
            "signal_parameters": {"rule": "up-close"},
        },
        {
            "take_profit_pct": 9,
            "stop_loss_pct": 20,
            "fee_bps": 25,
            "slippage_bps": 25,
            "signal_parameters": {"rule": "up-close"},
        },
    ]


def test_optimization_uses_train_only_then_one_chronological_validation_pass():
    dataset = _dataset()
    observed_signal_timestamps = []

    def recording_rule(prefix, parameters):
        assert isinstance(parameters, dict) or hasattr(parameters, "__getitem__")
        observed_signal_timestamps.append(prefix[-1]["timestamp"])
        return _up_close_rule(prefix, parameters)

    report = optimize_and_evaluate(
        dataset,
        recording_rule,
        _candidates(),
        split_index=7,
        embargo_bars=1,
        initial_equity_usd=1_000,
        position_usd=100,
    )

    train_end = report["split"]["train_end_exclusive"]
    split_timestamp = dataset["candles"][report["split"]["validation_start"]]["timestamp"]
    train_decision_count = len(_candidates()) * (train_end - 1)
    assert max(observed_signal_timestamps[:train_decision_count]) < split_timestamp
    assert min(observed_signal_timestamps[train_decision_count:]) >= split_timestamp
    assert report["split"]["parameters_selected_from"] == "training_only"
    assert report["split"]["validation_run_count"] == 1
    assert report["dataset_classification"] == "SYNTHETIC"
    assert report["status"] == "SYNTHETIC_RESEARCH_ONLY"
    assert report["performance_claim_eligible"] is False
    assert report["validation_metrics"] is not None
    assert {
        "roi_pct", "net_pnl_usd", "max_drawdown_usd", "win_rate_pct", "profit_factor", "fees_usd",
    }.issubset(report["validation_metrics"])
    assert report["fills"]["network_fees_and_rent"] == "not modeled"


def test_external_provider_name_and_recomputed_hash_do_not_authorize_metrics():
    # A caller can recompute the digest after changing the candles. It still
    # cannot promote this source declaration into authenticated provenance.
    external = _dataset(
        source="geckoterminal",
        asset_id="TokenMint123",
        prices=[100.25, 101, 104, 102, 105, 108, 107, 111, 114, 113, 117, 120],
    )
    called = False

    def should_not_run(_prefix, _parameters):
        nonlocal called
        called = True
        return True

    report = optimize_and_evaluate(
        external,
        should_not_run,
        _candidates(),
        split_index=7,
    )

    assert called is False
    assert report["status"] == "UNKNOWN"
    assert report["dataset_classification"] == "UNVERIFIED_EXTERNAL"
    assert report["metrics"] is None
    assert report["performance_claim_eligible"] is False


def test_synthetic_dataset_without_training_trades_has_no_performance_metrics():
    report = optimize_and_evaluate(
        _dataset(),
        lambda _prefix, _parameters: False,
        _candidates(),
        split_index=7,
    )

    assert report["status"] == "NO_TRAINING_TRADES"
    assert report["training_metrics"] is None
    assert report["validation_metrics"] is None
    assert report["metrics"] is None


def test_zero_bar_embargo_allows_adjacent_non_overlapping_chronological_partitions():
    report = optimize_and_evaluate(
        _dataset(),
        _up_close_rule,
        _candidates(),
        split_index=7,
        embargo_bars=0,
    )

    assert report["status"] == "SYNTHETIC_RESEARCH_ONLY"
    assert report["split"]["train_end_exclusive"] == report["split"]["validation_start"]
    assert report["split"]["train_end_timestamp"] < report["split"]["validation_start_timestamp"]


def test_non_finite_replay_metrics_are_rejected():
    with pytest.raises(BacktestResearchError, match="non-finite"):
        _metrics(
            {
                "trades": [{"net_pnl_usd": float("inf"), "fees_usd": 0.0}],
                "max_drawdown_usd": 0.0,
                "win_rate_pct": 100.0,
            },
            initial_equity_usd=1_000.0,
        )


@pytest.mark.parametrize(
    ("split_index", "embargo_bars"),
    [(True, 1), (2, 1), (7, -1), (11, 1)],
)
def test_rejects_invalid_or_degenerate_chronological_split(split_index, embargo_bars):
    with pytest.raises(BacktestResearchError):
        optimize_and_evaluate(
            _dataset(),
            _up_close_rule,
            _candidates(),
            split_index=split_index,
            embargo_bars=embargo_bars,
        )
