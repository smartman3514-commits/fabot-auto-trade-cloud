"""연말 비중 맞추기. 2026-11-10 인선 결정(fa-system week3_rebalance_design.md, 추천안대로):

1. 그해 마지막 거래일에 한 번. 목표 = (장부) 전체 평가액 × 칸 비중.
2. 넘친 칸: 그 칸 대기 현금에서 먼저 빼고, 모자라면 보유분을 판다.
3. 모자란 칸 — 계산 엔진(fa-journey strategy)과 똑같이:
   레버리지 2배(안 파는 칸) · 커버드콜 · 금 = 바로 산다 / 레버리지 3배(파는 칸) · 나스닥100 · 배당 = 그 칸 대기 현금으로 넣는다.
4. 한투 v2 는 같은 통화(달러 칸끼리 · 원화 칸끼리) 안에서만 맞춘다. 달러↔원화 차이는 보고만 한다(환전은 다음 판).
5. 엔진처럼, 어느 칸도 그 묶음의 0.5% 넘게 어긋나지 않으면 하지 않는다.
6. 12-29 저녁 미리보기 → 인선 확인(승인) → 12-30(국내)·12-31(미국) 마감 실행.

승인과 기록:
- `python rebalance.py --write` 가 계획 파일 rebalance_plans/{kr,v2}_{해}.json 을 만든다(approved=false).
- 인선이 확인하면 approved=true 로 바꿔 저장소에 올린다 — 이것이 '사람 승인'. **승인 없는 계획은 실행하지 않는다.**
- 실행: 계획에 적힌 날짜의 마감 실행 때 fabot_kr / fabot_v2 run_live 가 평소 주문 대신 계획의 매도·매수 수량만 낸다(그날은 다른 주문 쉼).
- 장부: 승인된 계획의 칸별 현금 옮김(move = 목표 − 그때 평가액, 묶음 합 0)을 그 날짜부터 build_ledger 가 더한다.
  매도·매수 자체는 평소처럼 trades 표에 기록되어 칸 현금을 바꾼다.

    python rebalance.py            # 두 계좌 계획 보기(주문 없음)
    python rebalance.py --write    # 계획 파일 만들기(approved=false)
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

BUY_NOW = {"curve", "peace", "gold"}      # 모자라면 바로 사는 규칙(엔진: QLD · 커버드콜 · 금). 나스닥100(curve)은 nasdaq_keys 로 뺀다
MIN_DRIFT = .005
PLAN_DIR = Path(os.environ.get("REBALANCE_PLAN_DIR") or Path(__file__).resolve().parent / "rebalance_plans")   # 리허설은 임시 폴더


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
    move: float = 0.0    # 장부에서 이 칸 현금에 더할 금액(목표 − 지금 평가액). 묶음 합 0


def _plan_group(pos: dict, prices: dict, keys: list[str], weights: dict[str, float], nasdaq_keys: set[str]) -> tuple[list[Step], float]:
    """같은 통화 칸들끼리 비중 맞추기. 돌려주는 것: 할 일 목록, 그 묶음 평가액."""
    vals = {k: pos[k].cash + pos[k].qty * prices[k] for k in keys}
    total = sum(vals.values())
    wsum = sum(weights[k] for k in keys)
    steps = []
    drift = max(abs(vals[k] - total * weights[k] / wsum) for k in keys) if keys else 0
    act = drift > MIN_DRIFT * total
    for k in keys:
        p, px = pos[k], prices[k]
        tgt = total * weights[k] / wsum
        diff = vals[k] - tgt
        name = p.sleeve.ledger
        if not act:
            steps.append(Step(k, name, vals[k], tgt, "none", note="어긋남이 0.5% 안 — 하지 않음")); continue
        if diff > 0:                                   # 넘침: 현금 먼저, 모자라면 판다
            from_cash = min(max(p.cash, 0.0), diff)
            rest = diff - from_cash
            q = min(int(p.qty), math.ceil(rest / px)) if rest > 0 and px > 0 else 0   # 팔 때는 올림(칸 현금이 음수가 되지 않게), 살 때는 내림
            if q > 0:
                steps.append(Step(k, name, vals[k], tgt, "sell", qty=q, amount=q * px, note=f"현금에서 {from_cash:,.0f} 빼고 {q}주 팖"))
            else:
                steps.append(Step(k, name, vals[k], tgt, "cash_out", amount=diff, note="칸 대기 현금에서 뺌"))
        elif diff < 0:                                 # 모자람
            need = -diff
            buy_now = p.sleeve.rule in BUY_NOW and k not in nasdaq_keys
            if buy_now and px > 0 and math.floor(need / px) > 0:
                q = math.floor(need / px)
                steps.append(Step(k, name, vals[k], tgt, "buy", qty=q, amount=q * px, note="엔진처럼 바로 삼"))
            else:
                steps.append(Step(k, name, vals[k], tgt, "cash_in", amount=need, note="칸 대기 현금으로 넣음(칸 규칙대로 삼)"))
        else:
            steps.append(Step(k, name, vals[k], tgt, "none"))
    if act:
        for st in steps:
            st.move = st.target - st.value
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


# ── 계획 파일 · 승인 · 장부 ────────────────────────────────────────────────

def last_trading_day(year: int, currency: str) -> str:
    """국내(KRW): 12-31 휴장이라 그 전 평일. 미국(USD): 그해 마지막 평일."""
    d = date(year, 12, 30 if currency == "KRW" else 31)
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() - 1)
    return d.isoformat()


def plan_path(key: str, year: int) -> Path:
    return PLAN_DIR / f"{key}_{year}.json"


def write_plan(key: str, account: str, groups: dict, prices: dict, fx: float | None, year: int, plan_dir: Path | None = None,
               approved: bool = False) -> Path:
    d = plan_dir or PLAN_DIR
    d.mkdir(exist_ok=True)
    out = {"account_key": key, "account": account, "made_at": datetime.now().isoformat(timespec="seconds"),
           "prices": prices, "fx": fx, "approved": approved, "approved_note": "리허설" if approved else "",
           "groups": {cur: {"date": g["date"], "steps": [asdict(s) for s in g["steps"]]} for cur, g in groups.items()}}
    p = d / f"{key}_{year}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _plans(key: str, plan_dir: Path | None = None) -> list[dict]:
    d = plan_dir or PLAN_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob(f"{key}_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"[경고] 비중 맞추기 계획 파일을 못 읽음({p.name}): {exc}")
    return out


def apply_moves(pos: dict, key: str, today: date | None = None, plan_dir: Path | None = None) -> None:
    """승인된 계획의 칸별 현금 옮김을, 그 날짜가 오늘이거나 지났으면 장부에 더한다(승인 안 된 계획은 무시)."""
    today = today or date.today()
    for plan in _plans(key, plan_dir):
        if not plan.get("approved"):
            continue
        for g in plan["groups"].values():
            if date.fromisoformat(g["date"]) > today:
                continue
            for st in g["steps"]:
                if st["sleeve"] in pos and st.get("move"):
                    pos[st["sleeve"]].cash += st["move"]; pos[st["sleeve"]].capital += st["move"]


def todays_orders(key: str, currency: str, today: date, plan_dir: Path | None = None) -> list[dict] | None:
    """오늘이 계획의 실행일이면 그 묶음의 매도·매수 목록(매도 먼저). 승인 전이면 None(경고만), 계획이 없으면 None."""
    for plan in _plans(key, plan_dir):
        g = plan.get("groups", {}).get(currency)
        if not g or g["date"] != today.isoformat():
            continue
        if not plan.get("approved"):
            print(f"-> 연말 비중 맞추기 {today} 계획이 있지만 승인 전 — 실행하지 않음(평소 규칙대로)")
            return None
        acts = [s for s in g["steps"] if s["action"] in ("sell", "buy") and s["qty"] > 0]
        return sorted(acts, key=lambda s: s["action"] != "sell")
    return None


# ── 보기 ───────────────────────────────────────────────────────────────

def show(title: str, steps: list[Step], money) -> None:
    print(f"\n== {title} ==")
    for st in steps:
        what = {"sell": f"{st.qty}주 팖(약 {money(st.amount)})", "buy": f"{st.qty}주 삼(약 {money(st.amount)})",
                "cash_in": f"현금 {money(st.amount)} 넣음", "cash_out": f"현금 {money(st.amount)} 뺌", "none": "그대로"}[st.action]
        print(f"  {st.sleeve:7s} {st.name:40s} 지금 {money(st.value):>16s} → 목표 {money(st.target):>16s} : {what}" + (f" — {st.note}" if st.note else ""))


def main() -> None:
    import argparse
    import crisis_plan
    import fabot_kr as K
    import fabot_v2 as V
    ap = argparse.ArgumentParser(description="연말 비중 맞추기 — 계획 보기(기본) · 계획 파일 만들기(--write)")
    ap.add_argument("--write", action="store_true", help="rebalance_plans/ 에 계획 파일을 만든다(approved=false)")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--rehearse", action="store_true",
                    help="리허설: 실행일을 오늘로, approved=true 로 만든다. REBALANCE_PLAN_DIR(임시 폴더)에서만 쓸 것 — 저장소에 올리지 않는다")
    args = ap.parse_args()
    if args.rehearse and not os.environ.get("REBALANCE_PLAN_DIR"):
        raise SystemExit("--rehearse 는 REBALANCE_PLAN_DIR(임시 폴더)를 정해야만 쓸 수 있어요")
    from datetime import timedelta, timezone
    kst_today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    day = (lambda cur: kst_today) if args.rehearse else (lambda cur: last_trading_day(args.year, cur))
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
    if args.write:
        print("계획 파일:", write_plan("kr", K.ACCOUNT, {"KRW": {"date": day("KRW"), "steps": ks}}, kpx, None, args.year, approved=args.rehearse))
    # 한투 v2
    vcfg = V.resolve_capital(V.load_config())
    vpos = V.build_ledger(vcfg, V.fetch_v2_trades())
    vpx = {s.key: (V.kis_us_price(s.code) if s.currency == "USD" else V.kis_kr_price(s.code)) for s in V.SLEEVES}
    fx = float(crisis_plan.fetch_usdkrw())
    su, sk, info = plan_v2(vpos, vpx, fx)
    print(f"\n[한투 v2] 달러 칸 {usd(info['usd_total'])} · 원화 칸 {won(info['krw_total'])} · 환율 {fx:,.1f}")
    show("한투 v2 달러 칸", su, usd)
    show("한투 v2 원화 칸", sk, won)
    gap = info["usd_gap"]
    print(f"\n  달러↔원화: 달러 칸이 목표보다 {usd(abs(gap))} {'많음' if gap > 0 else '적음'} — 올해는 보고만(환전 안 함)")
    if args.write:
        print("계획 파일:", write_plan("v2", V.V2_ACCOUNT, {"KRW": {"date": day("KRW"), "steps": sk},
                                                         "USD": {"date": day("USD"), "steps": su}}, vpx, fx, args.year, approved=args.rehearse))


if __name__ == "__main__":
    main()
