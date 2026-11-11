import pytest

from liquidity_monitor import (
    assess_liquidity_change,
    extract_liquidity_snapshot,
    update_position_liquidity,
)


def _pair(liquidity=10000, pair_address="PoolA", mint="MintA", chain="solana"):
    return {
        "chainId": chain,
        "pairAddress": pair_address,
        "dexId": "raydium",
        "baseToken": {"address": mint},
        "quoteToken": {"address": "USDC"},
        "liquidity": {"usd": liquidity},
    }


def test_extract_liquidity_snapshot_requires_solana_pair_matching_mint():
    assert extract_liquidity_snapshot("MintA", _pair())["liquidity_usd"] == 10000
    assert extract_liquidity_snapshot("MintA", _pair(chain="ethereum")) is None
    assert extract_liquidity_snapshot("OtherMint", _pair()) is None
    assert extract_liquidity_snapshot("MintA", _pair(liquidity="nan")) is None


def test_assess_liquidity_change_flags_only_same_pool_threshold_drop():
    previous = {"pair_address": "PoolA", "liquidity_usd": 10000}
    current = {"pair_address": "PoolA", "liquidity_usd": 5500}
    result = assess_liquidity_change(previous, current, threshold_pct=40)
    assert result["status"] == "liquidity_drop"
    assert result["alert"] is True
    assert result["drop_pct"] == 45
    assert assess_liquidity_change(previous, {"pair_address": "PoolB", "liquidity_usd": 100})["status"] == "pool_changed"


def test_assess_liquidity_change_rejects_invalid_threshold():
    with pytest.raises(ValueError):
        assess_liquidity_change(None, {}, threshold_pct=float("nan"))
    with pytest.raises(ValueError):
        assess_liquidity_change(None, {}, threshold_pct=100)


def test_position_monitor_emits_one_alert_per_drop_episode_and_resets_on_recovery():
    position = {"token_mint": "MintA"}
    first = update_position_liquidity(position, _pair(10000), sampled_at=1)
    assert first["status"] == "baseline"
    assert first["alert"] is False

    drop = update_position_liquidity(position, _pair(5000), sampled_at=2)
    assert drop["status"] == "liquidity_drop"
    assert drop["alert"] is True

    still_dropping = update_position_liquidity(position, _pair(4500), sampled_at=3)
    assert still_dropping["alert"] is False

    recovery = update_position_liquidity(position, _pair(9000), sampled_at=4)
    assert recovery["status"] == "ok"
    assert recovery["alert"] is False

    next_drop = update_position_liquidity(position, _pair(4000), sampled_at=5)
    assert next_drop["alert"] is True
