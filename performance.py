"""모의 검증 성과 — 한투 v2·키움 국내포트를 시작일부터 날마다 평가해 수익률·MDD를 낸다(계좌 체크 텔레그램에 붙임).

2026-10-26 인선: "MDD와 수익률 체크를 잘해야 되고, 매매가 될 시기에 매매가 잘되는지 봐야 되고,
3달간 잘 진행되면 실제 계좌로 넘어갈 것". 대시보드(fabot-trade-journal/v2_equity.js)와 같은 방식으로 센다:
시작 재원 + 그 장부(trades.account)의 매매, 가격은 Yahoo 일봉 종가(국내 종목은 .KS), 원/달러는 KRW=X.
비교로 나스닥100(QQQ × 원/달러)을 같은 날 같은 돈으로 샀다고 본 값도 낸다.

    python performance.py      # 화면에 출력
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import requests

import cooldown
import fabot_kr as kr
import fabot_v2 as v2

KST = timezone(timedelta(hours=9))


def _closes(symbol: str, start: date) -> dict[date, float]:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = {}
    for ts, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"]):
        if c:
            out[(datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=9)).date()] = float(c)
    last = res["meta"].get("regularMarketPrice")
    if last:
        out[datetime.now(KST).date()] = float(last)
    return {d: c for d, c in out.items() if d >= start - timedelta(days=10)}


def _price_on(series: dict[date, float], d: date) -> float | None:
    vals = [c for k, c in sorted(series.items()) if k <= d]
    return vals[-1] if vals else None


def _symbol(ticker: str) -> str:
    m = re.search(r"\d{6}", ticker)
    return f"{m.group(0)}.KS" if m else ticker.replace(".", "-")


def _trades(account: str) -> list[dict]:
    env = cooldown._load_journal_env()
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/trades",
                     params={"select": "trade_date,ticker,action,quantity,price,created_at", "account": f"eq.{account}",
                             "action": "in.(buy,sell)", "order": "created_at.asc"},
                     headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
                     timeout=15)
    r.raise_for_status()
    return r.json()


def equity(account: str, start: date, usd_cap: float, krw_cap: float) -> dict:
    """시작일부터 오늘까지 날마다 평가액(원). 반환: 날짜별 값, 수익률, MDD, 오늘 매매."""
    today = datetime.now(KST).date()
    trades = _trades(account)
    fx = _closes("KRW=X", start)
    series = {t: _closes(_symbol(t), start) for t in {x["ticker"] for x in trades}}
    fx0 = _price_on(fx, start) or _price_on(fx, today)
    start_value = usd_cap * fx0 + krw_cap
    points = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            usd, krw, ok = usd_cap, krw_cap, True
            for t in trades:
                if t["trade_date"] > d.isoformat():
                    continue
                amt = float(t["quantity"]) * float(t["price"])
                sign = -1 if t["action"] == "buy" else 1
                if re.search(r"\d{6}", t["ticker"]):
                    krw += sign * amt
                else:
                    usd += sign * amt
            qty: dict[str, float] = {}
            for t in trades:
                if t["trade_date"] <= d.isoformat():
                    qty[t["ticker"]] = qty.get(t["ticker"], 0) + float(t["quantity"]) * (1 if t["action"] == "buy" else -1)
            f = _price_on(fx, d) or fx0
            for t, q in qty.items():
                if abs(q) < 1e-9:
                    continue
                p = _price_on(series[t], d)
                if p is None:
                    ok = False
                    break
                if re.search(r"\d{6}", t):
                    krw += q * p
                else:
                    usd += q * p
            if ok:
                points.append((d, usd * f + krw))
        d += timedelta(days=1)
    if not points:
        points = [(start, start_value)]
    peak, mdd = start_value, 0.0
    for _, v in points:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    now = points[-1][1]
    todays = [t for t in trades if t["trade_date"] == today.isoformat()]
    return {"start_value": start_value, "now": now, "ret": now / start_value - 1, "mdd": mdd, "today_trades": todays,
            "days": len(points)}


def index_since(symbol: str, start: date) -> dict:
    """같은 날 같은 돈으로 그 지수 ETF를 샀다고 본 원화 수익률·MDD."""
    q, fx = _closes(symbol, start), _closes("KRW=X", start)
    days = sorted(d for d in q if d >= start)
    vals = [q[d] * (_price_on(fx, d) or 0) for d in days]
    vals = [v for v in vals if v]
    if not vals:
        return {"ret": 0.0, "mdd": 0.0}
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"ret": vals[-1] / vals[0] - 1, "mdd": mdd}


BANDS_PATH = __import__("pathlib").Path(__file__).resolve().parent / "backtest_bands.json"


def band_check(key: str, n_days: int, ret: float, mdd: float, nq: dict, sp: dict) -> str:
    """시뮬레이션 범위표(backtest_bands.json: 1998~ 아무 날에 새로 시작해 N거래일 돈 결과의 분위)와 대조."""
    import json
    bands = json.loads(BANDS_PATH.read_text(encoding="utf-8"))[key]
    n = min(65, max(5, int(round(n_days / 5)) * 5))
    b = bands[str(n)]
    def where(x, d, lo=5, hi=95):
        return "범위 안" if d[str(lo)] <= x <= d[str(hi)] else ("⚠️ 범위 아래" if x < d[str(lo)] else "범위 위")
    pp = lambda x: f"{x * 100:+.1f}%"
    parts = [f"수익 {pp(ret)} [시뮬 {pp(b['ret']['5'])}~{pp(b['ret']['95'])}, 중간 {pp(b['ret']['50'])}] {where(ret, b['ret'])}",
             f"MDD {pp(mdd)} [시뮬 하위5% {pp(b['mdd']['5'])}, 중간 {pp(b['mdd']['50'])}] {'범위 안' if mdd >= b['mdd']['5'] else '⚠️ 시뮬보다 깊음'}",
             f"나스닥 대비 {pp(ret - nq['ret'])} [시뮬 {pp(b['ex_nq']['5'])}~{pp(b['ex_nq']['95'])}] {where(ret - nq['ret'], b['ex_nq'])}",
             f"S&P 대비 {pp(ret - sp['ret'])} [시뮬 {pp(b['ex_sp']['5'])}~{pp(b['ex_sp']['95'])}] {where(ret - sp['ret'], b['ex_sp'])}"]
    return f"   시뮬 대조({n}거래일 칸): " + " · ".join(parts)


def signal_line() -> str:
    """매매가 나올 거리: F&G, CAPE 막기, 배당 칸 사다리 RSI(v2는 SCHD·버크셔B 각자, 국내포트는 458730)."""
    import today_signal
    fg = today_signal.get_cnn_score()["score"]
    c = v2.cape_block()
    parts = [f"F&G {fg:.0f}(커버드콜은 65 이하·TQQQ 칸 매도는 {v2.SELL_HALF} 이상)"]
    parts.append(f"CAPE {c['cape']} / 기준선 {c['line']:.1f} → 나스닥 칸 {'막힘' if c['hot'] else '열림'}" if c["cape"] else "CAPE 조회 실패")
    for name, sym in (("SCHD", "SCHD"), ("버크셔B", "BRK-B"), ("TIGER 미국배당다우존스", "458730.KS")):
        try:
            h = v2.fetch_daily_closes(sym, "1y")
            # 10-28: 점검 시각엔 그날 장이 아직이라 전날 종가 기준이다(실제 매매는 마감 무렵 현재가를 붙여 계산) — 기준일을 밝혀 차트와 헷갈리지 않게
            parts.append(f"{name} RSI {v2.rsi_wilder([x for _, x in h]):.0f}({h[-1][0].month}/{h[-1][0].day} 종가 기준, 사다리 첫 매수 {v2.LAD_RSI} 이하)")
        except Exception:
            parts.append(f"{name} RSI 조회 실패")
    return " · ".join(parts)


def report_lines() -> list[str]:
    lines = []
    vc = v2.resolve_capital(v2.load_config())
    kc = kr.load_config()
    strats = [(v2.V2_ACCOUNT, date.fromisoformat(vc["start_date"]), float(vc["usd_capital"]), float(vc["krw_capital"])),
              (kr.ACCOUNT, date.fromisoformat(kc["start_date"]), 0.0, float(kc["krw_capital"]))]
    today = datetime.now(KST).date()
    for (name, start, usd, krw), key in zip(strats, ("v2", "kr")):
        if today < start:
            lines.append(f"📈 {name}: 시작({start}) 전")
            continue
        e = equity(name, start, usd, krw)
        nq, sp = index_since("QQQ", start), index_since("SPY", start)
        lines.append(f"📈 {name} ({e['days'] - 1}거래일): {e['now']:,.0f}원 · 시작 대비 {e['ret']:+.2%} · MDD {e['mdd']:.2%} "
                     f"| 나스닥100 {nq['ret']:+.2%}·MDD {nq['mdd']:.2%} | S&P500 {sp['ret']:+.2%}·MDD {sp['mdd']:.2%} (원화)")
        if e["days"] - 1 >= 1:
            try:
                lines.append(band_check(key, e["days"] - 1, e["ret"], e["mdd"], nq, sp))
            except Exception as exc:
                lines.append(f"   시뮬 대조 실패: {exc}")
        for t in e["today_trades"]:
            lines.append(f"   오늘 {'매수' if t['action'] == 'buy' else '매도'} {t['ticker']} {float(t['quantity']):g}주 @ {float(t['price']):,.2f}")
    try:
        lines.append("🔎 " + signal_line())
    except Exception as exc:
        lines.append(f"🔎 신호 조회 실패: {exc}")
    return lines


if __name__ == "__main__":
    print("\n".join(report_lines()))
