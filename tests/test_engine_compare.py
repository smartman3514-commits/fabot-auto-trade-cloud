"""11-26 시스템 줄이기 1단계: 엔진 '오늘 판단'과 자동매매 판단 그림자 비교 — 같은 날만 비교, 허용 차이 밖이면 다름."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import fa_tools  # noqa: E402

E = {"date": "2026-11-20", "fg": 9.75, "target": 0.8491, "rsi": 26.5, "hot": True, "bear": True,
     "conditions": {"curve_buy_open": False, "dip_fg_met": True, "greed_sell_fg_met": False}}


class R:
    def __init__(self, j): self.j = j
    def raise_for_status(self): pass
    def json(self): return self.j


def _patch(monkeypatch, j):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(j))


def test_same_day_same_signals(monkeypatch):
    _patch(monkeypatch, E)
    r = fa_tools._engine_compare("2026-11-20", 9.93, 0.86, 27.0, True, 10)
    assert r["compared"] and r["same"], r


def test_detects_difference(monkeypatch):
    _patch(monkeypatch, E)
    r = fa_tools._engine_compare("2026-11-20", 18.6, 0.70, 27.0, True, 10)   # 11-20 처럼 F&G 가 다르면 공포 매수 판단도 갈림
    assert r["compared"] and not r["same"] and any("공포 매수" in d for d in r["diffs"]), r


def test_different_day_not_compared(monkeypatch):
    _patch(monkeypatch, E)
    r = fa_tools._engine_compare("2026-11-24", 17.4, 0.81, 36.0, True, 10)
    assert not r["compared"] and "날짜가 달라" in r["why"]
