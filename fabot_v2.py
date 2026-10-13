"""FABOT v2 (규칙서 2026-10-13 오후판) — 키움 모의계좌에서 돌리는 새 버전의 장부·규칙.

인선 결정(2026-10-13): KIS는 지금 버전 그대로, 키움은 이 새 버전으로 실제 주문을 낸다.
키움 계좌에는 옛 보유분(TIGER 배당커버드콜 약 6,700주, 원화 약 3.5억)이 섞여 있으므로,
새 버전의 칸별 현금·보유는 **trades 표에서 account='키움 v2'로 남긴 기록만으로** 매번
처음부터 다시 계산한다(상태 파일 없음 — crisis_plan과 같은 방식).

이 파일은 계산만 한다(주문 없음). 주문은 auto_trade_loop_kiwoom.py가 이 계획을 받아 낸다.

    python fabot_v2.py              # 지금 시각 기준 "오늘 할 일"을 계산해 보여 준다(주문 없음)
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

import cooldown
import crisis_plan

V2_ACCOUNT = "키움 v2"
CONFIG_PATH = Path(__file__).resolve().parent / "v2_config.json"
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Sleeve:
    key: str          # 칸 이름
    code: str         # 키움 주문 코드
    exchange: str     # 해외: ND/NY, 국내: KRX
    ledger: str       # trades.ticker 에 남기는 이름
    weight: float     # 새 버전 전체 대비 비중
    currency: str     # USD / KRW
    rule: str         # curve / curve_sell / infinite / dca / peace


SLEEVES = [
    Sleeve("TQQQ", "TQQQ", "ND", "TQQQ", .15, "USD", "curve_sell"),
    Sleeve("SOXL", "SOXL", "NY", "SOXL", .10, "USD", "infinite"),
    Sleeve("QLD", "QLD", "NY", "QLD", .15, "USD", "curve"),
    Sleeve("QQQ", "QQQ", "ND", "QQQ", .30, "USD", "curve"),
    Sleeve("BRK", "BRKb", "NY", "BRK.B", .20, "USD", "dca"),
    Sleeve("CC_DIV", "472150", "KRX", "TIGER 배당커버드콜액티브(472150)", .05, "KRW", "peace"),
    Sleeve("CC_NAS", "486290", "KRX", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", .05, "KRW", "peace"),
]
USD_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "USD")   # 0.90
KRW_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "KRW")   # 0.10

# 규칙 숫자 (규칙서 10-13 오후판)
CURVE_BASE, CURVE_POW, CURVE_GAP = .20, 2.0, .05      # 목표 = 20% + 80%·S², 5%p 이상 모자라면 채움
S_FG_W, S_DD_W, S_RUN_W = .4, .4, .2
S_DD_FULL, S_RUN_FULL, FEAR_RUN_FG = .60, 60, 35
CAPE_K = 1.7
SELL_HALF, SELL_ALL, SELL_GAP_DAYS = 72, 77, 4
DCA_WEEKS = 26
PEACE_LO, PEACE_HI, PEACE_FRAC, PEACE_GAP_DAYS = 35, 65, .10, 3


# ── 시작 설정 ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """{"start_date": "2026-10-19", "usd_capital": 숫자|null, "krw_capital": 숫자|null, "fx": 숫자|null}
    usd_capital이 비어 있으면 아직 시작 전(미리보기)이다."""
    if not CONFIG_PATH.exists():
        return {"start_date": None, "usd_capital": None, "krw_capital": None, "fx": None}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def is_live(cfg: dict, today: date | None = None) -> bool:
    today = today or datetime.now(KST).date()
    return bool(cfg.get("usd_capital")) and cfg.get("start_date") and today >= date.fromisoformat(cfg["start_date"])


# ── 장부 ────────────────────────────────────────────────────────────────────

def fetch_v2_trades() -> list[dict]:
    env = cooldown._load_journal_env()
    r = requests.get(
        f"{env['SUPABASE_URL']}/rest/v1/trades",
        params={"select": "trade_date,ticker,action,quantity,price,fg_score,created_at",
                "account": f"eq.{V2_ACCOUNT}", "order": "created_at.asc"},
        headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@dataclass
class Position:
    sleeve: Sleeve
    capital: float
    qty: float = 0.0
    cost: float = 0.0       # 남은 보유분의 매입원가(평균단가 × 수량)
    cash: float = 0.0
    buys: list = field(default_factory=list)    # [(date, qty, price)]
    sells: list = field(default_factory=list)

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > 0 else 0.0


def build_ledger(cfg: dict, trades: list[dict]) -> dict[str, Position]:
    usd_cap = float(cfg.get("usd_capital") or 0)
    krw_cap = float(cfg.get("krw_capital") or 0)
    pos = {}
    for s in SLEEVES:
        cap = usd_cap * s.weight / USD_WEIGHT if s.currency == "USD" else krw_cap * s.weight / KRW_WEIGHT
        pos[s.key] = Position(s, cap, cash=cap)
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


# ── 공포 점수 S (파봇곡선) ────────────────────────────────────────────────────

def fetch_daily_closes(ticker: str, rng: str = "3y") -> list[tuple[date, float]]:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={rng}&interval=1d",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = []
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c:
            out.append((datetime.fromtimestamp(ts, tz=timezone.utc).date(), float(c)))
    return out


def fear_score(fg_now: float, fg_history: list[tuple[date, float]], tqqq_now: float,
               tqqq_closes: list[tuple[date, float]]) -> dict:
    """S = 0.4·F&G점수 + 0.4·낙폭점수 + 0.2·공포기간점수 (백테스트 curve와 같은 식)."""
    hi2 = max([c for _, c in tqqq_closes[-504:]] + [tqqq_now])
    dd = 1 - tqqq_now / hi2
    run = 0
    for _, v in reversed(fg_history):           # 오늘(마지막)부터 거꾸로, F&G<35가 이어진 날 수
        if v < FEAR_RUN_FG:
            run += 1
        else:
            break
    f_fg = min(1., max(0., (50 - fg_now) / 35))
    f_dd = min(1., max(0., dd / S_DD_FULL))
    f_run = min(1., run / S_RUN_FULL)
    S = S_FG_W * f_fg + S_DD_W * f_dd + S_RUN_W * f_run
    return {"S": S, "target": CURVE_BASE + (1 - CURVE_BASE) * S ** CURVE_POW, "dd": dd, "hi2": hi2,
            "run": run, "f_fg": f_fg, "f_dd": f_dd, "f_run": f_run}


# ── CAPE 막기 ────────────────────────────────────────────────────────────────

def fetch_cape_monthly() -> list[tuple[date, float]]:
    r = requests.get("https://www.multpl.com/shiller-pe/table/by-month",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    rows = re.findall(r"<td>([A-Z][a-z]{2} \d{1,2}, \d{4})</td>\s*<td>\s*(?:&#x2002;|<abbr[^>]*>[^<]*</abbr>)?\s*([\d.]+)",
                      r.text)
    out = sorted((datetime.strptime(d, "%b %d, %Y").date(), float(v)) for d, v in rows)
    if len(out) < 600:
        raise RuntimeError(f"CAPE 기록이 너무 적음({len(out)}개)")
    return out


def cape_block() -> dict:
    """최근 CAPE가 '1871년~직전 달까지 log(CAPE) 직선 추세' × 1.7을 넘으면 막는다.
    조회에 실패하면 **막는 쪽**으로 둔다(2026-10 현재 막힌 상태라 실패 시에도 같은 판정)."""
    try:
        cape = fetch_cape_monthly()
        vals = np.log([v for _, v in cape])
        t = np.arange(len(vals), dtype=float)
        b, a = np.polyfit(t[:-1], vals[:-1], 1)
        trend = math.exp(a + b * t[-1])
        last_d, last_v = cape[-1]
        return {"hot": last_v > trend * CAPE_K, "cape": last_v, "line": trend * CAPE_K, "as_of": last_d, "error": None}
    except Exception as exc:
        return {"hot": True, "cape": None, "line": None, "as_of": None, "error": f"CAPE 조회 실패 — 매수를 막는 쪽으로 둠: {exc}"}


# ── 오늘 할 일 ──────────────────────────────────────────────────────────────

@dataclass
class Order:
    sleeve: str
    side: str          # buy / sell
    amount: float      # 통화 금액(매수) — 매도는 0
    qty: float | None  # 매도 수량(매수는 실행 때 가격으로 계산)
    note: str


def plan_us(pos: dict[str, Position], fg_now: float, fear: dict, cape: dict,
            prices: dict[str, float], today: date) -> list[Order]:
    orders: list[Order] = []
    # TQQQ 매도 (F&G 72 절반 / 77 전부, 판 뒤 4거래일 쉼)
    tq = pos["TQQQ"]
    if fg_now >= SELL_HALF and tq.qty > 0:
        last_sell = max((d for d, _, _ in tq.sells), default=None)
        if last_sell and cooldown._trading_days_between(last_sell, today) < SELL_GAP_DAYS:
            orders.append(Order("TQQQ", "hold", 0, None, f"F&G {fg_now:.0f} 매도 조건이지만 {last_sell} 매도 뒤 쉬는 중"))
        else:
            q = tq.qty if fg_now >= SELL_ALL else math.floor(tq.qty / 2)
            if q > 0:
                orders.append(Order("TQQQ", "sell", 0, q, f"F&G {fg_now:.0f} ≥ {SELL_ALL if fg_now >= SELL_ALL else SELL_HALF} → {'전부' if fg_now >= SELL_ALL else '절반'} 매도"))
    # 파봇곡선 (TQQQ·QLD·QQQ)
    for k in ("TQQQ", "QLD", "QQQ"):
        p = pos[k]; px = prices[k]; inv = p.qty * px; tot = p.cash + inv
        if tot <= 0 or any(o.sleeve == k and o.side == "sell" for o in orders):
            continue
        share = inv / tot
        if share >= fear["target"] - CURVE_GAP:
            continue
        if cape["hot"]:
            orders.append(Order(k, "hold", 0, None, f"투자 비중 {share:.0%} < 목표 {fear['target']:.0%}이지만 CAPE 막기(CAPE {cape['cape']} > 기준선 {cape['line'] and round(cape['line'], 1)})"))
            continue
        amt = min(p.cash, fear["target"] * tot - inv)
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"투자 비중 {share:.0%} → 목표 {fear['target']:.0%} (S {fear['S']:.2f})"))
    # 버크셔: 26주 동안 매주 한 번, 남은 돈 ÷ 남은 주
    b = pos["BRK"]
    done = len(b.buys)
    this_week = today.isocalendar()[:2]
    bought_this_week = any(d.isocalendar()[:2] == this_week for d, _, _ in b.buys)
    if done < DCA_WEEKS and not bought_this_week and b.cash > 0:
        amt = b.cash / (DCA_WEEKS - done)
        orders.append(Order("BRK", "buy", amt, None, f"6개월 나눠 사기 {done + 1}/{DCA_WEEKS}주차"))
    elif done >= DCA_WEEKS and b.cash > prices["BRK"]:
        orders.append(Order("BRK", "buy", b.cash, None, "6개월 나눠 사기 끝 — 남은 돈(연말 리밸런싱분 등) 매수"))
    # SOXL 무한매수법 — 4단계에서 연결
    orders.append(Order("SOXL", "hold", 0, None, "무한매수법은 LOC 시험 뒤 연결 예정(지금은 현금 보유)"))
    return orders


def plan_kr(pos: dict[str, Position], fg_now: float, today: date) -> list[Order]:
    orders = []
    for k in ("CC_DIV", "CC_NAS"):
        p = pos[k]
        if not (PEACE_LO <= fg_now <= PEACE_HI):
            continue
        last = max((d for d, _, _ in p.buys), default=None)
        if last and cooldown._trading_days_between(last, today) < PEACE_GAP_DAYS:
            orders.append(Order(k, "hold", 0, None, f"평시 매수 조건이지만 {last} 매수 뒤 {PEACE_GAP_DAYS}거래일 쉬는 중"))
            continue
        amt = p.cash * PEACE_FRAC
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"평시(F&G {fg_now:.0f}) 남은 돈의 {PEACE_FRAC:.0%}"))
    return orders


def summary_lines(pos: dict[str, Position], prices: dict[str, float], fx: float) -> list[str]:
    usd = sum(p.cash + p.qty * prices[k] for k, p in pos.items() if p.sleeve.currency == "USD")
    krw = sum(p.cash + p.qty * prices[k] for k, p in pos.items() if p.sleeve.currency == "KRW")
    total = usd * fx + krw
    parts = []
    for k, p in pos.items():
        v = p.cash + p.qty * prices[k]
        v_krw = v * fx if p.sleeve.currency == "USD" else v
        parts.append(f"{k} {v_krw / total:.0%}(투자 {p.qty * prices[k] / v:.0%})" if v > 0 else f"{k} 0%")
    return [f"새 버전 평가 {total:,.0f}원 (달러 칸 ${usd:,.0f} · 원화 칸 {krw:,.0f}원, 환율 {fx:,.1f})",
            "칸별 비중: " + " · ".join(parts)]


def main() -> None:
    import argparse
    import today_signal
    import kiwoom_client as K
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["us", "kr", "both"], default="both")
    args = ap.parse_args()

    cfg = load_config()
    today = datetime.now(KST).date()
    fx = crisis_plan.fetch_usdkrw()
    live = is_live(cfg, today)
    if not live:
        # 시작 전 미리보기: 지금 키움 달러 + TQQQ 평가액을 새 버전 재원으로 가정
        cash = float(K.get_overseas_cash_balance(mode="demo").get("d0_usd_fx_entr", 0) or 0)
        bal = K.get_overseas_balance(stk_cd="TQQQ", stex_tp="ND", mode="demo")
        tq_qty = sum(int(h["poss_qty"]) for h in bal.get("result_list", []) if h.get("stk_cd") == "TQQQ")
        tq_px = K.abs_price(K.get_overseas_quote("TQQQ", mode="demo")["cur_prc"])
        usd_cap = cash + tq_qty * tq_px
        cfg = {"start_date": cfg.get("start_date"), "usd_capital": usd_cap,
               "krw_capital": usd_cap / USD_WEIGHT * KRW_WEIGHT * fx, "fx": fx}
        print(f"그림자(v2 미리보기, 시작일 {cfg['start_date']}): 재원 가정 ${usd_cap:,.0f} "
              f"(달러 ${cash:,.0f} + TQQQ {tq_qty}주 × ${tq_px:.2f}) + 원화 {cfg['krw_capital']:,.0f}원")
        trades = []
    else:
        trades = fetch_v2_trades()
    pos = build_ledger(cfg, trades)

    prices = {}
    for s in SLEEVES:
        if s.currency == "USD":
            prices[s.key] = K.abs_price(K.get_overseas_quote(s.code, exchange=s.exchange, mode="demo")["cur_prc"])
        else:
            prices[s.key] = K.abs_price(K.get_domestic_quote(s.code, mode="demo")["cur_prc"])

    info = today_signal.get_cnn_score()
    hist = crisis_plan.fetch_cnn_history()
    fear = fear_score(info["score"], hist, prices["TQQQ"], fetch_daily_closes("TQQQ"))
    cape = cape_block()
    tag = "그림자(v2)" if not live else "v2"
    print(f"{tag} F&G {info['score']:.1f} · S {fear['S']:.2f} → 목표 투자 비중 {fear['target']:.0%} "
          f"(TQQQ 2년 고점 ${fear['hi2']:.2f} 대비 −{fear['dd']:.0%}, 공포 {fear['run']}일) · "
          + (f"CAPE {cape['cape']} / 기준선 {cape['line']:.1f} → {'막힘' if cape['hot'] else '열림'}" if cape['cape'] else cape['error']))
    for line in summary_lines(pos, prices, fx):
        print(f"{tag} {line}")
    orders = []
    if args.market in ("us", "both"):
        orders += plan_us(pos, info["score"], fear, cape, prices, today)
    if args.market in ("kr", "both"):
        orders += plan_kr(pos, info["score"], today)
    for o in orders:
        cur = "$" if pos[o.sleeve].sleeve.currency == "USD" else ""
        amt = f" {cur}{o.amount:,.0f}{'' if cur else '원'}" if o.side == "buy" else (f" {o.qty:g}주" if o.side == "sell" else "")
        print(f"{tag} {o.sleeve} {({'buy': '매수', 'sell': '매도', 'hold': '대기'})[o.side]}{amt} — {o.note}")


if __name__ == "__main__":
    main()
