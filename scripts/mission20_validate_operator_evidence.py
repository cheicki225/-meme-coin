"""Check the shape of a redacted Mission 20 evidence record, offline only.

The validator never proves that an operator record is authentic or that a real
target ran continuously. Every result remains MANUAL_VERIFICATION_REQUIRED.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


MIN_SOAK_SECONDS = 24 * 60 * 60
MAX_OBSERVATION_GAP_SECONDS = 70 * 60
MAX_RECORD_BYTES = 1_000_000
_REDACTED_REFERENCE = re.compile(r"redacted:[A-Za-z0-9_-]{1,64}\Z")


def _utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _reference_is_redacted(value: Any) -> bool:
    return isinstance(value, str) and _REDACTED_REFERENCE.fullmatch(value) is not None


def validate_evidence_record(record: Any) -> dict[str, Any]:
    """Validate record structure while keeping all readiness claims manual."""
    issues: list[str] = []
    gate_issues: list[str] = []
    if not isinstance(record, dict):
        return {
            "record_validation": "INVALID",
            "issues": ["ROOT_MUST_BE_OBJECT"],
            "reported_mission19_gate": "UNKNOWN",
            "recorded_duration_seconds": None,
            "observation_count": 0,
            "restore_record_present": False,
            "release_readiness": "MANUAL_VERIFICATION_REQUIRED",
            "operational_evidence_verified": False,
            "network_access": False,
        }

    if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        issues.append("SCHEMA_VERSION_MUST_BE_1")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        issues.append("RUN_ID_INVALID")

    gate = record.get("mission19_gate")
    reported_gate = gate.get("status") if isinstance(gate, dict) else "UNKNOWN"
    if reported_gate != "CLEARED":
        gate_issues.append("MISSION19_GATE_NOT_CLEARED")
    if not isinstance(gate, dict) or not _reference_is_redacted(gate.get("evidence_ref")):
        issues.append("MISSION19_GATE_REFERENCE_REQUIRED")

    opened = _utc_timestamp(record.get("opened_at_utc"))
    closed = _utc_timestamp(record.get("closed_at_utc"))
    if opened is None:
        issues.append("OPENING_TIME_MUST_BE_UTC")
    if closed is None:
        issues.append("CLOSING_TIME_MUST_BE_UTC")
    for field, code in (
        ("opening_integrity", "OPENING_INTEGRITY_NOT_OK"),
        ("closing_integrity", "CLOSING_INTEGRITY_NOT_OK"),
    ):
        if record.get(field) != "ok":
            issues.append(code)
    if not _reference_is_redacted(record.get("closeout_evidence_ref")):
        issues.append("CLOSEOUT_REFERENCE_REQUIRED")

    observations = record.get("observations")
    observation_times: list[datetime] = []
    if not isinstance(observations, list) or len(observations) < 25:
        issues.append("AT_LEAST_25_OBSERVATIONS_REQUIRED")
        observations = observations if isinstance(observations, list) else []
    for observation in observations:
        if not isinstance(observation, dict):
            issues.append("OBSERVATION_MUST_BE_OBJECT")
            continue
        timestamp = _utc_timestamp(observation.get("observed_at_utc"))
        if timestamp is None:
            issues.append("OBSERVATION_TIME_MUST_BE_UTC")
        else:
            observation_times.append(timestamp)
        if observation.get("trading_mode") != "PAPER":
            issues.append("OBSERVATION_NOT_PAPER")
        if observation.get("live_execution_enabled") is not False:
            issues.append("LIVE_FLAG_NOT_FALSE")
        if observation.get("emergency_stop_active") is not True:
            issues.append("EMERGENCY_STOP_NOT_ACTIVE")
        if type(observation.get("unknown_count")) is not int or observation.get("unknown_count") != 0:
            issues.append("UNKNOWN_COUNT_NOT_ZERO")
        if type(observation.get("rpc_429_count")) is not int or observation.get("rpc_429_count") != 0:
            issues.append("RPC_429_COUNT_NOT_ZERO")
        if observation.get("recovery_paused") is not False:
            issues.append("RECOVERY_PAUSED")
        if observation.get("provider_read_status") != "OK":
            issues.append("PROVIDER_READ_NOT_OK")
        if not _reference_is_redacted(observation.get("evidence_ref")):
            issues.append("OBSERVATION_REFERENCE_REQUIRED")

    if observation_times:
        if observation_times != sorted(observation_times) or len(set(observation_times)) != len(observation_times):
            issues.append("OBSERVATIONS_NOT_STRICTLY_INCREASING")
        for earlier, later in zip(observation_times, observation_times[1:]):
            if (later - earlier).total_seconds() > MAX_OBSERVATION_GAP_SECONDS:
                issues.append("OBSERVATION_GAP_EXCEEDS_70_MINUTES")

    duration_seconds: int | None = None
    if opened is not None and closed is not None:
        duration_seconds = int((closed - opened).total_seconds())
        if duration_seconds < MIN_SOAK_SECONDS:
            issues.append("RECORDED_DURATION_UNDER_24_HOURS")
        if closed <= opened:
            issues.append("CLOSING_TIME_NOT_AFTER_OPENING")
        if observation_times:
            if abs((observation_times[0] - opened).total_seconds()) > 5 * 60:
                issues.append("FIRST_OBSERVATION_NOT_NEAR_OPENING")
            if abs((closed - observation_times[-1]).total_seconds()) > 5 * 60:
                issues.append("LAST_OBSERVATION_NOT_NEAR_CLOSEOUT")

    restore = record.get("restore")
    restore_present = isinstance(restore, dict)
    if not isinstance(restore, dict):
        issues.append("RESTORE_RECORD_REQUIRED")
    else:
        backup_time = _utc_timestamp(restore.get("backup_created_at_utc"))
        restored_time = _utc_timestamp(restore.get("restored_at_utc"))
        if backup_time is None or restored_time is None:
            issues.append("RESTORE_TIMES_MUST_BE_UTC")
        elif restored_time < backup_time:
            issues.append("RESTORE_PRECEDES_BACKUP")
        if restore.get("restored_to_new_path") is not True:
            issues.append("RESTORE_MUST_USE_NEW_PATH")
        if restore.get("source_integrity") != "ok" or restore.get("restored_integrity") != "ok":
            issues.append("RESTORE_INTEGRITY_NOT_OK")
        if not _reference_is_redacted(restore.get("evidence_ref")):
            issues.append("RESTORE_REFERENCE_REQUIRED")

    unique_issues = list(dict.fromkeys(issues))
    unique_gate_issues = list(dict.fromkeys(gate_issues))
    return {
        "record_validation": "VALID" if not unique_issues else "INVALID",
        "issues": unique_issues + unique_gate_issues,
        "reported_mission19_gate": reported_gate,
        "recorded_duration_seconds": duration_seconds,
        "observation_count": len(observation_times),
        "restore_record_present": restore_present,
        "release_readiness": "MANUAL_VERIFICATION_REQUIRED",
        "operational_evidence_verified": False,
        "network_access": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="path to a sanitized JSON evidence record")
    args = parser.parse_args()
    try:
        if args.record.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("record exceeds size limit")
        with args.record.open("r", encoding="utf-8") as source:
            record = json.load(source)
        report = validate_evidence_record(record)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        report = {
            "record_validation": "INVALID",
            "issues": ["INPUT_UNREADABLE_OR_INVALID_JSON"],
            "reported_mission19_gate": "UNKNOWN",
            "recorded_duration_seconds": None,
            "observation_count": 0,
            "restore_record_present": False,
            "release_readiness": "MANUAL_VERIFICATION_REQUIRED",
            "operational_evidence_verified": False,
            "network_access": False,
        }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if report["record_validation"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
