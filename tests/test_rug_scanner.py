import asyncio
import logging
import time
from contextlib import contextmanager

import pytest

import backtest
import config
import execution_metrics
import main
import rug_scanner
import wallet_history


class Store:
    def __init__(self, candidates=None):
        self.state = {"rug_scan_candidates": candidates or {}}
        self.save_count = 0

    def save(self):
        self.save_count += 1


class Notifier:
    def __init__(self):
        self.messages = []

    async def notify(self, event, message):
        self.messages.append((event, message))


_DEFAULT_CREATIONS = object()


def _creation_rows(count=1):
    return [
        {"token_mint": f"mint-{index}", "signature": f"signature-{index}"}
        for index in range(count)
    ]


def _set_good_sources(
    monkeypatch, scanner, *, creations=_DEFAULT_CREATIONS, tx_count=250, volume=20_000
):
    async def get_created_tokens(*_args, **_kwargs):
        return _creation_rows() if creations is _DEFAULT_CREATIONS else creations

    async def count_transactions(_token_mint):
        return tx_count

    async def get_pair_data(_token_mint):
        return {"volume": {"h24": volume}}

    monkeypatch.setattr(wallet_history, "get_created_tokens", get_created_tokens)
    monkeypatch.setattr(scanner, "_count_transactions", count_transactions)
    monkeypatch.setattr(backtest, "_get_pair_data", get_pair_data)


def test_provider_creation_timestamp_uses_only_integer_enhanced_timestamp():
    assert rug_scanner.provider_creation_timestamp({"raw": {"timestamp": 1_700_000_000}}) == 1_700_000_000
    assert rug_scanner.provider_creation_timestamp({"raw": {"timestamp": True}}) is None
    assert rug_scanner.provider_creation_timestamp({"raw": {"timestamp": 1_700_000_000.0}}) is None
    assert rug_scanner.provider_creation_timestamp({"raw": {}}) is None
    assert rug_scanner.provider_creation_timestamp({"created_at": 1_700_000_000}) is None


def test_main_passes_provider_timestamp_to_rug_scanner(monkeypatch):
    timestamp = 1_700_000_000
    registered = []

    @contextmanager
    def no_metric(*_args, **_kwargs):
        yield {}

    class Scanner:
        def register_candidate(self, token_mint, dev_address, created_at=None, created_at_source=None):
            registered.append((token_mint, dev_address, created_at, created_at_source))

    class Store:
        state = {
            "monitored_dev_wallets": {
                "creator-address": {
                    "mode": "track_creation", "label": "known", "backtest_ratio": 1.0,
                }
            }
        }

        def is_dev_monitored(self, _dev_address):
            return True

        def update_wallet_activity(self, _dev_address):
            return None

    class Trader:
        async def open_position(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(execution_metrics, "measure_execution", no_metric)
    bot = object.__new__(main.SniperBot)
    bot.rug_scanner = Scanner()
    bot.data_store = Store()
    bot.trader = Trader()

    asyncio.run(bot.on_new_token({
        "token_mint": "candidate-mint",
        "dev_address": "creator-address",
        "raw": {"timestamp": timestamp},
    }))

    assert registered == [(
        "candidate-mint", "creator-address", timestamp,
        rug_scanner.CREATION_TIMESTAMP_SOURCE,
    )]


def test_check_filters_passes_exact_configured_boundaries_with_limited_provenance(monkeypatch):
    monkeypatch.setattr(config, "RUG_SCAN_MAX_DEV_CREATIONS", 3)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_TX_COUNT", 250)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_VOLUME_USD", 20_000)
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    _set_good_sources(monkeypatch, scanner, creations=_creation_rows(3), tx_count=250, volume=20_000)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 0.5))

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["reason_code"] is None
    assert result["observed_at_utc"].endswith("+00:00")
    assert result["freshness"] == "filter_source_timestamps_unavailable"
    assert result["provenance"]["developer_creations"]["count"] == 3
    assert result["provenance"]["developer_creations"]["provider_timestamp_utc"] is None
    assert result["provenance"]["token_signatures"]["count"] == 250
    assert result["provenance"]["pair_volume"]["volume_24h_usd"] == 20_000


@pytest.mark.parametrize(
    "created_tokens",
    [[], None, {}, [{"token_mint": "mint-only"}], [{"signature": "signature-only"}]],
)
def test_empty_or_malformed_developer_history_is_unknown_and_short_circuits(
    monkeypatch, created_tokens
):
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    _set_good_sources(monkeypatch, scanner, creations=created_tokens)
    monkeypatch.setattr(
        scanner,
        "_count_transactions",
        lambda *_args: pytest.fail("unknown developer history must short-circuit"),
    )
    monkeypatch.setattr(
        backtest,
        "_get_pair_data",
        lambda *_args: pytest.fail("unknown developer history must short-circuit"),
    )

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "unknown"
    assert result["passed"] is False
    assert result["reason_code"] == "developer_creation_history_unavailable"
    assert result["provenance"]["developer_creations"]["status"] == "unknown"


def test_developer_history_exception_is_unknown_without_exception_message(monkeypatch):
    store = Store()
    scanner = rug_scanner.RugScanner(store)

    async def fail_with_sensitive_message(*_args, **_kwargs):
        raise RuntimeError("provider credential detail")

    monkeypatch.setattr(wallet_history, "get_created_tokens", fail_with_sensitive_message)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "unknown"
    assert result["reason_code"] == "developer_creation_fetch_failed"
    assert result["source_error_type"] == "RuntimeError"
    assert "provider credential detail" not in str(result)


@pytest.mark.parametrize("tx_count", [None, 0, True, -1, 250.0])
def test_missing_or_malformed_signature_count_is_unknown(monkeypatch, tx_count):
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    _set_good_sources(monkeypatch, scanner, tx_count=tx_count)
    monkeypatch.setattr(
        backtest,
        "_get_pair_data",
        lambda *_args: pytest.fail("unknown signature count must short-circuit"),
    )

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "unknown"
    assert result["passed"] is False
    assert result["reason_code"] == "token_signature_count_unavailable"
    assert result["provenance"]["token_signatures"]["status"] == "unknown"


def test_transaction_counter_distinguishes_empty_and_malformed_responses(monkeypatch):
    scanner = rug_scanner.RugScanner(Store())

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(wallet_history, "get_all_signatures_paginated", empty)
    assert asyncio.run(scanner._count_transactions("candidate-mint")) is None

    async def malformed(*_args, **_kwargs):
        return [{"signature": ""}]

    monkeypatch.setattr(wallet_history, "get_all_signatures_paginated", malformed)
    assert asyncio.run(scanner._count_transactions("candidate-mint")) is None

    async def valid(*_args, **_kwargs):
        return [{"signature": "sig-a"}, {"signature": "sig-b"}]

    monkeypatch.setattr(wallet_history, "get_all_signatures_paginated", valid)
    assert asyncio.run(scanner._count_transactions("candidate-mint")) == 2


@pytest.mark.parametrize("volume", [None, True, float("nan"), float("inf"), -1, "20000"])
def test_missing_or_malformed_volume_is_unknown(monkeypatch, volume):
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    _set_good_sources(monkeypatch, scanner)

    async def get_pair_data(_token_mint):
        return {"volume": {"h24": volume}}

    monkeypatch.setattr(backtest, "_get_pair_data", get_pair_data)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "unknown"
    assert result["passed"] is False
    assert result["reason_code"] == "pair_volume_unavailable"
    assert result["provenance"]["pair_volume"]["status"] == "unknown"


def test_filter_threshold_rejections_keep_existing_short_circuit_order(monkeypatch):
    monkeypatch.setattr(config, "RUG_SCAN_MAX_DEV_CREATIONS", 3)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_TX_COUNT", 250)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_VOLUME_USD", 20_000)
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    calls = []

    async def get_created_tokens(*_args, **_kwargs):
        calls.append("creations")
        return _creation_rows(4)

    async def count_transactions(_mint):
        calls.append("signatures")
        return 300

    async def get_pair_data(_mint):
        calls.append("volume")
        return {"volume": {"h24": 30_000}}

    monkeypatch.setattr(wallet_history, "get_created_tokens", get_created_tokens)
    monkeypatch.setattr(scanner, "_count_transactions", count_transactions)
    monkeypatch.setattr(backtest, "_get_pair_data", get_pair_data)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "rejected"
    assert result["reason_code"] == "developer_creation_limit_exceeded"
    assert calls == ["creations"]


@pytest.mark.parametrize(
    ("tx_count", "volume", "reason_code"),
    [
        (249, 20_000, "token_signature_threshold_not_met"),
        (250, 19_999, "pair_volume_threshold_not_met"),
    ],
)
def test_signature_and_volume_thresholds_remain_configured(
    monkeypatch, tx_count, volume, reason_code
):
    monkeypatch.setattr(config, "RUG_SCAN_MAX_DEV_CREATIONS", 3)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_TX_COUNT", 250)
    monkeypatch.setattr(config, "RUG_SCAN_MIN_VOLUME_USD", 20_000)
    scanner = rug_scanner.RugScanner(Store())
    _set_good_sources(monkeypatch, scanner, tx_count=tx_count, volume=volume)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "rejected"
    assert result["reason_code"] == reason_code


@pytest.mark.parametrize(
    ("failed_source", "reason_code"),
    [
        ("signatures", "token_signature_fetch_failed"),
        ("volume", "pair_volume_fetch_failed"),
    ],
)
def test_provider_exceptions_are_unknown_without_raw_error_details(
    monkeypatch, failed_source, reason_code
):
    scanner = rug_scanner.RugScanner(Store())
    _set_good_sources(monkeypatch, scanner)

    if failed_source == "signatures":
        async def fail_signatures(_token_mint):
            raise RuntimeError("provider credential detail")

        monkeypatch.setattr(scanner, "_count_transactions", fail_signatures)
    else:
        async def fail_volume(_token_mint):
            raise RuntimeError("provider credential detail")

        monkeypatch.setattr(backtest, "_get_pair_data", fail_volume)

    result = asyncio.run(scanner._check_filters("candidate-mint", "creator-address", 1))

    assert result["status"] == "unknown"
    assert result["passed"] is False
    assert result["reason_code"] == reason_code
    assert result["source_error_type"] == "RuntimeError"
    assert "provider credential detail" not in str(result)


def test_future_or_invalid_candidate_time_is_not_scanned_or_notified(monkeypatch, caplog):
    now = 1_700_000_000
    token_mint = "candidate-public-mint"
    store = Store({
        token_mint: {
            "dev_address": "creator-address", "created_at": now + 1,
            "created_at_source": rug_scanner.CREATION_TIMESTAMP_SOURCE, "notified": False,
        },
        "missing-time-mint": {"dev_address": "creator-address", "created_at": None, "notified": False},
        "legacy-local-time-mint": {"dev_address": "creator-address", "created_at": now - 60, "notified": False},
    })
    notifier = Notifier()
    scanner = rug_scanner.RugScanner(store, notifier)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)
    monkeypatch.setattr(rug_scanner.time, "time", lambda: now)
    monkeypatch.setattr(
        wallet_history,
        "get_created_tokens",
        lambda *_args, **_kwargs: pytest.fail("future timestamp must not be scanned"),
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(scanner._scan_all_candidates())

    assert notifier.messages == []
    assert token_mint not in caplog.text
    assert "invalid_candidate_timestamp_or_creator" in caplog.text


def test_register_candidate_keeps_missing_or_invalid_timestamp_unknown(monkeypatch):
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)

    scanner.register_candidate("missing-time-mint", "creator-address")
    scanner.register_candidate("candidate-mint", "creator-address", created_at=0)

    assert store.state["rug_scan_candidates"]["missing-time-mint"]["created_at"] is None
    assert store.state["rug_scan_candidates"]["candidate-mint"]["created_at"] is None
    assert store.save_count == 2


def test_register_candidate_requires_helius_timestamp_provenance(monkeypatch):
    store = Store()
    scanner = rug_scanner.RugScanner(store)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)

    scanner.register_candidate(
        "candidate-mint", "creator-address", created_at=1_700_000_000,
        created_at_source=rug_scanner.CREATION_TIMESTAMP_SOURCE,
    )

    candidate = store.state["rug_scan_candidates"]["candidate-mint"]
    assert candidate["created_at"] == 1_700_000_000
    assert candidate["created_at_source"] == rug_scanner.CREATION_TIMESTAMP_SOURCE


def test_expired_candidate_is_removed_without_provider_calls(monkeypatch):
    now = 1_700_000_000
    token_mint = "candidate-public-mint"
    store = Store({
        token_mint: {
            "dev_address": "creator-address", "created_at": now - 90_000,
            "created_at_source": rug_scanner.CREATION_TIMESTAMP_SOURCE, "notified": False,
        }
    })
    scanner = rug_scanner.RugScanner(store)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)
    monkeypatch.setattr(config, "RUG_SCAN_MAX_AGE_HOURS", 24)
    monkeypatch.setattr(rug_scanner.time, "time", lambda: now)
    monkeypatch.setattr(
        wallet_history,
        "get_created_tokens",
        lambda *_args, **_kwargs: pytest.fail("expired candidate must not be scanned"),
    )

    asyncio.run(scanner._scan_all_candidates())

    assert token_mint not in store.state["rug_scan_candidates"]
    assert store.save_count == 1


def test_pass_notification_is_sent_once_and_describes_heuristic_filter_result(monkeypatch):
    now = int(time.time())
    token_mint = "candidate-public-mint"
    store = Store({
        token_mint: {
            "dev_address": "creator-address", "created_at": now - 60,
            "created_at_source": rug_scanner.CREATION_TIMESTAMP_SOURCE, "notified": False,
        }
    })
    notifier = Notifier()
    scanner = rug_scanner.RugScanner(store, notifier)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)
    monkeypatch.setattr(config, "RUG_SCAN_MAX_AGE_HOURS", 24)
    _set_good_sources(monkeypatch, scanner)

    asyncio.run(scanner._scan_all_candidates())
    asyncio.run(scanner._scan_all_candidates())

    assert len(notifier.messages) == 1
    assert "Candidat passé les filtres heuristiques" in notifier.messages[0][1]
    assert "ne confirment pas la sécurité du token" in notifier.messages[0][1]
    assert store.state["rug_scan_candidates"][token_mint]["notified"] is True


def test_unknown_result_never_notifies_even_if_passed_flag_is_true(monkeypatch):
    now = 1_700_000_000
    token_mint = "candidate-public-mint"
    store = Store({
        token_mint: {
            "dev_address": "creator-address", "created_at": now - 60,
            "created_at_source": rug_scanner.CREATION_TIMESTAMP_SOURCE, "notified": False,
        }
    })
    notifier = Notifier()
    scanner = rug_scanner.RugScanner(store, notifier)
    monkeypatch.setattr(config, "RUG_SCAN_ENABLED", True)
    monkeypatch.setattr(rug_scanner.time, "time", lambda: now)

    async def unknown(*_args):
        return {"passed": True, "status": "unknown", "reason_code": "source_unavailable"}

    monkeypatch.setattr(scanner, "_check_filters", unknown)

    asyncio.run(scanner._scan_all_candidates())

    assert notifier.messages == []
    assert store.state["rug_scan_candidates"][token_mint]["notified"] is False
