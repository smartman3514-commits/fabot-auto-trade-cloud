"""연말 비중 맞추기 — 계획만 계산한다(주문 없음). 2026-11-10 인선 결정(fa-system week3_rebalance_design.md, 추천안대로):

1. 그해 마지막 거래일에 한 번. 목표 = (장부) 전체 평가액 × 칸 비중.
2. 넘친 칸: 그 칸 대기 현금에서 먼저 빼고, 모자라면 보유분을 판다.
3. 모자란 칸 — 계산 엔진(fa-journey strategy)과 똑같이:
   레버리지 2배(안 파는 칸) · 커버드콜 · 금 = 바로 산다 / 레버리지 3배(파는 칸) · 나스닥100 · 배당 = 그 칸 대기 현금으로 넣는다.
4. 한투 v2 는 같은 통화(달러 칸끼리 · 원화 칸끼리) 안에서만 맞춘다. 달러↔원화 차이는 보고만 한다(환전은 다음 판).
5. 엔진처럼, 어느 칸도 전체의 0.5% 넘게 어긋나지 않으면 하지 않는다.
6. 12-29 저녁 미리보기 → 인선 확인 → 12-30 실행(실행 코드는 다음 단계 — 이 파일은 계획만 만든다).

    python rebalance.py            # 두 계좌 계획 보기(주문 없음)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BUY_NOW = {"curve", "peace", "gold"}      # 모자라면 바로 사는 규칙(엔진: QLD · 커버드콜 · 금)
TO_CASH = {"curve_sell", "dca", "ladder"}  # 모자라면 그 칸 대기 현금으로(엔진: TQQQ · 배당) — 나스닥100(curve) 은 아래에서 따로
MIN_DRIFT = .005


@dataclass
class Step:
    sleeve: str
    name: str
    value: float
    target: float
    action: str          # "sell" / "buy" / "cash_in" / "cash_out" / "none"
    qty: int = 0
    amount: float = 0.0
    note: str = ""


def _plan_group(pos: dict, prices: dict, keys: list[str], weights: dict[str, float], nasdaq_keys: set[str]) -> tuple[list[Step], float]:
    """같은 통화 칸들끼리 비중 맞추기. 돌려주는 것: 할 일 목록, 그 묶음 평가액."""
    vals = {k: pos[k].cash + pos[k].qty * prices[k] for k in keys}
    total = sum(vals.values())
    wsum = sum(weights[k] for k in keys)
    steps = []
    drift = max(abs(vals[k] - total * weights[k] / wsum) for k in keys) if keys else 0
    for k in keys:
        p, px = pos[k], prices[k]
        tgt = total * weights[k] / wsum
        diff = vals[k] - tgt
        name = p.sleeve.ledger
        if drift <= MIN_DRIFT * total:
            steps.append(Step(k, name, vals[k], tgt, "none", note="어긋남이 0.5% 안 — 하지 않음")); continue
        if diff > 0:                                   # 넘침: 현금 먼저, 모자라면 판다
            from_cash = min(max(p.cash, 0.0), diff)
            rest = diff - from_cash
            q = math.floor(rest / px) if rest > 0 and px > 0 else 0
            if q > 0:
                steps.append(Step(k, name, vals[k], tgt, "sell", qty=q, amount=q * px,
                                  note=f"현금에서 {from_cash:,.0f} 빼고 {q}주 팖"))
            else:
                steps.append(Step(k, name, vals[k], tgt, "cash_out", amount=diff, note="칸 대기 현금에서 뺌"))
        elif diff < 0:                                 # 모자람
            need = -diff
            rule = p.sleeve.rule
            buy_now = rule in BUY_NOW and k not in nasdaq_keys
            if buy_now and px > 0 and math.floor(need / px) > 0:
                q = math.floor(need / px)
                steps.append(Step(k, name, vals[k], tgt, "buy", qty=q, amount=q * px, note="엔진처럼 바로 삼"))
            else:
                steps.append(Step(k, name, vals[k], tgt, "cash_in", amount=need, note="칸 대기 현금으로 넣음(칸 규칙대로 삼)"))
        else:
            steps.append(Step(k, name, vals[k], tgt, "none"))
    return steps, total


def plan_kr(pos: dict, prices: dict) -> tuple[list[Step], float]:
    import fabot_kr as K
    keys = [s.key for s in K.SLEEVES]
    return _plan_group(pos, prices, keys, {s.key: s.weight for s in K.SLEEVES}, nasdaq_keys={"QQ"})


def plan_v2(pos: dict, prices: dict, fx: float) -> tuple[list[Step], list[Step], dict]:
    import fabot_v2 as V
    usd = [s.key for s in V.SLEEVES if s.currency == "USD"]
    krw = [s.key for s in V.SLEEVES if s.currency == "KRW"]
    w = {s.key: s.weight for s in V.SLEEVES}
    su, tu = _plan_group(pos, prices, usd, w, nasdaq_keys={"QQQ"})
    sk, tk = _plan_group(pos, prices, krw, w, nasdaq_keys=set())
    whole = tu * fx + tk
    gap_usd = tu - whole * V.USD_WEIGHT / fx       # +면 달러 칸이 목표보다 많음
    return su, sk, {"usd_total": tu, "krw_total": tk, "fx": fx, "usd_gap": gap_usd}


def show(title: str, steps: list[Step], money) -> None:
    print(f"\n== {title} ==")
    for st in steps:
        what = {"sell": f"{st.qty}주 팖(약 {money(st.amount)})", "buy": f"{st.qty}주 삼(약 {money(st.amount)})",
                "cash_in": f"현금 {money(st.amount)} 넣음", "cash_out": f"현금 {money(st.amount)} 뺌", "none": "그대로"}[st.action]
        print(f"  {st.sleeve:7s} {st.name:40s} 지금 {money(st.value):>16s} → 목표 {money(st.target):>16s} : {what}" + (f" — {st.note}" if st.note else ""))


def main() -> None:
    import crisis_plan
    import fabot_kr as K
    import fabot_v2 as V
    won = lambda v: f"{v:,.0f}원"
    usd = lambda v: f"${v:,.2f}"
    print("연말 비중 맞추기 — 미리보기(주문 없음). 지금 값으로 했다면.")
    # 키움 국내포트
    kcfg = K.load_config()
    kpos = K.build_ledger(kcfg, K.fetch_trades())
    kpx = {s.key: K.kiwoom_price(s.code) for s in K.SLEEVES}
    ks, kt = plan_kr(kpos, kpx)
    print(f"\n[키움 국내포트] 장부 평가 {won(kt)}")
    show("키움 국내포트", ks, won)
    # 한투 v2
    vcfg = V.resolve_capital(V.load_config())
    vpos = V.build_ledger(vcfg, V.fetch_v2_trades())
    vpx = {s.key: (V.kis_us_price(s.code) if s.currency == "USD" else V.kis_kr_price(s.code)) for s in V.SLEEVES}
    fx = float(crisis_plan.fetch_usdkrw())
    su, sk, info = plan_v2(vpos, vpx, fx)
    print(f"\n[한투 v2] 달러 칸 {usd(info['usd_total'])} · 원화 칸 {won(info['krw_total'])} · 환율 {fx:,.1f}")
    show("한투 v2 달러 칸", su, usd)
    show("한투 v2 원화 칸", sk, won)
    print(f"\n  달러↔원화: 달러 칸이 목표보다 {usd(info['usd_gap'])} {'많음' if info['usd_gap'] > 0 else '적음'} — 올해는 보고만(환전 안 함)")


if __name__ == "__main__":
    main()
