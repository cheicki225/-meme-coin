"""Versioned, hash-addressed OHLC datasets for reproducible offline replay.

This module stores caller-supplied history only. A SHA-256 digest detects
accidental changes; it does not authenticate the upstream data provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from historical_backtest import BacktestInputError, _validate_candles


DATASET_SCHEMA_VERSION = 1
_DATASET_KEYS = {
    "schema_version",
    "asset_id",
    "source",
    "interval_s",
    "fetched_at",
    "allowed_missing_candles",
    "candles",
    "sha256",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class BacktestDatasetError(ValueError):
    """Raised when an OHLC dataset is unsafe or not reproducible."""


def _canonical_json(value: dict) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BacktestDatasetError("Dataset contains a value outside the JSON schema") from exc


def _metadata_string(value, field: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise BacktestDatasetError(f"{field} must be a non-empty string of at most {max_length} characters")
    normalized = value.strip()
    if any(ord(character) < 32 for character in normalized):
        raise BacktestDatasetError(f"{field} cannot contain control characters")
    return normalized


def build_dataset(
    candles: Iterable[dict],
    *,
    asset_id: str,
    source: str,
    interval_s: int,
    fetched_at: int,
    allowed_missing_candles: int = 0,
) -> dict:
    """Normalize caller-supplied candles and compute their stable digest.

    ``fetched_at`` and candle ``timestamp`` values are Unix seconds. Only the
    timestamp and OHLC prices are stored; provider-specific extra fields are
    intentionally excluded from the replay dataset.
    """
    asset = _metadata_string(asset_id, "asset_id")
    provider = _metadata_string(source, "source", max_length=80)
    if not _ASSET_RE.fullmatch(asset):
        raise BacktestDatasetError("asset_id contains unsupported characters")
    if not _SOURCE_RE.fullmatch(provider):
        raise BacktestDatasetError("source must be a short provider identifier, not a URL")
    if type(interval_s) is not int or interval_s <= 0:
        raise BacktestDatasetError("interval_s must be a positive integer")
    if type(fetched_at) is not int or fetched_at < 0:
        raise BacktestDatasetError("fetched_at must be a non-negative Unix timestamp in seconds")
    if type(allowed_missing_candles) is not int or allowed_missing_candles < 0:
        raise BacktestDatasetError("allowed_missing_candles must be a non-negative integer")

    try:
        input_rows = list(candles)
    except TypeError as exc:
        raise BacktestDatasetError("candles must be an iterable of OHLC objects") from exc
    if not input_rows:
        raise BacktestDatasetError("dataset must contain at least one candle")

    normalized_rows = []
    for index, candle in enumerate(input_rows):
        if not isinstance(candle, dict):
            raise BacktestDatasetError(f"candle {index} must be an object")
        try:
            normalized_rows.append({
                key: candle[key]
                for key in ("timestamp", "open", "high", "low", "close")
            })
        except KeyError as exc:
            raise BacktestDatasetError(f"candle {index} is incomplete") from exc

    try:
        _validate_candles(
            normalized_rows,
            expected_interval_s=interval_s,
            allowed_missing_candles=allowed_missing_candles,
        )
    except BacktestInputError as exc:
        raise BacktestDatasetError(f"invalid candle series: {exc}") from exc
    if normalized_rows[-1]["timestamp"] > fetched_at:
        raise BacktestDatasetError("fetched_at cannot precede the last candle timestamp")

    payload = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "asset_id": asset,
        "source": provider,
        "interval_s": interval_s,
        "fetched_at": fetched_at,
        "allowed_missing_candles": allowed_missing_candles,
        "candles": normalized_rows,
    }
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return {**payload, "sha256": digest}


def validate_dataset(dataset: dict) -> dict:
    """Validate the version, candle series, and content hash of a dataset."""
    if not isinstance(dataset, dict) or set(dataset) != _DATASET_KEYS:
        raise BacktestDatasetError("dataset fields do not match the supported schema")
    if type(dataset.get("schema_version")) is not int or dataset["schema_version"] != DATASET_SCHEMA_VERSION:
        raise BacktestDatasetError("unsupported dataset schema version")
    digest = dataset.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise BacktestDatasetError("dataset sha256 is malformed")
    normalized = build_dataset(
        dataset.get("candles"),
        asset_id=dataset.get("asset_id"),
        source=dataset.get("source"),
        interval_s=dataset.get("interval_s"),
        fetched_at=dataset.get("fetched_at"),
        allowed_missing_candles=dataset.get("allowed_missing_candles"),
    )
    if normalized["sha256"] != digest:
        raise BacktestDatasetError("dataset content hash does not match its metadata")
    return normalized


def save_dataset(path: str | os.PathLike, dataset: dict) -> Path:
    """Atomically write a validated JSON dataset to the caller-selected path."""
    validated = validate_dataset(dataset)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(_canonical_json(validated))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
        return target
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _reject_json_constant(value: str):
    raise BacktestDatasetError(f"non-standard JSON constant {value} is not allowed")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BacktestDatasetError("dataset JSON contains a duplicate key")
        result[key] = value
    return result


def load_dataset(path: str | os.PathLike) -> dict:
    """Load a dataset while rejecting duplicate keys, NaN, and bad hashes."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            dataset = json.load(
                stream,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
    except BacktestDatasetError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BacktestDatasetError("dataset file cannot be read as valid JSON") from exc
    return validate_dataset(dataset)
