import json

from execution_metrics import measure_execution, record_execution_metric, summarize_metrics


def test_record_and_percentile_summary(tmp_path):
    path = tmp_path / "metrics.jsonl"
    for duration in (10, 20, 30, 40, 50):
        assert record_execution_metric("quote", duration, path=path)
    summary = summarize_metrics(path)
    row = summary["stages"]["quote:unknown"]
    assert summary["events"] == 5
    assert row["p50_ms"] == 30
    assert row["p95_ms"] == 48
    assert row["failure_rate"] == 0


def test_failure_and_unknown_count_as_failures(tmp_path):
    path = tmp_path / "metrics.jsonl"
    record_execution_metric("send", 10, "failure", "rpc", path=path)
    record_execution_metric("send", 20, "unknown", "rpc", path=path)
    record_execution_metric("send", 30, "success", "rpc", path=path)
    row = summarize_metrics(path)["stages"]["send:rpc"]
    assert row["failure_rate"] == 0.6667


def test_measure_context_records_unknown_on_exception(tmp_path):
    path = tmp_path / "metrics.jsonl"
    try:
        with measure_execution("confirm", path=path):
            raise TimeoutError("confirmation unclear")
    except TimeoutError:
        pass
    event = json.loads(path.read_text().strip())
    assert event["stage"] == "confirm"
    assert event["outcome"] == "unknown"
    assert event["duration_ms"] >= 0


def test_metrics_reject_non_finite_and_invalid_outcomes(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert not record_execution_metric("send", float("nan"), path=path)
    assert not record_execution_metric("send", 1, "retry-now", path=path)
    assert not path.exists()


def test_summary_ignores_corrupt_lines(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text('not-json\n{"stage":"x","provider":"rpc","outcome":"success","duration_ms":5}\n')
    assert summarize_metrics(path)["events"] == 1


def test_summary_missing_file_is_empty(tmp_path):
    assert summarize_metrics(tmp_path / "missing.jsonl") == {"events": 0, "stages": {}}
