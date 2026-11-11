from copytrade_listener import CopyTradeListener
from protection_scanner import ProtectionScanner


def test_copytrade_signature_cache_has_hard_size_bound(monkeypatch):
    import copytrade_listener

    monkeypatch.setattr(copytrade_listener, "_SIGNATURE_CACHE_MAX_SIZE", 3)
    listener = CopyTradeListener.__new__(CopyTradeListener)
    listener._recent_signatures = {
        "old-1": 1.0,
        "old-2": 2.0,
        "old-3": 3.0,
    }

    assert listener._already_processed("new-signature") is False
    assert len(listener._recent_signatures) == 3
    assert "old-1" not in listener._recent_signatures
    assert "new-signature" in listener._recent_signatures


def test_protection_signature_cache_has_hard_size_bound(monkeypatch):
    import protection_scanner

    monkeypatch.setattr(protection_scanner, "_SIGNATURE_CACHE_MAX_SIZE", 3)
    scanner = ProtectionScanner.__new__(ProtectionScanner)
    scanner._recent_signatures = {
        "old-1": 1.0,
        "old-2": 2.0,
        "old-3": 3.0,
    }

    assert scanner._already_processed("new-signature") is False
    assert len(scanner._recent_signatures) == 3
    assert "old-1" not in scanner._recent_signatures
    assert "new-signature" in scanner._recent_signatures
