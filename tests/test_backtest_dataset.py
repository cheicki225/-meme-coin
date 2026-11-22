import pytest

import backtest
from backtest_dataset import (
    BacktestDatasetError,
    build_dataset,
    load_dataset,
    save_dataset,
    validate_dataset,
)


def candle(timestamp, opened=100, high=101, low=99, close=100, **extra):
    return {
        "timestamp": timestamp,
        "open": opened,
        "high": high,
        "low": low,
        "close": close,
        **extra,
    }


def sample_dataset(*, gap=False):
    timestamps = [600, 720] if gap else [600, 660]
    return build_dataset(
        [candle(timestamps[0]), candle(timestamps[1])],
        asset_id="TokenMint123",
        source="geckoterminal",
        interval_s=60,
        fetched_at=800,
        allowed_missing_candles=1 if gap else 0,
    )


def test_dataset_hash_is_deterministic_and_ignores_provider_extra_fields():
    first = build_dataset(
        [candle(600, volume=3)], asset_id="TokenMint123", source="geckoterminal",
        interval_s=60, fetched_at=600,
    )
    second = build_dataset(
        [candle(600, volume=999)], asset_id="TokenMint123", source="geckoterminal",
        interval_s=60, fetched_at=600,
    )
    assert first["sha256"] == second["sha256"]
    assert set(first["candles"][0]) == {"timestamp", "open", "high", "low", "close"}


def test_dataset_round_trip_writes_atomically_and_validates_hash(tmp_path):
    dataset = sample_dataset()
    target = tmp_path / "archive" / "token.json"

    saved_path = save_dataset(target, dataset)
    loaded = load_dataset(saved_path)

    assert saved_path == target
    assert loaded == dataset
    assert list(target.parent.glob("*.tmp")) == []


def test_dataset_validation_rejects_modified_candle_content():
    dataset = sample_dataset()
    dataset["candles"][0]["close"] = 100.5

    with pytest.raises(BacktestDatasetError, match="hash"):
        validate_dataset(dataset)


def test_dataset_rejects_unsupported_schema_and_extra_fields():
    dataset = sample_dataset()
    dataset["schema_version"] = 2
    with pytest.raises(BacktestDatasetError, match="schema version"):
        validate_dataset(dataset)

    dataset = sample_dataset()
    dataset["provider_url"] = "https://provider.invalid/?api-key=should-not-be-stored"
    with pytest.raises(BacktestDatasetError, match="fields"):
        validate_dataset(dataset)


@pytest.mark.parametrize("source", ["https://provider.invalid/?key=value", "provider/path", "provider name"])
def test_source_metadata_must_be_a_provider_identifier_not_a_url(source):
    with pytest.raises(BacktestDatasetError, match="provider identifier"):
        build_dataset([candle(600)], asset_id="TokenMint123", source=source,
                      interval_s=60, fetched_at=600)


def test_dataset_enforces_candle_interval_and_fetched_time():
    with pytest.raises(BacktestDatasetError, match="missing candles"):
        build_dataset(
            [candle(600), candle(720)], asset_id="TokenMint123", source="geckoterminal",
            interval_s=60, fetched_at=720,
        )
    with pytest.raises(BacktestDatasetError, match="fetched_at"):
        build_dataset(
            [candle(600)], asset_id="TokenMint123", source="geckoterminal",
            interval_s=60, fetched_at=599,
        )


def test_dataset_replay_includes_provenance_and_bound_gap_policy():
    dataset = sample_dataset(gap=True)

    result = backtest.replay_historical_dataset(dataset, [])

    assert result["data_quality"]["missing_candle_count"] == 1
    assert result["dataset"] == {
        "schema_version": 1,
        "asset_id": "TokenMint123",
        "source": "geckoterminal",
        "interval_s": 60,
        "fetched_at": 800,
        "sha256": dataset["sha256"],
    }
    with pytest.raises(BacktestDatasetError, match="conflicts"):
        backtest.replay_historical_dataset(dataset, [], expected_interval_s=30)


def test_dataset_replay_maps_exact_signal_timestamps_to_candle_indices():
    dataset = build_dataset(
        [candle(600), candle(660), candle(720, opened=110, high=111, low=109, close=110)],
        asset_id="TokenMint123",
        source="geckoterminal",
        interval_s=60,
        fetched_at=720,
    )

    result = backtest.replay_historical_dataset(
        dataset,
        entry_signal_timestamps=[600],
        fee_bps=0,
        slippage_bps=0,
    )

    assert result["trades"][0]["entry_signal_index"] == 0
    assert result["trades"][0]["entry_time"] == 660
    assert result["signal_timestamp_mapping"] == [
        {"signal_timestamp": 600, "candle_index": 0}
    ]


@pytest.mark.parametrize("signal_timestamps", [[601], [True], [-1]])
def test_dataset_replay_rejects_invalid_or_unmatched_signal_timestamps(signal_timestamps):
    with pytest.raises(BacktestDatasetError):
        backtest.replay_historical_dataset(
            sample_dataset(),
            entry_signal_timestamps=signal_timestamps,
        )


def test_dataset_replay_rejects_both_signal_input_forms():
    with pytest.raises(BacktestDatasetError, match="not both"):
        backtest.replay_historical_dataset(
            sample_dataset(),
            [0],
            entry_signal_timestamps=[600],
        )


@pytest.mark.parametrize("raw", [
    '{"schema_version": NaN}',
    '{"schema_version": 1, "schema_version": 1}',
])
def test_dataset_loader_rejects_nonstandard_json_and_duplicate_keys(tmp_path, raw):
    target = tmp_path / "bad.json"
    target.write_text(raw, encoding="utf-8")

    with pytest.raises(BacktestDatasetError):
        load_dataset(target)
