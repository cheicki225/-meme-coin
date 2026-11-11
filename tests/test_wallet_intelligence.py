from wallet_intelligence import score_wallet

def test_missing_metrics_are_unknown_not_neutral():
    result = score_wallet({})
    assert result["score"] is None
    assert result["recommendation"] == "insufficient_data"
    assert result["confidence"] == "none"
    assert all(item["status"] == "unknown" for item in result["components"].values())

def test_scores_only_measured_metrics_and_reports_coverage():
    result = score_wallet({"realized_pnl_pct": 50, "sample_counts": {"realized_pnl_pct": 25}})
    assert result["score"] == 50.0
    assert result["coverage"] == 0.3
    assert result["recommendation"] == "insufficient_data"
    assert result["components"]["win_rate"]["score"] is None

def test_high_quality_wallet_can_be_recommended_to_follow():
    metrics = {
        "realized_pnl_pct": 120, "win_rate": 0.85, "profit_factor": 2.5,
        "consistency": 0.9, "early_entry_rate": 0.8, "sell_discipline": 0.9,
        "sample_counts": {name: 40 for name in (
            "realized_pnl_pct", "win_rate", "profit_factor", "consistency",
            "early_entry_rate", "sell_discipline"
        )},
    }
    result = score_wallet(metrics)
    assert result["score"] >= 70
    assert result["recommendation"] == "follow"
    assert result["confidence"] == "high"

def test_invalid_values_and_relationships_do_not_inflate_score():
    result = score_wallet(
        {
            "realized_pnl_pct": float("nan"), "win_rate": True,
            "profit_factor": "not-a-number",
            "sample_counts": {"realized_pnl_pct": 10, "win_rate": 10, "profit_factor": 10},
        },
        {"shared_funders": ["wallet-b"], "co_buy_wallets": ["wallet-c"]},
    )
    assert result["score"] is None
    assert result["recommendation"] == "insufficient_data"
    assert "shared_funder_relationship" in result["risk_flags"]
    assert "co_buy_relationship" in result["risk_flags"]


def test_known_malicious_relationship_overrides_excellent_metrics():
    metrics = {
        "realized_pnl_pct": 120, "win_rate": 0.85, "profit_factor": 2.5,
        "consistency": 0.9, "early_entry_rate": 0.8, "sell_discipline": 0.9,
        "sample_counts": {name: 40 for name in (
            "realized_pnl_pct", "win_rate", "profit_factor", "consistency",
            "early_entry_rate", "sell_discipline"
        )},
    }
    result = score_wallet(metrics, {"known_malicious_link": True})
    assert result["score"] >= 70
    assert result["recommendation"] == "avoid"
    assert "known_malicious_link" in result["risk_flags"]
