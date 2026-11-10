"""주문 직후 대조(account_check.py --after-trade): 맞으면 조용히, 어긋나면 🚨 알림 + 실패 코드."""
import sys

import pytest

import account_check as A


def _run(monkeypatch, problems, argv):
    sent = []
    monkeypatch.setattr(A, "check", lambda: (problems, ["키움 국내포트: 원화 1원(장부 1원)"]))
    monkeypatch.setattr(A, "send", lambda text: sent.append(text))
    monkeypatch.setitem(sys.modules, "performance", type("P", (), {"report_lines": staticmethod(lambda: ["📈 성과"])}))
    monkeypatch.setattr(sys, "argv", ["account_check.py", *argv])
    return sent


def test_after_trade_quiet_when_matching(monkeypatch):
    sent = _run(monkeypatch, [], ["--after-trade"])
    A.main()
    assert sent == []


def test_after_trade_alerts_and_fails_on_mismatch(monkeypatch):
    sent = _run(monkeypatch, ["키움 원화: 실제 ≠ 장부"], ["--after-trade"])
    with pytest.raises(SystemExit) as e:
        A.main()
    assert e.value.code == 1
    assert len(sent) == 1 and sent[0].startswith("🚨 [주문 직후 대조") and "📈" not in sent[0]


def test_daily_check_unchanged(monkeypatch):
    sent = _run(monkeypatch, [], [])
    A.main()
    assert len(sent) == 1 and sent[0].startswith("✅ [계좌 체크") and "📈 성과" in sent[0]
