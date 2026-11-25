import pytest

from scripts.mission21_sqlite_profile import run_profile, summarize_latency_ms


def test_latency_summary_uses_interpolated_percentiles():
    assert summarize_latency_ms([1.0, 2.0, 3.0]) == {
        "samples": 3,
        "p50_ms": 2.0,
        "p95_ms": 2.9,
        "p99_ms": 2.98,
        "max_ms": 3.0,
    }


@pytest.mark.parametrize("samples", [[], [-0.1], [float("nan")], [float("inf")]])
def test_latency_summary_rejects_empty_or_invalid_samples(samples):
    with pytest.raises(ValueError):
        summarize_latency_ms(samples)


def test_profile_inserts_deduplicates_and_drains_only_synthetic_rows(tmp_path):
    result = run_profile(12, tmp_path)

    assert result["status"] == "offline_synthetic_component_profile"
    assert result["environment"]["trading_mode"] == "PAPER"
    assert result["environment"]["live_execution_enabled"] is False
    assert result["environment"]["network_calls"] == 0
    assert result["workload"]["unique_rows_inserted"] == 12
    assert result["workload"]["duplicate_observations"] == 12
    assert result["workload"]["processed_rows"] == 12
    assert result["workload"]["pending_rows_after_drain"] == 0
    assert result["workload"]["unknown_rows_after_drain"] == 0
    assert result["workload"]["checkpoint_sequence"] == 12
    assert result["workload"]["integrity_check"] == "ok"
    assert result["storage"]["total_bytes"] > 0
    assert result["storage"]["provider_or_chain_fees"] == "not measured"
    assert {"mission21-inbox.sqlite3", "mission21-metrics.jsonl"}.issubset(
        {path.name for path in tmp_path.iterdir()}
    )


@pytest.mark.parametrize("event_count", [True, 0, 9, 5001])
def test_profile_rejects_invalid_event_counts_before_creating_output(
    tmp_path, event_count
):
    with pytest.raises(ValueError):
        run_profile(event_count, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing_name", [
    "mission21-inbox.sqlite3",
    "mission21-metrics.jsonl",
])
def test_profile_refuses_to_reuse_existing_database_or_metrics(tmp_path, existing_name):
    (tmp_path / existing_name).write_bytes(b"")
    with pytest.raises(FileExistsError):
        run_profile(10, tmp_path)
