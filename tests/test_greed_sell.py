"""탐욕 매도 — CAPE 안 막혔으면 칸의 최소 비중(곡선 목표)까지만 줄이기 (2026-11-16 인선).
예전: 77 이상이면 전부 팔고 다음 거래일에 곡선이 20% 다시 삼 → 이제 한 번에 '20%만 남기고 줄이기'."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402

TODAY = date(2026, 11, 5)


def tq(qty, cash):
    return v2.Position(v2.SLEEVES[0], capital=cash + qty * 100, cash=cash, qty=qty)


def test_cape_hot_sells_all_as_before():
    q, why = v2.greed_sell(tq(600, 40_000), 100.0, 78, .2, cape_hot=True)
    assert q == 600 and "전부" in why and "CAPE" in why


def test_cape_hot_half_as_before():
    q, why = v2.greed_sell(tq(600, 40_000), 100.0, 73, .2, cape_hot=True)
    assert q == 300 and "절반" in why


def test_77_keeps_floor_instead_of_selling_all():
    # 칸 10만 달러 = TQQQ 600주 × $100 + 현금 4만. 목표 20% → 200주 남기고 400주만 판다
    q, why = v2.greed_sell(tq(600, 40_000), 100.0, 78, .2, cape_hot=False)
    assert q == 400 and "최소 비중 20%" in why and "200주 남김" in why


def test_half_when_it_stays_above_floor():
    q, why = v2.greed_sell(tq(600, 40_000), 100.0, 73, .2, cape_hot=False)   # 절반 팔아도 30% 남음
    assert q == 300 and "절반" in why


def test_half_capped_at_floor():
    q, why = v2.greed_sell(tq(250, 75_000), 100.0, 73, .2, cape_hot=False)   # 절반이면 12.5% → 20%(200주)까지만
    assert q == 50 and "200주 남김" in why


def test_already_below_floor_does_not_sell():
    q, why = v2.greed_sell(tq(150, 85_000), 100.0, 73, .2, cape_hot=False)
    assert q == 0 and "안 팖" in why


def _us(fg, hot, qty=600, cash=40_000):
    pos = v2.build_ledger({"usd_capital": 100_000, "krw_capital": 0, "start_date": "2026-10-19"}, [])
    pos["TQQQ"].qty, pos["TQQQ"].cash = qty, cash
    prices = {s.key: 100.0 for s in v2.SLEEVES}
    fear = {"target": .2, "S": 0.0, "rsi": 60}
    brk = {k: [(TODAY - timedelta(days=i), 50.0) for i in range(400, 0, -1)] for k in v2.LADDER}
    return v2.plan_us(pos, fg, fear, {"hot": hot, "cape": 25, "line": 30}, prices, TODAY, brk_hist=brk,
                      start=date(2026, 10, 19), ndx_closes=None)


def test_plan_us_one_sell_order_no_rebuy():
    tq_orders = [o for o in _us(78, False) if o.sleeve == "TQQQ"]
    assert len(tq_orders) == 1 and tq_orders[0].side == "sell" and tq_orders[0].qty == 400


def test_plan_us_cape_hot_sells_all():
    tq_orders = [o for o in _us(78, True) if o.sleeve == "TQQQ"]
    assert len(tq_orders) == 1 and tq_orders[0].side == "sell" and tq_orders[0].qty == 600


def test_plan_us_next_day_curve_does_not_buy_back():
    # 판 뒤(200주·현금 8만) 다음 날 F&G 70: 비중 20% = 목표 → 곡선이 다시 사지 않음
    tq_orders = [o for o in _us(70, False, qty=200, cash=80_000) if o.sleeve == "TQQQ"]
    assert not any(o.side == "buy" for o in tq_orders)


def test_kr_plan_keeps_floor():
    import fabot_kr as K
    pos = K.build_ledger({"krw_capital": 500_000_000, "start_date": "2026-10-27", "gold_start": "2026-11-10"}, [])
    pos["TQ"].qty, pos["TQ"].cash = 3_000, 40_000_000          # 3,000주 × 2만 원 = 6천만 + 현금 4천만
    prices = {s.key: 20_000 for s in K.SLEEVES}
    fear = {"target": .2, "S": 0.0, "rsi": 60}
    div = [(TODAY - timedelta(days=i), 15_000.0) for i in range(400, 0, -1)]
    orders = K.plan(pos, 78, fear, {"hot": False, "cape": 25, "line": 30}, prices, TODAY, div, date(2026, 10, 27),
                    gold_start=date(2026, 11, 10), gold_rsi=70, ndx_closes=None)
    tq_orders = [o for o in orders if o.sleeve == "TQ"]
    assert len(tq_orders) == 1 and tq_orders[0].side == "sell" and tq_orders[0].qty == 2_000   # 1,000주(20%) 남김
