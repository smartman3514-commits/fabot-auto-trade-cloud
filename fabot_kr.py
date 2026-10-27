"""키움 국내포트 — v2 규칙(fabot_v2.py) 그대로, 상품만 국내 상장 ETF. 키움 모의 국내계좌(81372441)에서 돌린다.

2026-10-26 인선 결정: 키움 모의투자 해외 계좌가 다시 열리지 않아 v2는 한투로 옮기고, 키움 원화 5억은
v2 규칙을 국내 ETF로 옮긴 이 포트로 굴린다(백테스트 1998~: 연 15.6%, MDD −51.5%, 10년 75구간 손해 0개).

칸(비중)  상품                                             규칙 (숫자는 fabot_v2.py와 같음)
TQ  15%  TIGER 미국나스닥100레버리지(합성) 418660          FA곡선 + F&G 72 절반·77 전부 매도(매도 사이 4거래일) — v2 TQQQ 자리
QL  15%  같은 418660                                       FA곡선, 안 팖 — v2 QLD 자리(한국엔 3배가 없어 두 칸 모두 2배)
QQ  30%  KODEX 미국나스닥100 379810                         FA곡선, 안 팖 — v2 QQQM 자리
DIV 30%  TIGER 미국배당다우존스 458730                      사다리 — v2 SCHD 자리
CCD  5%  TIGER 배당커버드콜액티브 472150                    F&G 65 이하, 칸 현금 10%, 3거래일 간격
CCN  5%  TIGER 미국나스닥100타겟데일리커버드콜 486290       같음

신호(F&G·TQQQ 낙폭·RSI·CAPE)는 v2와 같은 것을 쓰고, 주문은 국내장 마감 창(15:05~15:35 KST)에만 낸다.
같은 상품(418660)을 두 칸이 나눠 가지므로 장부는 칸마다 다른 이름으로 남긴다. 칸별 현금·보유는
trades 표의 account='키움 국내포트' 기록만으로 매번 다시 계산한다(상태 파일 없음).

    python fabot_kr.py                 # 오늘 할 일 미리보기(주문 없음)
    python fabot_kr.py --market kr     # 실행 창과 상관없이 국내장 기준으로 계산
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

import requests

import cooldown
import crisis_plan
import fabot_v2 as V
from fabot_v2 import KST, Order, Position, Sleeve

ACCOUNT = "키움 국내포트"
CONFIG_PATH = Path(__file__).resolve().parent / "kr_config.json"
LEV = "TIGER 미국나스닥100레버리지(합성)(418660)"
SLEEVES = [
    Sleeve("TQ", "418660", "KRX", f"{LEV} 파는 칸", .15, "KRW", "curve_sell"),
    Sleeve("QL", "418660", "KRX", f"{LEV} 안 파는 칸", .15, "KRW", "curve"),
    Sleeve("QQ", "379810", "KRX", "KODEX 미국나스닥100(379810)", .30, "KRW", "curve"),
    Sleeve("DIV", "458730", "KRX", "TIGER 미국배당다우존스(458730)", .30, "KRW", "ladder"),
    Sleeve("CCD", "472150", "KRX", "TIGER 배당커버드콜액티브(472150)", .05, "KRW", "peace"),
    Sleeve("CCN", "486290", "KRX", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", .05, "KRW", "peace"),
]
DIV_YAHOO, DIV_NAME = "458730.KS", "TIGER 미국배당다우존스"
won = lambda v: f"{v:,.0f}원"


def load_config() -> dict:
    """{"start_date": "YYYY-MM-DD", "krw_capital": 숫자}"""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def is_live(cfg: dict, today: date | None = None) -> bool:
    today = today or datetime.now(KST).date()
    return bool(cfg.get("start_date")) and today >= date.fromisoformat(cfg["start_date"])


def fetch_trades() -> list[dict]:
    env = cooldown._load_journal_env()
    r = requests.get(
        f"{env['SUPABASE_URL']}/rest/v1/trades",
        params={"select": "trade_date,ticker,action,quantity,price,fg_score,created_at",
                "account": f"eq.{ACCOUNT}", "order": "created_at.asc"},
        headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def build_ledger(cfg: dict, trades: list[dict]) -> dict[str, Position]:
    cap = float(cfg["krw_capital"])
    pos = {s.key: Position(s, cap * s.weight, cash=cap * s.weight) for s in SLEEVES}
    by_ledger = {s.ledger: s.key for s in SLEEVES}
    for t in trades:
        k = by_ledger.get(t["ticker"])
        if k is None or t["action"] not in ("buy", "sell"):
            continue
        p = pos[k]; q = float(t["quantity"]); px = float(t["price"]); d = date.fromisoformat(t["trade_date"])
        if t["action"] == "buy":
            p.qty += q; p.cost += q * px; p.cash -= q * px; p.buys.append((d, q, px))
        else:
            fr = min(1.0, q / p.qty) if p.qty > 0 else 0.0
            p.cost -= p.cost * fr; p.qty -= q; p.cash += q * px; p.sells.append((d, q, px))
    return pos


def kiwoom_price(code: str) -> float:
    import kiwoom_client as K
    return K.abs_price(K.get_domestic_quote(code, mode="demo")["cur_prc"])


def gather(cfg: dict, trades: list[dict]) -> dict:
    import today_signal
    today = datetime.now(KST).date()
    pos = build_ledger(cfg, trades)
    prices = {s.key: kiwoom_price(s.code) for s in SLEEVES}
    info = today_signal.get_cnn_score()
    hist = crisis_plan.fetch_cnn_history()
    tq = V.fetch_daily_closes("TQQQ")          # 국내 마감 때 미국장은 닫혀 있다 → 지난밤 종가 기준
    fear = V.fear_score(info["score"], hist, tq[-1][1], tq)
    fear["rsi"] = V.rsi_wilder([c for _, c in tq])
    return {"pos": pos, "prices": prices, "fg": info["score"], "fear": fear, "cape": V.cape_block(),
            "div_hist": V.fetch_daily_closes(DIV_YAHOO, "2y"), "today": today, "tqqq_close": tq[-1]}


def plan(pos: dict[str, Position], fg: float, fear: dict, cape: dict, prices: dict[str, float],
         today: date, div_hist: list, start: date) -> list[Order]:
    """fabot_v2.plan_us + plan_kr를 국내 ETF 칸에 그대로 옮긴 것."""
    orders: list[Order] = []
    tq = pos["TQ"]
    if fg >= V.SELL_HALF and tq.qty > 0:
        last_sell = max((d for d, _, _ in tq.sells), default=None)
        if last_sell and cooldown._trading_days_between(last_sell, today) < V.SELL_GAP_DAYS:
            orders.append(Order("TQ", "hold", 0, None, f"F&G {fg:.0f} 매도 조건이지만 {last_sell} 매도 뒤 쉬는 중"))
        else:
            q = tq.qty if fg >= V.SELL_ALL else math.floor(tq.qty / 2)
            if q > 0:
                orders.append(Order("TQ", "sell", 0, q, f"F&G {fg:.0f} ≥ {V.SELL_ALL if fg >= V.SELL_ALL else V.SELL_HALF} → {'전부' if fg >= V.SELL_ALL else '절반'} 매도"))
    target = fear["target"]
    accel = fear.get("rsi") is not None and fear["rsi"] <= V.RSI_ACCEL_TH
    if accel:
        target = min(1.0, target + V.RSI_ACCEL_ADD)
    for k in ("TQ", "QL", "QQ"):
        p = pos[k]; px = prices[k]; inv = p.qty * px; tot = p.cash + inv
        if tot <= 0 or any(o.sleeve == k and o.side == "sell" for o in orders):
            continue
        share = inv / tot
        if share >= target - V.CURVE_GAP:
            continue
        if cape["hot"]:
            orders.append(Order(k, "hold", 0, None, f"투자 비중 {share:.0%} < 목표 {target:.0%}이지만 CAPE 막기(CAPE {cape['cape']} > 기준선 {cape['line'] and round(cape['line'], 1)})"))
            continue
        amt = min(p.cash, target * tot - inv)
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"투자 비중 {share:.0%} → 목표 {target:.0%} (S {fear['S']:.2f}" + (f", RSI {fear['rsi']:.0f} 가속" if accel else "") + ")"))
    lad = V.brk_ladder_plan(pos["DIV"], div_hist, prices["DIV"], start, today, name=DIV_NAME, money=won)
    if lad["order"] > 0:
        orders.append(Order("DIV", "buy", lad["order"], None, lad["note"]))
    elif lad["note"]:
        orders.append(Order("DIV", "hold", 0, None, lad["note"]))
    for k in ("CCD", "CCN"):
        p = pos[k]
        if not (V.PEACE_LO <= fg <= V.PEACE_HI):
            continue
        last = max((d for d, _, _ in p.buys), default=None)
        if last and cooldown._trading_days_between(last, today) < V.PEACE_GAP_DAYS:
            orders.append(Order(k, "hold", 0, None, f"F&G {V.PEACE_HI} 이하 매수 조건이지만 {last} 매수 뒤 {V.PEACE_GAP_DAYS}거래일 쉬는 중"))
            continue
        if p.cash * V.PEACE_FRAC > 0:
            orders.append(Order(k, "buy", p.cash * V.PEACE_FRAC, None, f"F&G {fg:.0f} ≤ {V.PEACE_HI} — 남은 돈의 {V.PEACE_FRAC:.0%}"))
    return orders


def print_status(cfg: dict, ctx: dict) -> None:
    f, c, pos, prices = ctx["fear"], ctx["cape"], ctx["pos"], ctx["prices"]
    acc = f" → RSI 가속 +{V.RSI_ACCEL_ADD:.0%}p" if f["rsi"] <= V.RSI_ACCEL_TH else ""
    cape_txt = f"CAPE {c['cape']} / 기준선 {c['line']:.1f} → {'막힘' if c['hot'] else '열림'}" if c["cape"] else c["error"]
    print(f"국내포트 F&G {ctx['fg']:.1f} · S {f['S']:.2f} → 목표 투자 비중 {f['target']:.0%} · "
          f"TQQQ RSI {f['rsi']:.0f}(종가 {ctx['tqqq_close'][0]}){acc} · {cape_txt}")
    total = sum(p.cash + p.qty * prices[k] for k, p in pos.items())
    parts = [f"{k} {(p.cash + p.qty * prices[k]) / total:.0%}(투자 {p.qty * prices[k] / (p.cash + p.qty * prices[k]):.0%})"
             for k, p in pos.items()]
    print(f"국내포트 평가 {total:,.0f}원 · 칸별: " + " · ".join(parts))


# ── 주문 (키움 국내) ────────────────────────────────────────────────────────

def _say(msg: str) -> None:
    print(f"-> 국내포트 {msg}")   # 텔레그램 요약이 "-> " 줄을 집어 간다


def _holding_qty(code: str) -> int:
    import kiwoom_client as K
    for h in K.get_domestic_holdings(mode="demo").get("acnt_evlt_remn_indv_tot", []):
        if h["stk_cd"].lstrip("A") == code:
            return int(h["rmnd_qty"])
    return 0


def _execute(group: list[tuple[Sleeve, Order]], side: str, fg: float, dry_run: bool) -> dict | None:
    """같은 상품·같은 방향 주문을 한 번에 낸다(418660은 두 칸이 같이 살 수 있음). 체결 수량은 칸 순서대로 나눈다.
    호가 추격은 동시호가 직전(15:19:30)까지만 하고, 남은 수량은 1% 넉넉한 지정가로 한 번 낸다.
    돌려주는 값: 덜 체결돼 마지막 정산(V.kr_settle)에 넘길 값, 다 끝났으면 None."""
    import kiwoom_client as K
    import fill_price
    from auto_trade_loop_kiwoom import KiwoomChaseOrder
    from cooldown import log_trade
    code = group[0][0].code
    book_key = "sel_fpr_bid" if side == "buy" else "buy_fpr_bid"
    ref = int(K.abs_price(K.get_domestic_orderbook(code, mode="demo").get(book_key, "0") or "0"))
    if ref <= 0:
        _say(f"{code} {'매수' if side == 'buy' else '매도'} 못 함 — 호가 0(국내 정규장 아님)")
        return None
    want = [(sl, o, int(o.qty) if side == "sell" else int(o.amount // ref), o.note) for sl, o in group]
    for sl, o, q, _ in want:
        if q < 1:
            _say(f"{sl.key} 매수 건너뜀 — {o.amount:,.0f}원으로는 1주({ref:,}원)도 못 삼")
    want = [w for w in want if w[2] >= 1]
    total = sum(w[2] for w in want)
    if total < 1:
        return None
    if dry_run:
        for sl, _, q, note in want:
            _say(f"{sl.key} {'매수' if side == 'buy' else '매도'} {q}주 @ 약 {ref:,}원 (dry-run, 주문 안 냄) — {note}")
        return None
    before = _holding_qty(code)
    ch = KiwoomChaseOrder(code, side, total, max_reprices=30, max_seconds=V.kr_chase_seconds(120.0), poll_interval=3.0)
    if ch.max_seconds > 0:
        try:
            ch.run()
        except RuntimeError as exc:   # 정정·취소 실패로 멈춰도 마지막 정산에서 보유 수량으로 다시 센다
            print(f"  호가 추격 중단: {exc}")
    if ch.filled_qty < total:
        rest = total - ch.filled_qty
        now_ref = int(K.abs_price(K.get_domestic_orderbook(code, mode="demo").get(book_key, "0") or "0"))
        px = V.kr_auction_price(max(ref, now_ref) if side == "buy" else min(ref, now_ref or ref), side)
        r = K.place_domestic_order(code, side, rest, price=px, mode="demo")
        if r.get("return_code") == 0:
            ch.__dict__.setdefault("order_nos", []).append(r["ord_no"])
            print(f"  넉넉한 지정가 주문: {side} {rest}주 @ {px:,}원 (1% {'위' if side == 'buy' else '아래'}, 주문번호 {r['ord_no']})")
        else:
            print(f"  넉넉한 지정가 주문 실패: {r.get('return_msg')}")

    def held() -> int:
        d = _holding_qty(code) - before
        return d if side == "buy" else -d

    def record(filled: int) -> None:
        px = fill_price.safe(fill_price.kiwoom_domestic, code, getattr(ch, "order_nos", [])) or ref
        left = filled
        for sl, _, q, note in want:
            n = min(q, left); left -= n
            if n <= 0:
                continue
            log_trade(ticker=sl.ledger, action=side, quantity=n, price=round(px, 4), fg_score=fg,
                      memo=f"국내포트 자동실행 — {note}", account=ACCOUNT)
            _say(f"{sl.key} {'매수' if side == 'buy' else '매도'} {n}/{q}주 @ {px:,.0f}원 — {note}")
            print(f"매매기록 저장 완료: {ACCOUNT} {side} {sl.ledger} {n}주")
    return V._kr_result(code, total, ch.filled_qty, held, record)


def run_live(dry_run: bool = False, force_market: str | None = None) -> None:
    """auto_trade_loop_kiwoom.py가 시작일부터 부른다. 국내장 마감 창에서만 계산하고 주문한다."""
    from auto_trade_loop_kiwoom import _within_domestic_close_window
    cfg = load_config()
    print(f"=== [{ACCOUNT}] v2 규칙 · 국내 ETF (시작 {cfg['start_date']}, 재원 {cfg['krw_capital']:,.0f}원) ===")
    if not (force_market == "kr" or _within_domestic_close_window()):
        print("최종 신호: 국내포트는 국내장 마감 창(15:05~15:35 KST)에서만 실행 — 건너뜁니다")
        return
    ctx = gather(cfg, fetch_trades())
    print_status(cfg, ctx)
    print("최종 신호: 국내포트 국내장 마감 실행" + (" (dry-run)" if dry_run else ""))
    orders = plan(ctx["pos"], ctx["fg"], ctx["fear"], ctx["cape"], ctx["prices"], ctx["today"],
                  ctx["div_hist"], date.fromisoformat(cfg["start_date"]))
    if not any(o.side in ("buy", "sell") for o in orders):
        _say("오늘 살 것·팔 것 없음")
    for o in orders:
        if o.side == "hold":
            _say(f"{o.sleeve} 대기 — {o.note}")
    sl = {s.key: s for s in SLEEVES}
    groups: dict[tuple[str, str], list] = {}
    for o in orders:
        if o.side in ("buy", "sell"):
            groups.setdefault((sl[o.sleeve].code, o.side), []).append((sl[o.sleeve], o))
    pending: list = []
    for (code, side), group in sorted(groups.items(), key=lambda kv: kv[0][1] != "sell"):   # 파는 것 먼저
        if any(p and p["key"] == code for p in pending):
            _say(f"{code} {side} 건너뜀 — 앞선 같은 상품 주문의 체결을 아직 확인 중")
            continue
        pending.append(_execute(group, side, ctx["fg"], dry_run))
    V.kr_settle(pending)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="키움 국내포트 — 오늘 할 일 미리보기(기본, 주문 없음) 또는 실행")
    ap.add_argument("--market", choices=["kr"], help="실행 창과 상관없이 국내장 기준으로 계산")
    ap.add_argument("--live", action="store_true", help="실제 모의 주문을 낸다(기본은 주문 없이 보여 주기만)")
    args = ap.parse_args()
    run_live(dry_run=not args.live, force_market=args.market)


if __name__ == "__main__":
    main()
