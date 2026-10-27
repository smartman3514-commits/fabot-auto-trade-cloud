"""FA v2 (규칙서 2026-10-15판) — 키움 모의계좌에서 돌리는 새 버전의 장부·규칙·주문.

2026-10-15 추가: FA곡선 RSI 가속(TQQQ 일봉 RSI(14) ≤ 30이면 목표 비중 +40%p), 버크셔 사다리식 매수,
실제 주문 실행(run_live). 2026-10-19 인선 결정으로 SOXL 칸은 뺐다(배당 칸 30으로). 10-27 배당 칸을 SCHD 15 + 버크셔B 15로 나눴다.

인선 결정(2026-10-13): KIS는 지금 버전 그대로, 키움은 이 새 버전으로 실제 주문을 낸다.
키움 계좌에는 옛 보유분(TIGER 배당커버드콜 약 6,700주, 원화 약 3.5억)이 섞여 있으므로,
새 버전의 칸별 현금·보유는 **trades 표에서 account='키움 v2'로 남긴 기록만으로** 매번
처음부터 다시 계산한다(상태 파일 없음 — crisis_plan과 같은 방식).

시작일(v2_config.json start_date)부터 auto_trade_loop_kiwoom.py가 옛 규칙 대신 run_live()를 부른다.

2026-10-26 인선 결정: 키움 모의투자 해외 계좌가 새로 열리지 않아(10-24 만료 뒤 국내 계좌 81372441만 나옴)
v2를 **한투 새 모의계좌(50220595, 원화 5억 + 달러 $100,000)**로 옮겨 새로 시작한다. v2_config.json의
"broker": "kis"면 한투 주문 도구(overseas_order_executor·live_order_executor)로 주문하고
auto_trade_loop.py가 부른다. 장부 이름은 "account"(한투 v2). 옛 '키움 v2' 기록은 그대로 둔다.

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

CONFIG_PATH = Path(__file__).resolve().parent / "v2_config.json"
KST = timezone(timedelta(hours=9))


def _cfg_value(key: str, default):
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(key) or default
    except (OSError, ValueError):
        return default


BROKER = str(_cfg_value("broker", "kiwoom")).lower()           # kis | kiwoom
V2_ACCOUNT = _cfg_value("account", "키움 v2")
# 한투 해외 주문·잔고는 4자리 거래소 코드를 쓴다. NYSE Arca 상장 ETF(QLD·SCHD)는 한투에서 AMEX로 다룬다.
# 버크셔B는 한투에서 "BRK/B"(NYSE)로만 조회된다(BRK.B·BRKB는 안 됨, 2026-10-27 실측).
KIS_EXCG = {"TQQQ": "NASD", "QQQM": "NASD", "QLD": "AMEX", "SCHD": "AMEX", "BRK/B": "NYSE"}
KIS_EXCG.update(_cfg_value("kis_exchange", {}))                 # 실측으로 다르면 설정에서 바꾼다


@dataclass(frozen=True)
class Sleeve:
    key: str          # 칸 이름
    code: str         # 키움 주문 코드
    exchange: str     # 해외: ND/NY, 국내: KRX
    ledger: str       # trades.ticker 에 남기는 이름
    weight: float     # 새 버전 전체 대비 비중
    currency: str     # USD / KRW
    rule: str         # curve / curve_sell / infinite / dca / peace


# 배당 칸 30% = SCHD 15% + 버크셔B 15% (2026-10-27 인선: "반반으로 해주세요").
# 백테스트(원화, 1998~, CAPE 1.6배): 버크셔만 15.1%/−40.6%, SCHD만 14.4%/−43.2%, 반반 14.8%/−41.5%.
# 2011-11~(실제 SCHD 자료)에는 셋 차이가 연 0.4%p 안 — 고른 이유는 수익보다 현금흐름(SCHD 배당)과 하락장 방어(버크셔)를 나눠 갖기 위해서.
# 두 칸은 같은 사다리 규칙을 각자 자기 가격(RSI)으로 돈다.
LADDER = {"BRK": {"yahoo": "BRK-B", "name": "버크셔B"}, "SCHD": {"yahoo": "SCHD", "name": "SCHD"}}

SLEEVES = [
    Sleeve("TQQQ", "TQQQ", "ND", "TQQQ", .15, "USD", "curve_sell"),
    Sleeve("QLD", "QLD", "NY", "QLD", .15, "USD", "curve"),
    Sleeve("QQQ", "QQQM", "ND", "QQQM", .30, "USD", "curve"),   # 10-19 인선: QQQ → QQQM(같은 나스닥100, 1주 약 $290이라 작은 금액도 규칙에 가깝게)
    Sleeve("SCHD", "SCHD", "NY", "SCHD", .15, "USD", "dca"),      # 10-19 인선: SOXL을 빼고 배당 칸 30 → 10-27 SCHD·버크셔 반반
    Sleeve("BRK", "BRK/B" if BROKER == "kis" else "BRKb", "NY", "BRK.B", .15, "USD", "dca"),
    Sleeve("CC_DIV", "472150", "KRX", "TIGER 배당커버드콜액티브(472150)", .05, "KRW", "peace"),
    Sleeve("CC_NAS", "486290", "KRX", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", .05, "KRW", "peace"),
]
USD_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "USD")   # 0.90
KRW_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "KRW")   # 0.10

# 규칙 숫자 (규칙서 10-13 오후판)
CURVE_BASE, CURVE_POW, CURVE_GAP = .20, 2.0, .05      # 목표 = 20% + 80%·S², 5%p 이상 모자라면 채움
S_FG_W, S_DD_W, S_RUN_W = .4, .4, .2
S_DD_FULL, S_RUN_FULL, FEAR_RUN_FG = .60, 60, 35
CAPE_K = 1.6            # 2026-10-26 인선: 1.7 → 1.6 (v2 엔진 1998~ MDD −46.2% → −40.6%, 연수익 15.3% → 15.0%)
SELL_HALF, SELL_ALL, SELL_GAP_DAYS = 72, 77, 4
DCA_WEEKS = 26          # (옛 규칙 — 10-15부터 버크셔는 사다리식)
RSI_LEN, RSI_ACCEL_TH, RSI_ACCEL_ADD = 14, 30, .40      # FA곡선 RSI 가속 (TQQQ 일봉)
LAD_RSI, LAD_UNIT, LAD_GAP, LAD_DEADLINE = 40, .10, 21, 252
LAD_DOWN = [(.90, 1.5), (.85, 2.0), (.80, 2.5), (.75, 3.0), (.70, 3.5), (.65, 4.0), (.60, 4.5), (.55, 5.0), (.50, 5.5)]
LAD_UP = [1 + .05 * k for k in range(2, 11)]           # +10%, +15%, … +50% → 1회분씩
OVERSEAS_FEE_MARGIN = .011
PEACE_LO, PEACE_HI, PEACE_FRAC, PEACE_GAP_DAYS = 0, 65, .10, 3   # 10-19 인선: 35~65 → 65 이하(공포 때도 산다, 백테스트 결과 같음)


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
    """최근 CAPE가 '1871년~직전 달까지 log(CAPE) 직선 추세' × CAPE_K(1.6)를 넘으면 막는다.
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


def brk_ladder_plan(b: Position, hist: list[tuple[date, float]], price_now: float, start: date, today: date,
                    name: str = "", money=lambda v: f"${v:,.2f}") -> dict:
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
                units = 1.0; p1 = c; why = f"RSI {r:.0f} ≤ {LAD_RSI} 첫 매수(기준가 {money(c)})"
        else:
            hits = [m for lv, m in LAD_DOWN if lv not in done and c <= p1 * lv]
            for lv, m in LAD_DOWN:
                if lv not in done and c <= p1 * lv:
                    done.add(lv)
            ups = [lv for lv in LAD_UP if ("u", lv) not in done and c >= p1 * lv]
            for lv in ups:
                done.add(("u", lv))
            units = sum(hits) + len(ups)
            why = f"기준가 {money(p1)} 대비 {c / p1 - 1:+.0%}"
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
            note = f"사다리: 지금까지 사야 했던 {money(want)} 중 {money(spent)}만 삼 — 모자란 만큼 따라잡기"
        return {"order": short, "note": note, "p1": p1}
    if p1 is None and not note:
        note = f"사다리: {name} RSI가 {LAD_RSI} 이하로 내려오기를 기다리는 중(시작 후 {len(days) - 1}거래일, 1년 지나면 전부 매수)"
    return {"order": 0.0, "note": note, "p1": p1}


def plan_us(pos: dict[str, Position], fg_now: float, fear: dict, cape: dict,
            prices: dict[str, float], today: date, brk_hist: dict | None = None, start: date | None = None) -> list[Order]:
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
        tgt = fear["target"]   # 10-21 인선: QQQM 30% 예비(F&G 15 이하용) 뺌 — 백테스트 차이 거의 없음, 규칙 단순화
        share = inv / tot
        if share >= tgt - CURVE_GAP:
            continue
        if cape["hot"]:
            orders.append(Order(k, "hold", 0, None, f"투자 비중 {share:.0%} < 목표 {tgt:.0%}이지만 CAPE 막기(CAPE {cape['cape']} > 기준선 {cape['line'] and round(cape['line'], 1)})"))
            continue
        amt = min(p.cash, tgt * tot - inv)
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"투자 비중 {share:.0%} → 목표 {tgt:.0%} (S {fear['S']:.2f}" + (f", RSI {fear['rsi']:.0f} 가속" if fear.get("rsi") is not None and fear["rsi"] <= RSI_ACCEL_TH else "") + ")"))
    # 배당 칸 SCHD·버크셔B: 각자 사다리식 (규칙서 10-15판)
    for k, info in LADDER.items():
        lad = brk_ladder_plan(pos[k], brk_hist[k], prices[k], start, today, name=info["name"])
        if lad["order"] > 0:
            orders.append(Order(k, "buy", lad["order"], None, lad["note"]))
        elif lad["note"]:
            orders.append(Order(k, "hold", 0, None, lad["note"]))
    return orders


def plan_kr(pos: dict[str, Position], fg_now: float, today: date) -> list[Order]:
    orders = []
    for k in ("CC_DIV", "CC_NAS"):
        p = pos[k]
        if not (PEACE_LO <= fg_now <= PEACE_HI):
            continue
        last = max((d for d, _, _ in p.buys), default=None)
        if last and cooldown._trading_days_between(last, today) < PEACE_GAP_DAYS:
            orders.append(Order(k, "hold", 0, None, f"F&G {PEACE_HI} 이하 매수 조건이지만 {last} 매수 뒤 {PEACE_GAP_DAYS}거래일 쉬는 중"))
            continue
        amt = p.cash * PEACE_FRAC
        if amt > 0:
            orders.append(Order(k, "buy", amt, None, f"F&G {fg_now:.0f} ≤ {PEACE_HI} — 남은 돈의 {PEACE_FRAC:.0%}"))
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

def kis_us_price(code: str) -> float:
    """한투 해외 현재가(장이 닫혀 있으면 마지막 체결가). 시세 API는 3자리 거래소 코드(NAS/NYS/AMS)를 쓴다."""
    import overseas_order_executor as O
    excd = O._QUOTE_EXCD_BY_ORDER_EXCG[KIS_EXCG[code]]
    O._throttle()
    r = O._get_with_retry(f"{O.BASE_URL}/uapi/overseas-price/v1/quotations/price",
                          headers={**O._headers("HHDFS00000300"), "tr_cont": ""}, params={"AUTH": "", "EXCD": excd, "SYMB": code})
    O._raise_verbose(r)
    d = r.json()
    if d.get("rt_cd") != "0" or not float(d["output"].get("last") or 0):
        raise RuntimeError(f"한투 {code}({excd}) 현재가 조회 실패: {d.get('msg1') or d.get('output')}")
    return float(d["output"]["last"])


def kis_kr_price(code: str) -> float:
    """한투 국내 현재가(stck_prpr)."""
    import live_order_executor as L
    L._throttle()
    r = L._get_with_retry(f"{L.BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                          headers={**L._headers("FHKST01010100"), "tr_cont": ""},
                          params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    L._raise_verbose(r)
    d = r.json()
    if d.get("rt_cd") != "0":
        raise RuntimeError(f"한투 {code} 현재가 조회 실패: {d.get('msg1')}")
    return float(d["output"]["stck_prpr"])


def gather(cfg: dict, trades: list[dict]) -> dict:
    import today_signal
    from zoneinfo import ZoneInfo
    today_kst = datetime.now(KST).date()
    today_us = datetime.now(ZoneInfo("America/New_York")).date()
    pos = build_ledger(cfg, trades)
    prices = {}
    for s_ in SLEEVES:
        if BROKER == "kis":
            prices[s_.key] = kis_us_price(s_.code) if s_.currency == "USD" else kis_kr_price(s_.code)
            continue
        import kiwoom_client as K
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
            "brk_hist": {k: fetch_daily_closes(v["yahoo"], "2y") for k, v in LADDER.items()}, "today_kst": today_kst, "today_us": today_us}


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


# ── 실제 주문 (한투) ────────────────────────────────────────────────────────

def wait_kr_close_auction() -> None:
    """국내장 15:20~15:30은 장 마감 동시호가 — 그 사이 걸린 주문은 15:30에 한꺼번에 체결되고, 취소도 안 된다
    (10-23 키움 486290 73주가 '미체결'로 끝났다가 15:30에 실제 체결된 사고). 그 창 안이면 15:31까지 기다린다."""
    import time
    now = datetime.now(KST)
    until = now.replace(hour=15, minute=31, second=0, microsecond=0)
    if now.replace(hour=15, minute=19, second=0, microsecond=0) <= now < until:
        print(f"  장 마감 동시호가 체결을 기다립니다({until:%H:%M} KST까지).")
        time.sleep((until - now).total_seconds())


def _kis_overseas_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> None:
    import fill_price
    import overseas_order_executor as O
    from cooldown import log_trade
    excg = KIS_EXCG[sl.code]
    ref = float(O._get_asking_price(sl.code, excg).get("pask1") or 0)
    if ref <= 0:
        _say(f"{sl.key} 매수 못 함 — 호가 0(미국 정규장 아님)")
        return
    qty = int(amount * (1 - OVERSEAS_FEE_MARGIN) // ref)
    cap = O.get_overseas_order_capacity(sl.code, excg, ref)
    if cap["max_qty"] is not None and qty > cap["max_qty"]:
        print(f"  증권사 한도({cap['max_qty']}주)를 넘어 {qty}주 → {cap['max_qty']}주로 줄입니다.")
        qty = cap["max_qty"]
    if qty < 1:
        _say(f"{sl.key} 매수 건너뜀 — ${amount:,.0f}로는 1주(${ref:,.2f})도 못 삼")
        return
    if dry_run:
        _say(f"{sl.key} 매수 {qty}주 @ 약 ${ref:,.2f} (dry-run, 주문 안 냄) — {note}")
        return
    before = O.get_overseas_holding(sl.code, excg)
    ch = O.OverseasChaseOrder(sl.code, excg, "buy", qty, max_reprices=15, max_seconds=240.0, poll_interval=3.0)
    ch.run()
    if ch.filled_qty <= 0:
        _say(f"{sl.key} 매수 주문이 체결되지 않음")
        return
    after = O.get_overseas_holding(sl.code, excg)
    # 한투 모의 해외 평균단가는 당일 갱신이 안 될 때가 있다(10-03) → 체결내역 체결가를 먼저 쓴다
    fill = (fill_price.safe(fill_price.kis_overseas, sl.code, excg, getattr(ch, "order_nos", []))
            or fill_price.safe(fill_price.from_holding_delta, before, after, ch.filled_qty) or ref)
    log_trade(ticker=sl.ledger, action="buy", quantity=ch.filled_qty, price=round(fill, 4), fg_score=fg,
              memo=f"v2 자동실행(한투) — {note}", account=V2_ACCOUNT)
    _say(f"{sl.key} 매수 {ch.filled_qty}/{qty}주 @ ${fill:,.2f} — {note}")
    print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {ch.filled_qty}주")


def _kis_overseas_sell(sl: Sleeve, qty: int, fg: float, dry_run: bool, note: str, account: str = V2_ACCOUNT) -> None:
    import fill_price
    import overseas_order_executor as O
    from cooldown import log_trade
    excg = KIS_EXCG[sl.code]
    ref = float(O._get_asking_price(sl.code, excg).get("pbid1") or 0)
    if ref <= 0:
        _say(f"{sl.key} 매도 못 함 — 호가 0(미국 정규장 아님)")
        return
    held = O.get_overseas_holding(sl.code, excg)
    if not held or held["qty"] < qty:
        _say(f"{sl.key} 매도 {qty}주 중 계좌 보유가 {held['qty'] if held else 0}주뿐 — 보유만큼만 팝니다")
        qty = held["qty"] if held else 0
        if qty <= 0:
            return
    if dry_run:
        _say(f"{sl.key} 매도 {qty}주 @ 약 ${ref:,.2f} (dry-run, 주문 안 냄) — {note}")
        return
    ch = O.OverseasChaseOrder(sl.code, excg, "sell", qty, max_reprices=15, max_seconds=240.0, poll_interval=3.0)
    ch.run()
    if ch.filled_qty <= 0:
        _say(f"{sl.key} 매도 주문이 체결되지 않음")
        return
    px = fill_price.safe(fill_price.kis_overseas, sl.code, excg, getattr(ch, "order_nos", [])) or (ch.order["price"] if ch.order else ref)
    log_trade(ticker=sl.ledger, action="sell", quantity=ch.filled_qty, price=round(px, 4), fg_score=fg,
              memo=f"v2 자동실행(한투) — {note}", account=account)
    _say(f"{sl.key} 매도 {ch.filled_qty}/{qty}주 @ ${px:,.2f} — {note}")
    print(f"매매기록 저장 완료: {account} sell {sl.ledger} {ch.filled_qty}주")


def _kis_domestic_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> None:
    import asyncio
    import fill_price
    import live_order_executor as L
    from cooldown import log_trade
    ref = int(float(L._get_asking_price(sl.code).get("askp1") or 0))
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
    before = L.get_holding(sl.code)
    ch = L.ChaseOrder(sl.code, "buy", qty, max_reprices=60, max_seconds=630.0)
    asyncio.run(ch.run())
    filled = ch.filled_qty
    if filled < qty:   # 동시호가에 걸린 주문은 15:30에 체결된다 → 보유 수량 차이로 다시 센다
        wait_kr_close_auction()
        after = L.get_holding(sl.code)
        delta = (after["qty"] if after else 0) - (before["qty"] if before else 0)
        if delta > filled:
            print(f"  보유 수량으로 다시 셈: {filled}주 → {min(delta, qty)}주")
            filled = min(delta, qty)
    if filled <= 0:
        _say(f"{sl.key} 매수 주문이 체결되지 않음")
        return
    fill = fill_price.safe(fill_price.kis_domestic, sl.code, getattr(ch, "order_nos", [])) or ref
    log_trade(ticker=sl.ledger, action="buy", quantity=filled, price=round(fill, 4), fg_score=fg,
              memo=f"v2 자동실행(한투) — {note}", account=V2_ACCOUNT)
    _say(f"{sl.key} 매수 {filled}/{qty}주 @ {fill:,.0f}원 — {note}")
    print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {filled}주")


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
    """시작일부터 auto_trade_loop.py(한투) 또는 auto_trade_loop_kiwoom.py(키움)가 부른다(v2_config.json broker).
    실행 창에 따라 미국/국내 할 일을 계산하고 주문한다."""
    if BROKER == "kis":
        from auto_trade_loop import _within_tqqq_close_window, _within_domestic_close_window
    else:
        from auto_trade_loop_kiwoom import _within_tqqq_close_window, _within_domestic_close_window
    market = force_market or ("us" if _within_tqqq_close_window()
                              else "kr" if _within_domestic_close_window() else None)
    cfg = resolve_capital(load_config())
    print(f"=== [{V2_ACCOUNT}] FA 새 규칙 (시작 {cfg['start_date']}) ===")
    print(f"v2 재원 ${cfg['usd_capital']:,.0f} + 원화 {cfg['krw_capital']:,.0f}원 ({cfg.get('basis', '설정값')})")
    if market is None:
        print("최종 신호: v2 실행 창 밖 — 건너뜁니다")
        return
    trades = fetch_v2_trades()
    ctx = gather(cfg, trades)
    print_status("v2", cfg, ctx)
    print(f"최종 신호: v2 {'미국장' if market == 'us' else '국내장'} 마감 실행" + (" (dry-run)" if dry_run else ""))
    sl = {x.key: x for x in SLEEVES}
    buy_us, sell_us, buy_kr = ((_kis_overseas_buy, _kis_overseas_sell, _kis_domestic_buy) if BROKER == "kis"
                               else (_overseas_buy, _overseas_sell, _domestic_buy))
    if market == "us":
        if BROKER != "kis":   # 한투 새 계좌에는 옛 규칙 TQQQ가 없다
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
            sell_us(sl[o.sleeve], int(o.qty), ctx["fg"], dry_run, o.note)
        elif sl[o.sleeve].currency == "USD":
            buy_us(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note)
        else:
            buy_kr(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note)


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
