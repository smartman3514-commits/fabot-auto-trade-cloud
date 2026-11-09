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
import time
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

# 2026-11-09 인선: FA여정 공격형 v1 비중 그대로 + 금 칸(ACE KRX금현물). 바꾸기 전: TQQQ .15 · QLD .15 · QQQM .30 ·
# SCHD .15 · 버크셔 .15 · 커버드콜 .05+.05. 금은 모의투자에서 KRX 금현물 주문이 안 돼서(RC9000) 같은 금값을 따라가는
# ETF(거래대금 1위)로 산다 — 실전 전환 때 KRX 금현물(매매차익 비과세)로 바꿀 것.
SLEEVES = [
    Sleeve("TQQQ", "TQQQ", "ND", "TQQQ", .26, "USD", "curve_sell"),
    Sleeve("QLD", "QLD", "NY", "QLD", .26, "USD", "curve"),
    Sleeve("QQQ", "QQQM", "ND", "QQQM", .0557, "USD", "curve"),   # 10-19 인선: QQQ → QQQM(같은 나스닥100, 1주 약 $290이라 작은 금액도 규칙에 가깝게)
    Sleeve("SCHD", "SCHD", "NY", "SCHD", .02785, "USD", "dca"),     # 10-19 인선: SOXL을 빼고 배당 칸 → 10-27 SCHD·버크셔 반반
    Sleeve("BRK", "BRK/B" if BROKER == "kis" else "BRKb", "NY", "BRK.B", .02785, "USD", "dca"),
    Sleeve("CC_DIV", "472150", "KRX", "TIGER 배당커버드콜액티브(472150)", .0093, "KRW", "peace"),
    Sleeve("CC_NAS", "486290", "KRX", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", .0093, "KRW", "peace"),
    Sleeve("GOLD", "411060", "KRX", "ACE KRX금현물(411060)", .35, "KRW", "gold"),
]
USD_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "USD")   # 0.6314
KRW_WEIGHT = sum(s.weight for s in SLEEVES if s.currency == "KRW")   # 0.3686

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
GOLD_YAHOO, GOLD_RSI_MAX, GOLD_SLOTS, GOLD_DEADLINE_DAYS = "411060.KS", 60, 6, 365
# 금 칸(2026-11-09 인선 · FA여정 엔진과 같은 규칙): 금 칸 시작일부터 한 달마다 금 칸의 1/6 씩 '살 차례'가 쌓이고,
# 금 값(ACE KRX금현물 일봉) RSI(14) ≤ 60 인 날 쌓인 만큼 산다. 6번째 차례(5달 뒤)부터는 남은 돈 전부, 1년 지나면 RSI 와 상관없이 전부.
PEACE_LO, PEACE_HI, PEACE_FRAC, PEACE_GAP_DAYS = 0, 50, .10, 3   # 10-19 인선: 35~65 → 65 이하. 11-06 인선: 65 → 50 이하(FA 엔진 1998~: p5 연 14.84→14.86%, MDD 같음)


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
    absorb_into_gold(pos, "KRW")
    return pos


def absorb_into_gold(pos: dict[str, "Position"], currency: str) -> None:
    """비중을 줄인 칸이 이미 새 재원보다 많이 사 둔 몫(현금이 음수)을 금 칸 재원에서 뺀다(같은 통화 칸만).
    2026-11-09 공격형 v1 으로 바꿀 때 커버드콜 칸은 이미 새 비중(0.93%)보다 많이 사 둔 상태였다 — 팔지 않고 그대로 두는 대신,
    그만큼 금 칸이 덜 사서 계좌 전체 현금이 장부보다 모자라지 않게 한다."""
    g = pos.get("GOLD")
    if g is None:
        return
    short = sum(-p.cash for k, p in pos.items() if k != "GOLD" and p.sleeve.currency == currency and p.cash < 0)
    if short > 0:
        g.capital -= short; g.cash -= short


def gold_plan(p: "Position", start: date, today: date, rsi: float | None, price: float, money=None) -> "Order":
    """금 칸 할 일 하나(buy 또는 hold). p.capital 은 금 칸 재원, 산 돈은 p.buys 로 센다(금 칸은 팔지 않는다)."""
    money = money or (lambda v: f"{v:,.0f}원")
    months = (today.year - start.year) * 12 + today.month - start.month - (1 if today.day < start.day else 0)
    slots = max(0, min(GOLD_SLOTS, months + 1))
    spent = sum(q * px for _, q, px in p.buys)
    if p.cash < price:
        return Order("GOLD", "hold", 0, None, f"금 칸 다 삼(남은 돈 {money(p.cash)})")
    if (today - start).days >= GOLD_DEADLINE_DAYS:
        return Order("GOLD", "buy", p.cash, None, f"금 칸 시작({start}) 1년 지남 — 남은 돈 전부")
    if rsi is None:
        return Order("GOLD", "hold", 0, None, "금 RSI 를 못 구해 오늘은 쉼")
    allow = p.cash if slots >= GOLD_SLOTS else min(p.cash, p.capital * slots / GOLD_SLOTS - spent)
    if allow < price:
        return Order("GOLD", "hold", 0, None, f"이번 차례({slots}/{GOLD_SLOTS}) 몫은 다 삼 — 다음 차례를 기다림")
    if rsi > GOLD_RSI_MAX:
        return Order("GOLD", "hold", 0, None, f"살 차례 {money(allow)} 쌓였지만 금 RSI {rsi:.0f} > {GOLD_RSI_MAX} — 내려오면 삼")
    what = "남은 돈 전부" if slots >= GOLD_SLOTS else f"{slots}/{GOLD_SLOTS} 차례까지 쌓인 몫"
    return Order("GOLD", "buy", allow, None, f"금 RSI {rsi:.0f} ≤ {GOLD_RSI_MAX} — {what}")


def gold_rsi(price_now: float, today: date) -> float | None:
    """ACE KRX금현물 일봉(야후) + 지금 값으로 RSI(14). 못 구하면 None(그날은 금을 사지 않는다)."""
    try:
        return rsi_wilder(closes_with_today(fetch_daily_closes(GOLD_YAHOO, "2y"), price_now, today))
    except Exception as exc:   # 야후 일시 장애로 다른 칸 주문까지 멈추지 않게
        print(f"[경고] 금 RSI 계산 실패: {exc}")
        return None


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
    return closes_from_chart(r.json()["chart"]["result"][0])


def closes_from_chart(res: dict) -> list[tuple[date, float]]:
    """야후 일봉에서 (날짜, 종가). 비어 있는 칸(None)은 건너뛴다.

    2026-11-06: 장이 끝났는데 마지막 일봉 종가 칸이 비어 있는 때가 있다 — 11-05 장 마감 13시간 뒤에도 SCHD·TQQQ 의
    11-05 칸이 None 이고, 종가(SCHD 35.42, TQQQ 50.465)는 meta.regularMarketPrice 에만 있었다. 그대로 두면
    RSI·공포 점수가 하루 묵은 값(11-04)으로 계산된다. 그래서 '마지막 칸이 비었고, 그 장이 끝났고
    (regularMarketTime ≥ 그 장의 끝), 그 칸이 바로 그 장' 일 때만 meta 종가로 채운다. 장중에는 채우지 않는다."""
    ts_list = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(ts, tz=timezone.utc).date(), float(c)) for ts, c in zip(ts_list, closes) if c]
    meta = res.get("meta") or {}
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if ts_list and closes and closes[-1] is None and price and at and reg.get("start") is not None and reg.get("end") is not None:
        last = ts_list[-1]
        if reg["start"] <= last < reg["end"] and at >= reg["end"]:
            out.append((datetime.fromtimestamp(last, tz=timezone.utc).date(), float(price)))
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

def _get(url: str, tries: int = 3, timeout: int = 25) -> requests.Response:
    """2026-11-06: CAPE 를 놓치지 않게 — 일시 오류(시간 초과·5xx)면 3·10초 쉬고 다시(최대 3번)."""
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if k < tries - 1:
                time.sleep((3, 10)[min(k, 1)])
    raise RuntimeError(f"{url} {tries}번 시도 실패: {last}")


def fetch_cape_monthly() -> list[tuple[date, float]]:
    r = _get("https://www.multpl.com/shiller-pe/table/by-month")
    rows = re.findall(r"<td>([A-Z][a-z]{2} \d{1,2}, \d{4})</td>\s*<td>\s*(?:&#x2002;|<abbr[^>]*>[^<]*</abbr>)?\s*([\d.]+)",
                      r.text)
    out = sorted((datetime.strptime(d, "%b %d, %Y").date(), float(v)) for d, v in rows)
    if len(out) < 600:
        raise RuntimeError(f"CAPE 기록이 너무 적음({len(out)}개)")
    return out


CAPE_PAGE = "https://www.multpl.com/shiller-pe"


def parse_cape_now(html: str) -> dict:
    """multpl 첫 화면의 'Current Shiller PE Ratio: 38.79 -1.83 (-4.51%) / 4:00 PM EST, Thu Nov 5' 를 그대로 읽는다.
    사람이 사이트에서 보는 바로 그 숫자다. 시각이 4:00 PM 이면 그날 종가, 아니면 장중 값(미국장이 열려 있어 계속 바뀜)."""
    m = re.search(r'<div id="current">.*?</b>\s*([\d.]+)(.*?)<div id="timestamp">\s*([^<]+?)\s*</div>', html, re.S)
    if not m:
        raise RuntimeError("multpl 첫 화면에서 현재 CAPE 를 찾지 못함(화면 모양이 바뀌었을 수 있음)")
    value = float(m[1])
    ch = re.search(r"([+-]?\d+(?:\.\d+)?)\s*\(", m[2])
    change = float(ch[1]) if ch else 0.0
    stamp = " ".join(m[3].split())                      # "4:00 PM EST, Thu Nov 5"
    return {"value": value, "change": change, "prev_close": round(value - change, 2), "stamp": stamp,
            "closed": stamp.startswith("4:00 PM")}


def fetch_cape_now() -> dict:
    return parse_cape_now(_get(CAPE_PAGE).text)


# ── 보험: 실러 교수 원자료 + S&P500 가격으로 직접 계산(multpl 을 못 읽을 때·대조용) ──
# CAPE = S&P500 가격 ÷ 10년 평균 실질 이익. 분모는 한 달에 아주 조금만 변하므로
# '오늘 CAPE = 오늘 S&P500 × (실러 지난달 CAPE ÷ 실러 지난달 평균 S&P500)' 으로 계산한다.
# 2026-11-06 확인: 11-05 직접 계산 38.77 vs multpl 38.79(−0.06%), 최근 36개월 월별 최대 차이 0.78%.
SHILLER_PAGE = "https://shillerdata.com/"
CAPE_CROSSCHECK_PCT = 2.0


def fetch_shiller_monthly() -> list[tuple[date, float, float]]:
    """(그 달 1일, CAPE, 그 달 평균 S&P500). shillerdata.com 첫 화면의 ie_data.xls 링크를 따라간다(주소가 가끔 바뀜)."""
    import io
    import pandas as pd
    page = _get(SHILLER_PAGE).text
    m = re.search(r'href="([^"]*ie_data\.xls[^"]*)"', page)
    if not m:
        raise RuntimeError("shillerdata.com 에서 ie_data.xls 링크를 찾지 못함")
    url = m[1] if m[1].startswith("http") else "https:" + m[1]
    x = pd.read_excel(io.BytesIO(_get(url, timeout=60).content), sheet_name="Data", header=None)
    hdr = [str(v).strip() for v in x.iloc[7]]
    ci = hdr.index("CAPE")
    out = []
    for _, r in x.iloc[8:].iterrows():
        try:
            ym, p, c = f"{float(r[0]):.2f}", float(r[1]), float(r[ci])
        except (TypeError, ValueError):
            continue
        if math.isnan(p) or math.isnan(c):
            continue
        out.append((date(int(ym[:4]), int(ym[5:7]), 1), c, p))
    if len(out) < 600:
        raise RuntimeError(f"실러 CAPE 기록이 너무 적음({len(out)}개)")
    return out


def spx_now() -> float:
    """S&P500 지금 값(장중이면 장중, 마감 뒤면 종가) — 야후 regularMarketPrice."""
    r = _get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d")
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def own_cape(shiller: list[tuple[date, float, float]]) -> float:
    _, c, p = shiller[-1]
    return spx_now() * c / p


def cape_block() -> dict:
    """최근 CAPE가 '1871년~직전 달까지 log(CAPE) 직선 추세' × CAPE_K(1.6)를 넘으면 막는다.
    조회에 실패하면 **막는 쪽**으로 둔다(2026-10 현재 막힌 상태라 실패 시에도 같은 판정).

    2026-11-06: 지금 CAPE 는 multpl 첫 화면(사람이 보는 숫자)과 그 시각을 함께 읽는다. 11-05 계좌 체크가 미국장
    중간(10:25 ET)에 돌아 장중 값 40.13 을 '시각 없이' 보여 줬고, 장 마감 뒤 사이트는 종가 38.79 를 보여 줘서
    '틀린 숫자' 로 보였다. 이제 숫자 옆에 사이트 시각과 종가/장중을 붙이고, 장중이면 직전 종가도 같이 쓴다.
    표(by-month) 맨 윗줄과 첫 화면 숫자가 다르면 그것도 알린다. 판정(막힘/열림)과 기준선 계산 방식은 그대로다.

    2026-11-06(인선: 판정은 그대로, 값만 잘 받아오기): multpl 을 3번까지 다시 읽고, 그래도 못 읽으면 실러 원자료 +
    S&P500 으로 직접 계산한 값으로 판정한다(기준선도 실러 월별 CAPE 로). 둘 다 읽히면 서로 2% 넘게 다를 때 알린다."""
    out = {"cape": None, "line": None, "as_of": None, "error": None, "stamp": None, "closed": None,
           "prev_close": None, "mismatch": None, "source": "multpl", "own": None}
    notes = []
    series = None
    try:
        series = [(d, v) for d, v in fetch_cape_monthly()]
    except Exception as exc:  # noqa: BLE001
        notes.append(f"multpl 표 조회 실패({exc})")
    shiller = None
    try:
        shiller = fetch_shiller_monthly()
        out["own"] = round(own_cape(shiller), 2)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"직접 계산(실러) 실패({exc})")
    if series is None:
        if shiller is None or out["own"] is None:
            return {"hot": True, "cape": None, "line": None, "as_of": None,
                    "error": "CAPE 조회 실패 — 매수를 막는 쪽으로 둠: " + " · ".join(notes)}
        # 실러 월별(지난달까지) + 오늘 직접 계산 값을 multpl 표와 같은 모양으로
        series = [(d, c) for d, c, _ in shiller] + [(date.today(), out["own"])]
        out["source"] = "실러 원자료+S&P500 직접 계산"
    vals = np.log([v for _, v in series])
    t = np.arange(len(vals), dtype=float)
    b, a = np.polyfit(t[:-1], vals[:-1], 1)
    out["line"] = math.exp(a + b * t[-1]) * CAPE_K
    out["as_of"], out["cape"] = series[-1]
    if out["source"] == "multpl":
        try:
            now = fetch_cape_now()
            if abs(now["value"] - out["cape"]) > 0.005:
                notes.append(f"표 맨 윗줄 {out['cape']} ≠ 첫 화면 {now['value']}")
            out.update(cape=now["value"], stamp=now["stamp"], closed=now["closed"], prev_close=now["prev_close"])
        except Exception as exc:  # noqa: BLE001 — 첫 화면을 못 읽어도 표 값으로 판정은 한다
            notes.append(f"첫 화면 조회 실패({exc}) — 표 값 사용")
        if out["own"] and abs(out["own"] / out["cape"] - 1) * 100 > CAPE_CROSSCHECK_PCT:
            notes.append(f"multpl {out['cape']} 과 직접 계산 {out['own']} 이 {CAPE_CROSSCHECK_PCT:.0f}% 넘게 다름")
    else:
        notes.append("multpl 대신 직접 계산 값으로 판정")
    out["mismatch"] = " · ".join(notes) or None
    out["hot"] = out["cape"] > out["line"]
    return out


def cape_text(c: dict) -> str:
    """'CAPE 38.79(multpl 4:00 PM EST, Thu Nov 5 종가) / 기준선 36.1 → 막힘' — 사이트와 바로 대조할 수 있게 시각을 붙인다."""
    if not c.get("cape"):
        return c.get("error") or "CAPE 조회 실패"
    if c.get("stamp"):
        when = f"multpl {c['stamp']} {'종가' if c['closed'] else '장중 — 미국장이 열려 있어 계속 바뀜'}"
        if not c["closed"]:
            when += f", 직전 종가 {c['prev_close']:.2f}"
    elif c.get("source", "multpl") != "multpl":
        when = c["source"]
    else:
        when = f"multpl 표 {c['as_of']}"
    if c.get("own") and c.get("source", "multpl") == "multpl":
        when += f" · 직접 계산 {c['own']:.2f}"
    warn = f" ⚠️ {c['mismatch']}" if c.get("mismatch") else ""
    return f"CAPE {c['cape']:.2f}({when}) / 기준선 {c['line']:.1f} → {'막힘' if c['hot'] else '열림'}{warn}"


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


def plan_kr(pos: dict[str, Position], fg_now: float, today: date, gold: dict | None = None) -> list[Order]:
    """gold = {"start": 금 칸 시작일, "rsi": 금 RSI, "price": ACE KRX금현물 지금 값} — 있으면 금 칸 할 일도 넣는다."""
    orders = []
    if gold and "GOLD" in pos:
        orders.append(gold_plan(pos["GOLD"], gold["start"], today, gold["rsi"], gold["price"]))
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
            "brk_hist": {k: fetch_daily_closes(v["yahoo"], "2y") for k, v in LADDER.items()}, "today_kst": today_kst, "today_us": today_us,
            "gold_rsi": gold_rsi(prices["GOLD"], today_kst) if "GOLD" in prices else None}


def print_status(tag: str, cfg: dict, ctx: dict) -> None:
    f = ctx["fear"]
    c = ctx["cape"]
    acc = f" → RSI 가속 +{RSI_ACCEL_ADD:.0%}p" if f["rsi"] <= RSI_ACCEL_TH else ""
    cape_txt = cape_text(c)
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


def _domestic_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> dict | None:
    import kiwoom_client as K
    import fill_price
    from cooldown import log_trade
    from auto_trade_loop_kiwoom import KiwoomChaseOrder
    from fabot_kr import _holding_qty
    book = K.get_domestic_orderbook(sl.code, mode="demo")
    ref = int(K.abs_price(book.get("sel_fpr_bid", "0") or "0"))
    if ref <= 0:
        _say(f"{sl.key} 매수 못 함 — 호가 0(국내 정규장 아님)")
        return None
    qty = int(amount // ref)
    if qty < 1:
        _say(f"{sl.key} 매수 건너뜀 — {amount:,.0f}원으로는 1주({ref:,}원)도 못 삼")
        return None
    if dry_run:
        _say(f"{sl.key} 매수 {qty}주 @ 약 {ref:,}원 (dry-run, 주문 안 냄) — {note}")
        return None
    before = _holding_qty(sl.code)
    ch = KiwoomChaseOrder(sl.code, "buy", qty, max_reprices=60, max_seconds=kr_chase_seconds(600.0), poll_interval=3.0)
    if ch.max_seconds > 0:
        try:
            ch.run()
        except RuntimeError as exc:   # 정정·취소 실패로 멈춰도 마지막 정산에서 보유 수량으로 다시 센다
            print(f"  호가 추격 중단: {exc}")
    if ch.filled_qty < qty:
        rest = qty - ch.filled_qty
        px = kr_auction_price(max(ref, int(K.abs_price(K.get_domestic_orderbook(sl.code, mode="demo").get("sel_fpr_bid", "0") or "0"))), "buy")
        r = K.place_domestic_order(sl.code, "buy", rest, price=px, mode="demo")
        if r.get("return_code") == 0:
            ch.__dict__.setdefault("order_nos", []).append(r["ord_no"])
            print(f"  넉넉한 지정가 주문: buy {rest}주 @ {px:,}원 (1% 위, 주문번호 {r['ord_no']})")
        else:
            print(f"  넉넉한 지정가 주문 실패: {r.get('return_msg')}")

    def record(n: int) -> None:
        fill = fill_price.safe(fill_price.kiwoom_domestic, sl.code, getattr(ch, "order_nos", [])) or ref
        log_trade(ticker=sl.ledger, action="buy", quantity=n, price=round(fill, 4), fg_score=fg,
                  memo=f"v2 자동실행 — {note}", account=V2_ACCOUNT)
        _say(f"{sl.key} 매수 {n}/{qty}주 @ {fill:,.0f}원 — {note}")
        print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {n}주")
    return _kr_result(sl.key, qty, ch.filled_qty, lambda: _holding_qty(sl.code) - before, record, sl.ledger)


# ── 실제 주문 (한투) ────────────────────────────────────────────────────────

def wait_kr_close_auction() -> None:
    """국내장 15:20~15:30은 장 마감 동시호가 — 그 사이 걸린 주문은 15:30에 한꺼번에 체결된다
    (10-23 키움 486290 73주가 '미체결'로 끝났다가 15:30에 실제 체결된 사고). 15:31 전이면 15:31까지 기다린다."""
    import time
    now = datetime.now(KST)
    until = now.replace(hour=15, minute=31, second=0, microsecond=0)
    if now < until:
        print(f"  장 마감 동시호가 체결을 기다립니다({until:%H:%M} KST까지).")
        time.sleep((until - now).total_seconds())


# 2026-10-27: 486290 매수가 동시호가에서 종가(9,815원)보다 5~10원 낮은 지정가로 걸려 두 계좌 모두 놓쳤다
# (키움은 15:30 전에 스스로 취소까지 했다). 호가 추격은 동시호가 전(15:19:30)까지만 하고, 남은 수량은
# 1% 넉넉한 지정가로 한 번만 낸다 — 동시호가는 모두 같은 종가로 체결되므로 넉넉히 써도 더 비싸게 사지 않는다.
KR_CHASE_UNTIL = (15, 19, 30)
KR_AUCTION_MARGIN = .01


def kr_chase_seconds(max_seconds: float) -> float:
    """호가 추격에 쓸 수 있는 시간(초). 동시호가 직전(15:19:30 KST)을 넘지 않는다. 0이면 추격하지 않는다."""
    now = datetime.now(KST)
    h, m, s = KR_CHASE_UNTIL
    left = (now.replace(hour=h, minute=m, second=s, microsecond=0) - now).total_seconds()
    return max(0.0, min(max_seconds, left))


def kr_auction_price(ref: float, side: str) -> int:
    """동시호가용 지정가: 매수는 1% 위로 올림, 매도는 1% 아래로 내림. ETF 호가 단위 2,000원 미만 1원, 이상 5원."""
    p = ref * (1 + KR_AUCTION_MARGIN if side == "buy" else 1 - KR_AUCTION_MARGIN)
    tick = 1 if p < 2000 else 5
    return int(math.ceil(p / tick) * tick if side == "buy" else math.floor(p / tick) * tick)


def _kr_result(key: str, qty: int, filled: int, held, record, name: str = "") -> dict | None:
    """다 체결됐으면 바로 기록하고 None, 남았으면 마지막 정산(kr_settle)에 넘길 값을 돌려준다."""
    if filled >= qty:
        record(filled)
        return None
    return {"key": key, "name": name or key, "qty": qty, "filled": filled, "held": held, "record": record}


def kr_settle(pending: list[dict]) -> None:
    """남은 국내 주문(넉넉한 지정가)을 모든 종목 주문을 낸 뒤 한 번만 기다려, 보유 수량 차이로 세고 기록한다.
    종목마다 15:31까지 기다리면 다음 종목이 장 마감 뒤로 밀린다."""
    import time
    pending = [p for p in pending if p]
    if not pending:
        return
    time.sleep(15)   # 동시호가 전에 낸 넉넉한 지정가는 곧바로 체결된다
    if any(max(p["filled"], p["held"]()) < p["qty"] for p in pending):
        wait_kr_close_auction()
    for p in pending:
        n = min(max(p["filled"], p["held"]()), p["qty"])
        if n > p["filled"]:
            print(f"  {p['name']} 보유 수량으로 다시 셈: {p['filled']}주 → {n}주")
        if n > 0:
            p["record"](n)
        else:
            _say(f"{p['name']} 주문이 체결되지 않음")


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


def _kis_domestic_buy(sl: Sleeve, amount: float, fg: float, dry_run: bool, note: str) -> dict | None:
    import asyncio
    import fill_price
    import live_order_executor as L
    from cooldown import log_trade
    ref = int(float(L._get_asking_price(sl.code).get("askp1") or 0))
    if ref <= 0:
        _say(f"{sl.key} 매수 못 함 — 호가 0(국내 정규장 아님)")
        return None
    qty = int(amount // ref)
    if qty < 1:
        _say(f"{sl.key} 매수 건너뜀 — {amount:,.0f}원으로는 1주({ref:,}원)도 못 삼")
        return None
    if dry_run:
        _say(f"{sl.key} 매수 {qty}주 @ 약 {ref:,}원 (dry-run, 주문 안 냄) — {note}")
        return None
    h0 = L.get_holding(sl.code)
    before = h0["qty"] if h0 else 0
    ch = L.ChaseOrder(sl.code, "buy", qty, max_reprices=60, max_seconds=kr_chase_seconds(630.0))
    if ch.max_seconds > 0:
        try:
            asyncio.run(ch.run())
        except RuntimeError as exc:   # 정정·취소 실패로 멈춰도 마지막 정산에서 보유 수량으로 다시 센다
            print(f"  호가 추격 중단: {exc}")
    if ch.filled_qty < qty:
        rest = qty - ch.filled_qty
        px = kr_auction_price(max(ref, int(float(L._get_asking_price(sl.code).get("askp1") or 0))), "buy")
        try:
            r = L._place_order(sl.code, "buy", rest, px)
            ch.__dict__.setdefault("order_nos", []).append(r["ODNO"])
            print(f"  넉넉한 지정가 주문: buy {rest}주 @ {px:,}원 (1% 위, 주문번호 {r['ODNO']})")
        except RuntimeError as exc:
            print(f"  넉넉한 지정가 주문 실패: {exc}")

    def held() -> int:
        h = L.get_holding(sl.code)
        return (h["qty"] if h else 0) - before

    def record(n: int) -> None:
        fill = fill_price.safe(fill_price.kis_domestic, sl.code, getattr(ch, "order_nos", [])) or ref
        log_trade(ticker=sl.ledger, action="buy", quantity=n, price=round(fill, 4), fg_score=fg,
                  memo=f"v2 자동실행(한투) — {note}", account=V2_ACCOUNT)
        _say(f"{sl.key} 매수 {n}/{qty}주 @ {fill:,.0f}원 — {note}")
        print(f"매매기록 저장 완료: {V2_ACCOUNT} buy {sl.ledger} {n}주")
    return _kr_result(sl.key, qty, ch.filled_qty, held, record, sl.ledger)


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
        gstart = date.fromisoformat(cfg.get("gold_start") or cfg["start_date"])
        orders = plan_kr(ctx["pos"], ctx["fg"], ctx["today_kst"],
                         gold={"start": gstart, "rsi": ctx["gold_rsi"], "price": ctx["prices"]["GOLD"]})
    if not any(o.side in ("buy", "sell") for o in orders):
        _say("오늘 살 것·팔 것 없음")
    kr_pending: list = []
    for o in orders:
        if o.side == "hold":
            _say(f"{o.sleeve} 대기 — {o.note}")
        elif o.side == "sell":
            sell_us(sl[o.sleeve], int(o.qty), ctx["fg"], dry_run, o.note)
        elif sl[o.sleeve].currency == "USD":
            buy_us(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note)
        else:
            kr_pending.append(buy_kr(sl[o.sleeve], o.amount, ctx["fg"], dry_run, o.note))
    kr_settle(kr_pending)


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
