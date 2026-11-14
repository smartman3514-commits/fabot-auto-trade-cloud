"""CAPE 막기 중 공포 매수(V2d + 쉬기 풀기, 2026-11-14 인선) — fabot_v2.dip_plan 과 plan_us 연결.
규칙: F&G ≤ 25 + 나스닥100 고점 대비 −7% 넘게 → 칸의 4%(F&G ≤ 10 이면 8%), 5거래일 쉼(마지막 산 값보다 10% 넘게 싸면 풀림),
한 하락(나스닥100 고점 뒤)에서 칸의 20% 까지."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402

TODAY = date(2026, 11, 5)          # 목요일
PEAK_D = date(2026, 10, 6)


def ndx(peak=100.0):
    """10-06 에 고점 100, 그 뒤 90 근처."""
    out, d = [], date(2026, 9, 1)
    while d < TODAY:
        if d.weekday() < 5:
            out.append((d, peak if d == PEAK_D else 95.0))
        d += timedelta(days=1)
    return out


def tq(cash=100_000.0, qty=0.0, buys=None):
    p = v2.Position(v2.SLEEVES[0], capital=cash, cash=cash, qty=qty)
    p.buys = buys or []
    return p


def test_not_fear_returns_none():
    assert v2.dip_plan(tq(), 50.0, 30, ndx(), 90.0, TODAY) is None


def test_small_index_drop_holds():
    o = v2.dip_plan(tq(), 50.0, 20, ndx(), 95.0, TODAY)        # 고점 대비 −5%
    assert o.side == "hold" and "7%" in o.note


def test_buy_four_percent():
    o = v2.dip_plan(tq(), 50.0, 20, ndx(), 90.0, TODAY)        # −10%
    assert o.side == "buy" and abs(o.amount - 4_000) < 1e-6


def test_panic_doubles():
    o = v2.dip_plan(tq(), 50.0, 8, ndx(), 90.0, TODAY)
    assert o.side == "buy" and abs(o.amount - 8_000) < 1e-6


def test_cooldown_holds_then_releases_on_ten_percent_lower():
    buys = [(date(2026, 11, 3), 80, 50.0)]                     # 2거래일 전 $50 에 삼
    p = tq(cash=96_000.0, qty=80, buys=buys)
    hold = v2.dip_plan(p, 46.0, 20, ndx(), 90.0, TODAY)        # −8% → 아직 쉼
    assert hold.side == "hold" and "쉬는 중" in hold.note
    rel = v2.dip_plan(p, 44.0, 20, ndx(), 90.0, TODAY)         # −12% → 쉬기 풀기
    assert rel.side == "buy" and "쉬기 풀기" in rel.note


def test_budget_per_drawdown():
    # 고점(10-06) 뒤 이미 칸의 20% 를 씀 → 더 안 삼. 고점 전 매수는 세지 않음
    buys = [(date(2026, 9, 10), 100, 50.0), (date(2026, 10, 16), 200, 50.0), (date(2026, 10, 23), 200, 50.0)]
    p = tq(cash=75_000.0, qty=500, buys=buys)                 # 칸 = 75,000 + 500×50 = 100,000, 고점 뒤 산 돈 20,000
    o = v2.dip_plan(p, 50.0, 20, ndx(), 90.0, TODAY)
    assert o.side == "hold" and "한도" in o.note


def test_budget_caps_amount():
    buys = [(date(2026, 10, 16), 360, 50.0)]                   # 고점 뒤 18,000 씀 → 남은 한도 2,000
    p = tq(cash=82_000.0, qty=360, buys=buys)
    o = v2.dip_plan(p, 50.0, 20, ndx(), 90.0, TODAY)
    assert o.side == "buy" and abs(o.amount - 2_000) < 1e-6


def test_missing_index_data_is_safe():
    o = v2.dip_plan(tq(), 50.0, 20, None, None, TODAY)
    assert o.side == "hold"


def test_plan_us_replaces_cape_hold_with_dip_buy():
    pos = v2.build_ledger({"usd_capital": 100_000, "krw_capital": 0, "start_date": "2026-10-19"}, [])
    prices = {s.key: 50.0 for s in v2.SLEEVES}
    prices["QQQ"] = 90.0
    fear = {"target": .9, "S": .8, "rsi": 40}
    cape = {"hot": True, "cape": 38.0, "line": 30.0}
    brk = {k: [(TODAY - timedelta(days=i), 50.0) for i in range(400, 0, -1)] for k in v2.LADDER}
    orders = v2.plan_us(pos, 20, fear, cape, prices, TODAY, brk_hist=brk, start=date(2026, 10, 19), ndx_closes=ndx())
    tq_orders = [o for o in orders if o.sleeve == "TQQQ"]
    assert len(tq_orders) == 1 and tq_orders[0].side == "buy" and "공포 매수" in tq_orders[0].note
    # QLD·QQQ 는 여전히 CAPE 막기
    assert any(o.sleeve == "QLD" and o.side == "hold" and "CAPE" in o.note for o in orders)


def test_plan_us_no_dip_when_cape_open():
    pos = v2.build_ledger({"usd_capital": 100_000, "krw_capital": 0, "start_date": "2026-10-19"}, [])
    prices = {s.key: 50.0 for s in v2.SLEEVES}
    prices["QQQ"] = 90.0
    fear = {"target": .9, "S": .8, "rsi": 40}
    brk = {k: [(TODAY - timedelta(days=i), 50.0) for i in range(400, 0, -1)] for k in v2.LADDER}
    orders = v2.plan_us(pos, 20, fear, {"hot": False, "cape": 25, "line": 30}, prices, TODAY, brk_hist=brk,
                        start=date(2026, 10, 19), ndx_closes=ndx())
    tq_orders = [o for o in orders if o.sleeve == "TQQQ"]
    assert tq_orders and tq_orders[0].side == "buy" and "공포 매수" not in tq_orders[0].note   # 평소 곡선 매수


def test_kr_plan_uses_same_rule_on_sell_sleeve():
    import fabot_kr as K
    pos = K.build_ledger({"krw_capital": 500_000_000, "start_date": "2026-10-27", "gold_start": "2026-11-10"}, [])
    prices = {s.key: 20_000 for s in K.SLEEVES}
    fear = {"target": .9, "S": .8, "rsi": 40}
    div = [(TODAY - timedelta(days=i), 15_000.0) for i in range(400, 0, -1)]
    orders = K.plan(pos, 20, fear, {"hot": True, "cape": 38.0, "line": 30.0}, prices, TODAY, div, date(2026, 10, 27),
                    gold_start=date(2026, 11, 10), gold_rsi=70, ndx_closes=ndx() + [(TODAY - timedelta(days=1), 90.0)])
    tq_orders = [o for o in orders if o.sleeve == "TQ"]
    assert len(tq_orders) == 1 and tq_orders[0].side == "buy" and "공포 매수" in tq_orders[0].note
    assert any(o.sleeve == "QL" and o.side == "hold" and "CAPE" in o.note for o in orders)
