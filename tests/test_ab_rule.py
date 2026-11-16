"""A+B (2026-11-16 인선 결정) — A: TQQQ 가 1년 최고가보다 30% 넘게 아래면 탐욕 매도 쉼.
B: 탐욕으로 판 뒤엔 그 판 값보다 낮게 마감하기 전까지(최대 252거래일) 곡선이 TQQQ 칸을 최소 비중(20%)까지만 채움.
엔진 근거: fa-system/day24 (AB: 공격형 1998~ 17.68% · MDD −35.4%, 기준 ①③ 미달을 알고 결정)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402

TODAY = date(2026, 11, 5)          # 목요일
SOLD = date(2026, 11, 2)           # 월요일


def closes(after_sale):
    """10월은 110, 판 날(11-02)부터는 after_sale 값들."""
    out, d = [], date(2026, 10, 1)
    while d < SOLD:
        if d.weekday() < 5:
            out.append((d, 110.0))
        d += timedelta(days=1)
    days = [SOLD + timedelta(days=i) for i in range(0, 4)]
    return out + list(zip(days, after_sale))


def test_greed_sell_memo_detected():
    assert v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행(한투) — F&G 78 ≥ 77 → 칸의 최소 비중 20%만 남기고 줄임(200주 남김)"})
    assert v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행 — F&G 73 ≥ 72 → 절반 매도 (가격은 마지막 주문가)"})
    assert not v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행(한투) — 연말 비중 맞추기 — 300주 팖"})
    assert not v2.is_greed_sell({"action": "sell", "memo": "v2 자동실행 — 시작일 정리: 옛 규칙 TQQQ 10주 매도(재원으로 씀)"})
    assert not v2.is_greed_sell({"action": "buy", "memo": "F&G 20 ≥ 72"})


def test_bear_skip_threshold():
    assert v2.bear_skip({"hi1": 100.0, "tq_now": 69.0}) and "31%" in v2.bear_skip({"hi1": 100.0, "tq_now": 69.0})
    assert v2.bear_skip({"hi1": 100.0, "tq_now": 71.0}) is None
    assert v2.bear_skip({}) is None                       # 1년 최고가를 모르면 원래대로 판다


def _pos(qty=600, cash=40_000, greed=None):
    pos = v2.build_ledger({"usd_capital": 100_000, "krw_capital": 0, "start_date": "2026-10-19"}, [])
    pos["TQQQ"].qty, pos["TQQQ"].cash = qty, cash
    pos["TQQQ"].greed_sells = greed or []
    return pos


def _us(pos, fg, fear, tq_closes=None, hot=False):
    prices = {s.key: 100.0 for s in v2.SLEEVES}
    brk = {k: [(TODAY - timedelta(days=i), 50.0) for i in range(400, 0, -1)] for k in v2.LADDER}
    return [o for o in v2.plan_us(pos, fg, {"S": .5, "rsi": 60, **fear}, {"hot": hot, "cape": 25, "line": 30}, prices, TODAY,
                                  brk_hist=brk, start=date(2026, 10, 19), ndx_closes=None, tq_closes=tq_closes) if o.sleeve == "TQQQ"]


def test_a_skips_greed_sell_in_bear_market():
    o = _us(_pos(), 78, {"target": .2, "hi1": 200.0, "tq_now": 100.0})          # 1년 최고가 대비 −50%
    assert len(o) == 1 and o[0].side == "hold" and "A+B 의 A" in o[0].note


def test_a_sells_normally_when_not_bear():
    o = _us(_pos(), 78, {"target": .2, "hi1": 120.0, "tq_now": 100.0})          # −17% → 판다
    assert o[0].side == "sell" and o[0].qty == 400


def test_sale_day_keeps_only_base_floor_even_if_curve_higher():
    # 곡선 목표가 50% 여도 판 날은 20%(CURVE_BASE)만 남김 — 엔진 AB 와 같음
    o = _us(_pos(), 78, {"target": .5, "hi1": 120.0, "tq_now": 100.0})
    assert o[0].side == "sell" and o[0].qty == 400 and "20%" in o[0].note


def test_b_caps_curve_until_price_below_sale():
    # 11-02 에 $100 에 탐욕 매도, 그 뒤 계속 100 이상 → 곡선 목표 60% 여도 20% 까지만(이미 20% → 안 삼)
    pos = _pos(qty=200, cash=80_000, greed=[(SOLD, 99.0)])
    o = _us(pos, 40, {"target": .6, "hi1": 120.0, "tq_now": 100.0}, tq_closes=closes([100.0, 101.0, 102.0, 100.5]))
    assert len(o) == 1 and o[0].side == "hold" and "A+B 의 B" in o[0].note


def test_b_released_after_close_below_sale_price():
    pos = _pos(qty=200, cash=80_000, greed=[(SOLD, 99.0)])
    o = _us(pos, 40, {"target": .6, "hi1": 120.0, "tq_now": 100.0}, tq_closes=closes([100.0, 98.0, 102.0, 100.5]))
    assert o and o[0].side == "buy"                                              # 11-03 에 98 < 99 로 마감 → 풀림


def test_b_released_after_252_trading_days():
    pos = _pos(qty=200, cash=80_000, greed=[(TODAY - timedelta(days=400), 99.0)])
    o = _us(pos, 40, {"target": .6, "hi1": 120.0, "tq_now": 100.0}, tq_closes=closes([100.0, 101.0, 102.0, 100.5]))
    assert o and o[0].side == "buy"


def test_b_not_released_on_sale_day():
    p = v2.Position(v2.SLEEVES[0], capital=100_000, cash=80_000, qty=200)
    p.greed_sells = [(TODAY, 105.0)]
    assert v2.refill_cap(p, 100.0, None, TODAY) is not None                    # 판 그날은 값이 낮아도 안 풀림


def test_kr_plan_a_and_b():
    import fabot_kr as K
    pos = K.build_ledger({"krw_capital": 500_000_000, "start_date": "2026-10-27", "gold_start": "2026-11-10"}, [])
    pos["TQ"].qty, pos["TQ"].cash = 3_000, 40_000_000
    prices = {s.key: 20_000 for s in K.SLEEVES}
    div = [(TODAY - timedelta(days=i), 15_000.0) for i in range(400, 0, -1)]
    args = dict(gold_start=date(2026, 11, 10), gold_rsi=70, ndx_closes=None)
    o = [x for x in K.plan(pos, 78, {"target": .2, "S": 0.0, "rsi": 60, "hi1": 200.0, "tq_now": 100.0}, {"hot": False, "cape": 25, "line": 30},
                           prices, TODAY, div, date(2026, 10, 27), **args) if x.sleeve == "TQ"]
    assert o[0].side == "hold" and "A+B 의 A" in o[0].note
    pos["TQ"].qty, pos["TQ"].cash = 1_000, 80_000_000
    pos["TQ"].greed_sells = [(SOLD, 19_000.0)]
    o = [x for x in K.plan(pos, 40, {"target": .6, "S": .5, "rsi": 60, "hi1": 120.0, "tq_now": 100.0}, {"hot": False, "cape": 25, "line": 30},
                           prices, TODAY, div, date(2026, 10, 27), tq_closes=[(SOLD + timedelta(days=1), 20_100.0)], **args) if x.sleeve == "TQ"]
    assert o[0].side == "hold" and "A+B 의 B" in o[0].note
