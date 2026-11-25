from copy import deepcopy
from datetime import datetime, timedelta, timezone

from scripts.mission20_validate_operator_evidence import validate_evidence_record


def _record():
    opened = datetime(2026, 11, 20, tzinfo=timezone.utc)
    observations = []
    for hour in range(25):
        observed = opened + timedelta(hours=hour)
        observations.append({
            "observed_at_utc": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trading_mode": "PAPER",
            "live_execution_enabled": False,
            "emergency_stop_active": True,
            "unknown_count": 0,
            "rpc_429_count": 0,
            "recovery_paused": False,
            "provider_read_status": "OK",
            "evidence_ref": f"redacted:hour-{hour:02d}",
        })
    return {
        "schema_version": 1,
        "run_id": "synthetic-fixture-run",
        "mission19_gate": {
            "status": "CLEARED",
            "evidence_ref": "redacted:m19-review",
        },
        "opened_at_utc": opened.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "closed_at_utc": (opened + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "opening_integrity": "ok",
        "closing_integrity": "ok",
        "closeout_evidence_ref": "redacted:closeout",
        "observations": observations,
        "restore": {
            "backup_created_at_utc": (opened + timedelta(hours=12)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "restored_at_utc": (opened + timedelta(hours=13)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "restored_to_new_path": True,
            "source_integrity": "ok",
            "restored_integrity": "ok",
            "evidence_ref": "redacted:restore",
        },
    }


def test_valid_record_shape_never_claims_operational_readiness():
    report = validate_evidence_record(_record())

    assert report["record_validation"] == "VALID"
    assert report["recorded_duration_seconds"] == 24 * 60 * 60
    assert report["observation_count"] == 25
    assert report["restore_record_present"] is True
    assert report["release_readiness"] == "MANUAL_VERIFICATION_REQUIRED"
    assert report["operational_evidence_verified"] is False
    assert report["network_access"] is False


def test_unresolved_mission19_gate_stays_blocked_even_with_complete_record_shape():
    record = _record()
    record["mission19_gate"]["status"] = "MANUAL_VERIFICATION_REQUIRED"

    report = validate_evidence_record(record)

    assert report["record_validation"] == "VALID"
    assert report["issues"] == ["MISSION19_GATE_NOT_CLEARED"]
    assert report["release_readiness"] == "MANUAL_VERIFICATION_REQUIRED"


def test_incomplete_or_gapped_observation_period_is_invalid():
    record = _record()
    record["observations"].pop(12)

    report = validate_evidence_record(record)

    assert report["record_validation"] == "INVALID"
    assert "AT_LEAST_25_OBSERVATIONS_REQUIRED" in report["issues"]
    assert "OBSERVATION_GAP_EXCEEDS_70_MINUTES" in report["issues"]


def test_non_paper_or_unknown_observation_is_invalid():
    record = _record()
    record["observations"][4]["trading_mode"] = "LIVE"
    record["observations"][8]["unknown_count"] = 1

    report = validate_evidence_record(record)

    assert report["record_validation"] == "INVALID"
    assert "OBSERVATION_NOT_PAPER" in report["issues"]
    assert "UNKNOWN_COUNT_NOT_ZERO" in report["issues"]


def test_restore_must_use_new_path_with_integrity_evidence():
    record = _record()
    record["restore"]["restored_to_new_path"] = False
    record["restore"]["restored_integrity"] = "unknown"

    report = validate_evidence_record(record)

    assert "RESTORE_MUST_USE_NEW_PATH" in report["issues"]
    assert "RESTORE_INTEGRITY_NOT_OK" in report["issues"]


def test_naive_timestamp_and_unredacted_reference_are_rejected():
    record = deepcopy(_record())
    record["observations"][0]["observed_at_utc"] = "2026-11-20T00:00:00"
    record["observations"][1]["evidence_ref"] = "C:/private/log.json"

    report = validate_evidence_record(record)

    assert "OBSERVATION_TIME_MUST_BE_UTC" in report["issues"]
    assert "OBSERVATION_REFERENCE_REQUIRED" in report["issues"]


def test_under_24_hour_duration_is_invalid():
    record = _record()
    record["closed_at_utc"] = "2026-11-20T23:59:59Z"
    record["observations"].pop()

    report = validate_evidence_record(record)

    assert "RECORDED_DURATION_UNDER_24_HOURS" in report["issues"]
