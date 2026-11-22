import asyncio
import json

import pytest

from execution_metrics import measure_execution, record_execution_metric, summarize_metrics


def test_metrics_capture_percentiles_outcomes_and_drop_sensitive_labels(tmp_path):
    path = tmp_path / "metrics.jsonl"
    for duration in (10, 20, 30, 40, 50):
        assert record_execution_metric("jupiter.order", duration, provider="jupiter", path=path)
    assert record_execution_metric("jupiter.submit", 25, "unknown", "rpc_primary", path=path)
    assert not record_execution_metric("jupiter.submit", 1, provider="private-label-placeholder", path=path)
    assert not record_execution_metric("private-label-placeholder", 1, provider="jupiter", path=path)

    summary = summarize_metrics(path)
    quote = summary["stages"]["jupiter.order:jupiter"]
    submit = summary["stages"]["jupiter.submit:rpc_primary"]
    assert summary["events"] == 6
    assert quote["p50_ms"] == 30
    assert quote["p95_ms"] == 48
    assert quote["p99_ms"] == 49.6
    assert submit["failure_rate"] == 1
    assert "private-label-placeholder" not in path.read_text(encoding="utf-8")


def test_measure_execution_preserves_exception_and_records_unknown(tmp_path):
    path = tmp_path / "metrics.jsonl"
    with pytest.raises(TimeoutError):
        with measure_execution("jupiter.confirm", path=path):
            raise TimeoutError("confirmation timed out")

    summary = summarize_metrics(path)
    assert summary["stages"]["jupiter.confirm:unknown"]["failure_rate"] == 1
    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["outcome"] == "unknown"
    assert "confirmation timed out" not in path.read_text(encoding="utf-8")


def test_summary_handles_missing_or_corrupt_metrics_file(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert summarize_metrics(path) == {"events": 0, "dropped_events": 0, "stages": {}}
    path.write_text(
        'not-json\n{"stage":"rpc.request","provider":"solana_rpc",'
        '"outcome":"success","duration_ms":5}\n',
        encoding="utf-8",
    )
    assert summarize_metrics(path)["events"] == 1


def test_metrics_writer_counts_disk_write_failures(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("fixture", encoding="utf-8")
    metrics_path = parent_file / "metrics.jsonl"

    assert record_execution_metric("jupiter.order", 1, "success", "jupiter", path=metrics_path)
    summary = summarize_metrics(metrics_path)

    assert summary["events"] == 0
    assert summary["dropped_events"] >= 1


def test_telemetry_decorator_records_async_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    import execution_metrics

    @execution_metrics.measure_async("websocket.copytrade_message", "helius")
    async def operation():
        return 42

    assert asyncio.run(operation()) == 42
    summary = summarize_metrics(tmp_path / "metrics.jsonl")
    assert summary["stages"]["websocket.copytrade_message:helius"]["count"] == 1


def test_copytrade_message_errors_are_counted_as_failures(monkeypatch, tmp_path):
    from copytrade_listener import CopyTradeListener

    path = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("EXECUTION_METRICS_PATH", str(path))
    listener = object.__new__(CopyTradeListener)
    asyncio.run(listener._handle_message("not-json"))

    summary = summarize_metrics(path)
    stage = summary["stages"]["websocket.copytrade_message:helius"]
    assert stage["failure_rate"] == 1


def test_solana_websocket_detection_provider_is_allowed_and_measured(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert record_execution_metric(
        "websocket.new_token_creation", 12, provider="solana_ws", path=path
    )

    summary = summarize_metrics(path)
    assert summary["stages"]["websocket.new_token_creation:solana_ws"]["count"] == 1
