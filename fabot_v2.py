"""FA v2 (규칙서 2026-10-15판) — 키움 모의계좌에서 돌리는 새 버전의 장부·규칙·주문.

2026-10-15 추가: FA곡선 RSI 가속(TQQQ 일봉 RSI(14) ≤ 30이면 목표 비중 +40%p), 버크셔 사다리식 매수,
실제 주문 실행(run_live). SOXL 수익 30분할은 아직 연결 전(현금 보유).

인선 결정(2026-10-13): KIS는 지금 버전 그대로, 키움은 이 새 버전으로 실제 주문을 낸다.
키움 계좌에는 옛 보유분(TIGER 배당커버드콜 약 6,700주, 원화 약 3.5억)이 섞여 있으므로,
새 버전의 칸별 현금·보유는 **trades 표에서 account='키움 v2'로 남긴 기록만으로** 매번
처음부터 다시 계산한다(상태 파일 없음 — crisis_plan과 같은 방식).

시작일(v2_config.json start_date)부터 auto_trade_loop_kiwoom.py가 옛 규칙 대신 run_live()를 부른다.

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


# 버크셔 칸에 넣을 종목 — v2_config.json "defense_stock": "BRK" | "SCHD" (2026-10-16 인선 요청).
# 칸 이름(key)은 "BRK" 그대로 두고 주문 코드·장부 이름·시세만 바꾼다. 사다리 규칙은 같다.
# 이미 산 종목이 있는데 바꾸면 예전 종목은 장부에서 빠지므로, 바꾸기 전에 먼저 판다(plan_us가 막는다).
DEFENSE_CHOICES = {
    "BRK": {"code": "BRKb", "exchange": "NY", "ledger": "BRK.B", "yahoo": "BRK-B", "name": "버크셔"},
    "SCHD": {"code": "SCHD", "exchange": "NY", "ledger": "SCHD", "yahoo": "SCHD", "name": "SCHD"},
}


def _defense_key() -> str:
    try:
        v = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("defense_stock") if CONFIG_PATH.exists() else None
    except (OSError, ValueError):
        v = None
    v = str(v or "BRK").upper()
    return v if v in DEFENSE_CHOICES else "BRK"


DEFENSE = DEFENSE_CHOICES[_defense_key()]
STRAY_DEFENSE: dict[str, float] = {}   # 칸 종목을 바꾼 뒤에도 남아 있는 예전 종목 수량

SLEEVES = [
    Sleeve("TQQQ", "TQQQ", "ND", "TQQQ", .15, "USD", "curve_sell"),
    Sleeve("SOXL", "SOXL", "NY", "SOXL", .05, "USD", "infinite"),   # 10-19 인선: 10 → 5
    Sleeve("QLD", "QLD", "NY", "QLD", .15, "USD", "curve"),
    Sleeve("QQQ", "QQQM", "ND", "QQQM", .30, "USD", "curve"),   # 10-19 인선: QQQ → QQQM(같은 나스닥100, 1주 약 $290이라 작은 금액도 규칙에 가깝게)
    Sleeve("BRK", DEFENSE["code"], DEFENSE["exchange"], DEFENSE["ledger"], .25, "USD", "dca"),   # 10-19 인선: 20 → 25 (SOXL에서 옮김)
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
DCA_WEEKS = 26          # (옛 규칙 — 10-15부터 버크셔는 사다리식)
RSI_LEN, RSI_ACCEL_TH, RSI_ACCEL_ADD = 14, 30, .40      # FA곡선 RSI 가속 (TQQQ 일봉)
LAD_RSI, LAD_UNIT, LAD_GAP, LAD_DEADLINE = 40, .10, 21, 252
LAD_DOWN = [(.90, 1.5), (.85, 2.0), (.80, 2.5), (.75, 3.0), (.70, 3.5), (.65, 4.0), (.60, 4.5), (.55, 5.0), (.50, 5.5)]
LAD_UP = [1 + .05 * k for k in range(2, 11)]           # +10%, +15%, … +50% → 1회분씩
OVERSEAS_FEE_MARGIN = .011
PEACE_LO, PEACE_HI, PEACE_FRAC, PEACE_GAP_DAYS = 35, 65, .10, 3
QQQ_RESERVE, QQQ_RESERVE_FG = .30, 15   # 10-19 인선: QQQ(QQQM) 칸 30%는 F&G 15 이하(진짜 패닉)에서만 쓴다


# ── 시작 설정 ────────────────────────────────────────────────────────────────

def _load_config_file() -> dict:
    """{"start_date": "2026-10-19", "usd_capital": 숫자|null, "krw_capital": 숫자|null, "fx": 숫자|null}
    usd_capital이 비어 있으면 아직 시작 전(미리보기)이다."""
    if not CONFIG_PATH.exists():
        return {"start_date": None, "usd_capital": None, "krw_capital": None, "fx": None}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_config() -> dict:
    """미리보기(--start)로 덮어쓴 값이 있으면 그것을, 없으면 v2_config.json을 읽는다."""
    return dict(CONFIG_OVERRIDE) if CONFIG_OVERRIDE else _load_config_file()


def is_live(cfg: dict, today: date | None = None) -> bool:
    """시작일 당일부터 True. 재원은 resolve_capital()이 매번 같은 값으로 다시 계산한다(상태 파일 없음)."""
    today = today or datetime.now(KST).date()
    return bool(cfg.get("start_date")) and today >= date.fromisoformat(cfg["start_date"])


def _close_on(ticker: str, day: date) -> tuple[date, float]:
    closes = fetch_daily_closes(ticker, "3mo")
    vals = [(d, c) for d, c in closes if d <= day]
    if not vals:
        raise RuntimeError(f"{ticker} {day} 종가를 못 찾음")
    return vals[-1]


def resolve_capital(cfg: dict) -> dict:
    """재원 = 시작 전 달러 예수금 + 옛 TQQQ 수량 × price_date 종가, 원화 칸 = 달러 재원 ÷ 0.9 × 0.1 × 환율.
    usd_capital이 적혀 있으면 그 값을 쓴다. 같은 입력이면 언제 다시 계산해도 같은 값이 나온다."""
    if cfg.get("usd_capital"):
        fx = float(cfg.get("fx") or crisis_plan.fetch_usdkrw())
        return {**cfg, "fx": fx, "krw_capital": cfg.get("krw_capital") or cfg["usd_capital"] / USD_WEIGHT * KRW_WEIGHT * fx}
    pd_ = date.fromisoformat(cfg["price_date"])
    tq_d, tq = _close_on("TQQQ", pd_)
    _, fx = _close_on("KRW=X", pd_)
    usd = float(cfg["usd_cash_at_start"]) + int(cfg["legacy_tqqq_qty"]) * tq
    return {**cfg, "usd_capital": usd, "krw_capital": usd / USD_WEIGHT * KRW_WEIGHT * fx, "fx": fx,
            "basis": f"달러 ${float(cfg['usd_cash_at_start']):,.2f} + TQQQ {cfg['legacy_tqqq_qty']}주 × ${tq:.2f}({tq_d} 종가), 환율 {fx:,.1f}"}


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
    others = {c["ledger"] for c in DEFENSE_CHOICES.values()} - {DEFENSE["ledger"]}
    STRAY_DEFENSE.clear()
    for t in trades:
        if t["ticker"] in others and t["action"] in ("buy", "sell"):
            q = float(t["quantity"])
            STRAY_DEFENSE[t["ticker"]] = STRAY_DEFENSE.get(t["ticker"], 0.0) + (q if t["action"] == "buy" else -q)
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


def rsi_wilder(values: list[float], n: int = RSI_LEN) -> float:
    """마지막 값의 와일더 RSI(백테스트·규칙서와 같은 식)."""
    a = np.asarray(values, dtype=float)
    d = np.diff(a)
    up = np.clip(d, 0, None); dn = np.clip(-d, 0, None)
    au = ad = None
    for u, w in zip(up, dn):
        au = u if au is None else au + (u - au) / n
        ad = w if ad is None else ad + (w - ad) / n
    if not ad:
        return 100.0
    return float(100 - 100 / (1 + au / ad))


def closes_with_today(closes: list[tuple[date, float]], price_now: float, today_us: date) -> list[float]:
    vals = [c for d, c in closes if d < today_us]
    return vals + [price_now]


# ── 공포 점수 S (FA곡선) ────────────────────────────────────────────────────

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


def brk_ladder_plan(b: Position, hist: list[tuple[date, float]], price_now: float, start: date, today: date) -> dict:
    """사다리를 시작일부터 오늘까지 일봉으로 다시 돌려(상태 없음) '지금까지 샀어야 할 금액'을 구하고,
    실제로 산 금액과의 차이만큼 산다. 빠진 날이 있어도 다음 실행에서 따라잡는다."""
    u = LAD_UNIT * b.capital
    days = [(d, c) for d, c in hist if start <= d < today] + [(today, price_now)]
    full = [c for d, c in hist if d < today] + [price_now]
    first_i = len(full) - len(days)
    p1 = None; done = set(); last = None; cum = 0.0; note = ""; fired_today = False
    for i, (d, c) in enumerate(days):
        units = 0.0
        if i >= LAD_DEADLINE:
            units = float("inf"); why = "1년 기한 — 남은 돈 전부"
        elif p1 is None:
            r = rsi_wilder(full[:first_i + i + 1])
            if r <= LAD_RSI:
                units = 1.0; p1 = c; why = f"RSI {r:.0f} ≤ {LAD_RSI} 첫 매수(기준가 ${c:.2f})"
        else:
            hits = [m for lv, m in LAD_DOWN if lv not in done and c <= p1 * lv]
            for lv, m in LAD_DOWN:
                if lv not in done and c <= p1 * lv:
                    done.add(lv)
            ups = [lv for lv in LAD_UP if ("u", lv) not in done and c >= p1 * lv]
            for lv in ups:
                done.add(("u", lv))
            units = sum(hits) + len(ups)
            why = f"기준가 ${p1:.2f} 대비 {c / p1 - 1:+.0%}"
            if units == 0 and last is not None and i - last >= LAD_GAP:
                units = 1.0; why = f"한 달({LAD_GAP}거래일) 동안 매수 없음"
        if units:
            cum = b.capital if units == float("inf") else cum + units * u
            last = i
            if d == today:
                fired_today = True; note = f"사다리: {why}"
    want = min(b.capital, cum)
    spent = sum(q * px for _, q, px in b.buys)
    short = min(b.cash, want - spent)
    if short >= price_now:
        if not fired_today:
            note = f"사다리: 지금까지 사야 했던 ${want:,.0f} 중 ${spent:,.0f}만 삼 — 모자란 만큼 따라잡기"
        return {"order": short, "note": note, "p1": p1}
    if p1 is None and not note:
        note = f"사다리: {DEFENSE['name']} RSI가 {LAD_RSI} 이하로 내려오기를 기다리는 중(시작 후 {len(days) - 1}거래일, 1년 지나면 전부 매수)"
    return {"order": 0.0, "note": note, "p1": p1}


def plan_us(pos: dict[str, Position], fg_now: float, fear: dict, cape: dict,
            prices: dict[str, float], today: date, brk_hist: list | None = None, start: date | None = None) -> list[Order]:
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
    # FA곡선 (TQQQ·QLD·QQQ) + RSI 가속
    target = fear["target"]
    if fear.get("rsi") is not None and fear["rsi"] <= RSI_ACCEL_TH:
        target = min(1.0, target + RSI_ACCEL_ADD)
    fear = {**fear, "target": target}
    for k in ("TQQQ", "QLD", "QQQ"):
        p = pos[k]; px = prices[k]; inv = p.qty * px; tot = p.cash + inv
        if tot <= 0 or any(o.sleeve == k and o.side == "sell" for o in orders):
            continue
        tgt = fear["target"]; reserve_note = ""
        if k == "QQQ" and fg_now > QQQ_RESERVE_FG and tgt > 1 - QQQ_RESERVE:
            tgt = 1 - QQQ_RESERVE
            reserve_note = f", {QQQ_RESERVE:.0%}는 F&G {QQQ_RESERVE_FG} 이하용으로 남김"
        share = inv / tot
        if share >= tgt - CURVE_GAP:
            continue
        if cape["hot"]:
            orders.append(Order(k, "hold", 0, None, f"투자 비중 {share:.0%} < 목표 {tgt:.0%}이지만 CAPE 막기(CAPE {cape['cape']} > 기준선 {cape['line'] and round(cape['line'], 1)})"))
            continue
        amt = min(p.cash, tgt * tot - inv)
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"투자 비중 {share:.0%} → 목표 {tgt:.0%} (S {fear['S']:.2f}" + (f", RSI {fear['rsi']:.0f} 가속" if fear.get("rsi") is not None and fear["rsi"] <= RSI_ACCEL_TH else "") + reserve_note + ")"))
    # 버크셔(또는 SCHD): 사다리식 (규칙서 10-15판)
    stray = {t: q for t, q in STRAY_DEFENSE.items() if q > 1e-9}
    if stray:
        left = ", ".join(f"{t} {q:g}주" for t, q in stray.items())
        orders.append(Order("BRK", "hold", 0, None, f"칸 종목을 {DEFENSE['name']}(으)로 바꿨는데 예전 {left}가 남아 있음 — 먼저 정리해야 함"))
        orders.append(Order("SOXL", "hold", 0, None, "무한매수법은 LOC 시험 뒤 연결 예정(지금은 현금 보유)"))
        return orders
    lad = brk_ladder_plan(pos["BRK"], brk_hist, prices["BRK"], start, today)
    if lad["order"] > 0:
        orders.append(Order("BRK", "buy", lad["order"], None, lad["note"]))
    elif lad["note"]:
        orders.append(Order("BRK", "hold", 0, None, lad["note"]))
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


# ── 상황 모으기 ──────────────────────────────────────────────────────────────

def gather(cfg: dict, trades: list[dict]) -> dict:
    import kiwoom_client as K
    import today_signal
    from zoneinfo import ZoneInfo
    today_kst = datetime.now(KST).date()
    today_us = datetime.now(ZoneInfo("America/New_York")).date()
    pos = build_ledger(cfg, trades)
    prices = {}
    for s_ in SLEEVES:
        if s_.currency == "USD":
            prices[s_.key] = K.abs_price(K.get_overseas_quote(s_.code, exchange=s_.exchange, mode="demo")["cur_prc"])
        else:
            prices[s_.key] = K.abs_price(K.get_domestic_quote(s_.code, mode="demo")["cur_prc"])
    info = today_signal.get_cnn_score()
    hist = crisis_plan.fetch_cnn_history()
    tq_closes = fetch_daily_closes("TQQQ")
    fear = fear_score(info["score"], hist, prices["TQQQ"], tq_closes)
    fear["rsi"] = rsi_wilder(closes_with_today(tq_closes, prices["TQQQ"], today_us))
    return {"pos": pos, "prices": prices, "fg": info["score"], "fear": fear, "cape": cape_block(),
            "brk_hist": fetch_daily_closes(DEFENSE["yahoo"], "2y"), "today_kst": today_kst, "today_us": today_us}


def print_status(tag: str, cfg: dict, ctx: dict) -> None:
    f = ctx["fear"]
    c = ctx["cape"]
    acc = f" → RSI 가속 +{RSI_ACCEL_ADD:.0%}p" if f["rsi"] <= RSI_ACCEL_TH else ""
    cape_txt = (f"CAPE {c['cape']} / 기준선 {c['line']:.1f} → {'막힘' if c['hot'] else '열림'}"
                if c["cape"] else c["error"])
    print(f"{tag} F&G {ctx['fg']:.1f} · S {f['S']:.2f} → 목표 투자 비중 {f['target']:.0%} · "
          f"TQQQ RSI {f['rsi']:.0f}{acc} · {cape_txt}")
    for line in summary_lines(ctx["pos"], ctx["prices"], cfg["fx"]):
        print(f"{tag} {line}")


# ── 실제 주문 ───────────────────────────────────────────────────────────────

def _say(msg: str) -> None:
    print(f"-> v2 {msg}")   # 텔레그램 요약이 "-> " 줄을 집어 간다


def _overseas_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> None:
    import kiwoom_client as K
    import fill_price
    from cooldown import log_trade
    from auto_trade_loop_kiwoom import KiwoomOverseasChaseOrder, _overseas_holding
    book = K.get_overseas_orderbook(sl.code, exchange=sl.exchange, mode="demo")
    ref = abs(float(book.get("sel_1bid", "0") or "0"))
    if ref <= 0:
        _say(f"{sl.key} 매수 못 함 — 호가 0(미국 정규장 아님)")
        return
    qty = int(amount * (1 - OVERSEAS_FEE_MARGIN) // ref)
    if qty < 1:
        _say(f"{sl.key} 매수 건너뜀 — ${amount:,.0f}로는 1주(${ref:,.2f})도 못 삼")
        return
    if dry_run:
        _say(f"{sl.key} 매수 {qty}주 @ 약 ${ref:,.2f} (dry-run, 주문 안 냄) — {note}")
        return
    before = _overseas_holding(sl.code, sl.exchange)
    ch = KiwoomOverseasChaseOrder("buy", qty, max_reprices=15, max_seconds=240.0, poll_interval=3.0,
                                  qty_before=before["qty"] if before else 0, ticker=sl.code, exchange=sl.exchange)
    ch.run()
    if ch.filled_qty <= 0:
        _say(f"{sl.key} 매수 주문이 체결되지 않음")
        return
    after = _overseas_holding(sl.code, sl.exchange)
    fill = fill_price.safe(fill_price.from_holding_delta, before, after, ch.filled_qty) or ref
    log_trade(ticker=sl.ledger, action="buy", quantity=ch.filled_qty, price=round(fill, 4), fg_score=fg,
              memo=f"v2 자동실행 — {note}", account=V2_ACCOUNT)
    _say(f"{sl.key} 매수 {ch.filled_qty}/{qty}주 @ ${fill:,.2f} — {note}")
    print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {ch.filled_qty}주")


def _overseas_sell(sl: Sleeve, qty: int, fg: float, dry_run: bool, note: str, account: str = V2_ACCOUNT) -> None:
    import kiwoom_client as K
    from cooldown import log_trade
    from auto_trade_loop_kiwoom import KiwoomOverseasChaseOrder, _overseas_holding
    book = K.get_overseas_orderbook(sl.code, exchange=sl.exchange, mode="demo")
    ref = abs(float(book.get("buy_1bid", "0") or "0"))
    if ref <= 0:
        _say(f"{sl.key} 매도 못 함 — 호가 0(미국 정규장 아님)")
        return
    if dry_run:
        _say(f"{sl.key} 매도 {qty}주 @ 약 ${ref:,.2f} (dry-run, 주문 안 냄) — {note}")
        return
    before = _overseas_holding(sl.code, sl.exchange)
    ch = KiwoomOverseasChaseOrder("sell", qty, max_reprices=15, max_seconds=240.0, poll_interval=3.0,
                                  qty_before=before["qty"] if before else 0, ticker=sl.code, exchange=sl.exchange)
    ch.run()
    if ch.filled_qty <= 0:
        _say(f"{sl.key} 매도 주문이 체결되지 않음")
        return
    px = ch.order["price"] if ch.order else ref
    log_trade(ticker=sl.ledger, action="sell", quantity=ch.filled_qty, price=round(px, 4), fg_score=fg,
              memo=f"v2 자동실행 — {note} (가격은 마지막 주문가)", account=account)
    _say(f"{sl.key} 매도 {ch.filled_qty}/{qty}주 @ 약 ${px:,.2f} — {note}")
    print(f"매매기록 저장 완료: {account} sell {sl.ledger} {ch.filled_qty}주")


def _domestic_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> None:
    import kiwoom_client as K
    import fill_price
    from cooldown import log_trade
    from auto_trade_loop_kiwoom import KiwoomChaseOrder
    book = K.get_domestic_orderbook(sl.code, mode="demo")
    ref = int(K.abs_price(book.get("sel_fpr_bid", "0") or "0"))
    if ref <= 0:
        _say(f"{sl.key} 매수 못 함 — 호가 0(국내 정규장 아님)")
        return
    qty = int(amount // ref)
    if qty < 1:
        _say(f"{sl.key} 매수 건너뜀 — {amount:,.0f}원으로는 1주({ref:,}원)도 못 삼")
        return
    if dry_run:
        _say(f"{sl.key} 매수 {qty}주 @ 약 {ref:,}원 (dry-run, 주문 안 냄) — {note}")
        return
    ch = KiwoomChaseOrder(sl.code, "buy", qty, max_reprices=60, max_seconds=600.0, poll_interval=3.0)
    ch.run()
    if ch.filled_qty <= 0:
        _say(f"{sl.key} 매수 주문이 체결되지 않음")
        return
    fill = fill_price.safe(fill_price.kiwoom_domestic, sl.code, getattr(ch, "order_nos", [])) or ref
    log_trade(ticker=sl.ledger, action="buy", quantity=ch.filled_qty, price=round(fill, 4), fg_score=fg,
              memo=f"v2 자동실행 — {note}", account=V2_ACCOUNT)
    _say(f"{sl.key} 매수 {ch.filled_qty}/{qty}주 @ {fill:,.0f}원 — {note}")
    print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {ch.filled_qty}주")


def _sell_legacy_tqqq(cfg: dict, pos: dict[str, Position], fg: float, dry_run: bool) -> None:
    """계좌의 TQQQ 중 v2 장부에 없는 몫(옛 규칙이 산 것)을 판다. 상태 없이 '실제 보유 − v2 장부'로 센다."""
    from auto_trade_loop_kiwoom import _overseas_holding, KIWOOM_ACCOUNT_LABEL
    h = _overseas_holding("TQQQ", "ND")
    legacy = (h["qty"] if h else 0) - int(round(pos["TQQQ"].qty))
    if legacy <= 0:
        return
    if legacy != int(cfg.get("legacy_tqqq_qty") or 0):
        _say(f"주의: 옛 TQQQ가 {legacy}주로 설정값({cfg.get('legacy_tqqq_qty')}주)과 다름 — 재원 계산을 확인하세요")
    sl = next(x for x in SLEEVES if x.key == "TQQQ")
    _overseas_sell(sl, legacy, fg, dry_run, f"시작일 정리: 옛 규칙 TQQQ {legacy}주 매도(재원으로 씀)",
                   account=KIWOOM_ACCOUNT_LABEL)


def run_live(dry_run: bool = False, force_market: str | None = None) -> None:
    """auto_trade_loop_kiwoom.py가 시작일부터 부른다. 실행 창에 따라 미국/국내 할 일을 계산하고 주문한다."""
    from auto_trade_loop_kiwoom import _within_tqqq_close_window, _within_domestic_close_window
    market = force_market or ("us" if _within_tqqq_close_window()
                              else "kr" if _within_domestic_close_window() else None)
    cfg = resolve_capital(load_config())
    print(f"=== [키움 v2] FA 새 규칙 (시작 {cfg['start_date']}) ===")
    print(f"v2 재원 ${cfg['usd_capital']:,.0f} + 원화 {cfg['krw_capital']:,.0f}원 ({cfg.get('basis', '설정값')})")
    if market is None:
        print("최종 신호: v2 실행 창 밖 — 건너뜁니다")
        return
    trades = fetch_v2_trades()
    ctx = gather(cfg, trades)
    print_status("v2", cfg, ctx)
    print(f"최종 신호: v2 {'미국장' if market == 'us' else '국내장'} 마감 실행" + (" (dry-run)" if dry_run else ""))
    sl = {x.key: x for x in SLEEVES}
    if market == "us":
        _sell_legacy_tqqq(cfg, ctx["pos"], ctx["fg"], dry_run)
        orders = plan_us(ctx["pos"], ctx["fg"], ctx["fear"], ctx["cape"], ctx["prices"], ctx["today_us"],
                         brk_hist=ctx["brk_hist"], start=date.fromisoformat(cfg["start_date"]))
    else:
        orders = plan_kr(ctx["pos"], ctx["fg"], ctx["today_kst"])
    if not any(o.side in ("buy", "sell") for o in orders):
        _say("오늘 살 것·팔 것 없음")
    for o in orders:
        if o.side == "hold":
            _say(f"{o.sleeve} 대기 — {o.note}")
        elif o.side == "sell":
            _overseas_sell(sl[o.sleeve], int(o.qty), ctx["fg"], dry_run, o.note)
        elif sl[o.sleeve].currency == "USD":
            _overseas_buy(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note)
        else:
            _domestic_buy(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note)


CONFIG_OVERRIDE: dict = {}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="FA v2 — 오늘 할 일 미리보기(기본, 주문 없음) 또는 실행")
    ap.add_argument("--market", choices=["us", "kr"], help="실행 창과 상관없이 이 시장 기준으로 계산")
    ap.add_argument("--live", action="store_true", help="실제 모의 주문을 낸다(기본은 주문 없이 보여 주기만)")
    ap.add_argument("--start", help="미리보기용 시작일 바꾸기(예: 오늘 날짜로 시험)")
    args = ap.parse_args()
    if args.start:
        CONFIG_OVERRIDE.update({**_load_config_file(), "start_date": args.start})
    run_live(dry_run=not args.live, force_market=args.market)


if __name__ == "__main__":
    main()
