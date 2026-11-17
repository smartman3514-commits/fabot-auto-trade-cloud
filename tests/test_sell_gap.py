"""탐욕 매도 71/78 · 판 뒤 42거래일 쉬기 (2026-11-17 인선 결정). 엔진 fa-journey PR #20 과 같게."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402
from test_ab_rule import TODAY, _pos, _us  # noqa: E402


def test_numbers():
    assert (v2.SELL_HALF, v2.SELL_ALL, v2.SELL_GAP_DAYS) == (71, 78, 42)


def test_new_and_old_memos_are_greed_sells():
    assert v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행 — F&G 71 ≥ 71 → 절반 매도"})
    assert v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행 — F&G 79 ≥ 78 → 전부 매도"})
    assert v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행 — F&G 73 ≥ 72 → 절반 매도"})   # 11-17 전 메모


def test_71_sells_half_in_cape_hot():
    o = _us(_pos(qty=600), 71, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)
    assert o[0].side == "sell" and o[0].qty == 300 and "≥ 71" in o[0].note


def test_77_is_still_half_78_is_all_in_cape_hot():
    assert _us(_pos(qty=600), 77, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)[0].qty == 300
    assert _us(_pos(qty=600), 78, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)[0].qty == 600


def test_rest_42_trading_days_after_greed_sell():
    sold = TODAY - timedelta(days=40)       # 약 28거래일 전
    pos = _pos(qty=300, greed=[(sold, 100.0)])
    o = _us(pos, 80, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)
    assert o[0].side == "hold" and "42거래일 쉬는 중" in o[0].note


def test_rest_over_after_42_trading_days():
    sold = TODAY - timedelta(days=70)       # 약 50거래일 전
    pos = _pos(qty=300, greed=[(sold, 100.0)])
    o = _us(pos, 80, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)
    assert o[0].side == "sell" and o[0].qty == 300


def test_year_end_sell_does_not_start_rest():
    pos = _pos(qty=300)
    pos["TQQQ"].sells = [(TODAY - timedelta(days=3), 10, 100.0)]      # 연말 비중 맞추기 같은 보통 매도
    o = _us(pos, 75, {"target": .2, "hi1": 120.0, "tq_now": 100.0}, hot=True)
    assert o[0].side == "sell" and o[0].qty == 150
