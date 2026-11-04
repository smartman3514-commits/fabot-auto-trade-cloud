"""현금(예수금) 이자를 RP처럼 하루 단위로 계산해 기록한다(2026-11-04 인선 요청).

모의계좌는 예수금에 실제 이자가 안 붙어서 연 3%(RP 가정)를 추정해 기록한다. 예전에는 매달 5일에
'그날 현금 × 0.25%(한 달치)'를 한 번에 넣어서, 10-27에 시작한 계좌도 11-05에 한 달치를 받는 등
날수와 맞지 않았다. 이제는 매일 아침 실행해서, 마지막으로 기록한 날 다음 날부터 어제(한국 날짜)까지
**하루하루 그날 끝의 현금 × 3% ÷ 365** 를 더해 한 줄로 기록한다(매일 돌면 하루 한 줄). RP 이자는
주말·공휴일에도 붙으므로 달력 날짜로 센다.

그날 끝의 현금은 전략 장부(매매기록)로 다시 계산한다 — 그 날짜까지의 매수·매도만 넣어 장부를 만들면
칸별 현금이 나온다. 그래서 며칠을 한꺼번에 채워도 날마다의 실제 현금으로 계산된다.
  - 한투 v2: 원화 칸 현금, 달러 칸 현금(그날 원/달러 종가로 원화 환산) — fabot_v2 장부
  - 키움 국내포트: 원화 현금 — fabot_kr 장부
  - KIS 모의투자: 한투 계좌 원화 중 v2에 넣지 않은 나머지(지금 계좌 원화 − v2 원화 칸, 매매 없음)

기록은 action="dividend", ticker=현금(이자,원화)/현금(이자,달러) — 대시보드가 배당과 같은 '현금흐름'으로
총수익에 더하고, 표에서는 이자로 따로 보여 준다(app.js CASH_INTEREST_TICKERS 와 같은 문자열).
같은 계좌·통화의 마지막 기록 날짜 다음 날부터만 계산하므로 여러 번 돌려도 두 번 쌓이지 않는다.
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import requests

from cooldown import _load_journal_env
from live_order_executor import _inquire_balance_raw

CASH_INTEREST_TRADE_KEY_KRW = "현금(이자,원화)"  # app.js의 CASH_INTEREST_TICKER_KRW와 동일해야 함
CASH_INTEREST_TRADE_KEY_USD = "현금(이자,달러)"  # app.js의 CASH_INTEREST_TICKER_USD와 동일해야 함
ANNUAL_RATE = 0.03
DAILY_RATE = ANNUAL_RATE / 365
KST = timezone(timedelta(hours=9))
# 예전(월 단위) 기록이 있던 'KIS 모의투자'는 한투 계좌가 10-26 초기화된 뒤(새 계좌 10-27~)부터 일 단위로 센다.
KIS_REMAINDER_START = "2026-10-27"


def _headers(env: dict) -> dict:
    return {"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"}


def _last_interest_date(env: dict, account: str, ticker: str) -> date | None:
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/trades", headers=_headers(env), timeout=10,
                     params={"select": "trade_date", "ticker": f"eq.{ticker}", "action": "eq.dividend",
                             "account": f"eq.{account}", "order": "trade_date.desc", "limit": 1})
    r.raise_for_status()
    rows = r.json()
    return date.fromisoformat(rows[0]["trade_date"]) if rows else None


def _insert(env: dict, account: str, ticker: str, day: date, amount: int, memo: str) -> None:
    r = requests.post(f"{env['SUPABASE_URL']}/rest/v1/trades", timeout=10,
                      headers={**_headers(env), "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json={"trade_date": day.isoformat(), "ticker": ticker, "action": "dividend", "quantity": 1,
                            "price": amount, "fg_score": None, "memo": memo, "account": account})
    r.raise_for_status()


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _cash_series(build, cfg: dict, trades: list[dict], days: list[date], currency: str | None) -> dict[date, float]:
    """그날 끝(그날 매매까지 반영)의 장부 현금. currency 가 None 이면 모든 칸."""
    out = {}
    for d in days:
        upto = [t for t in trades if t["trade_date"] <= d.isoformat()]
        pos = build(cfg, upto)
        out[d] = max(0.0, sum(p.cash for p in pos.values() if currency is None or p.sleeve.currency == currency))
    return out


def _usdkrw_by_day(days: list[date], fallback: float) -> dict[date, float]:
    """그날(또는 그 전 가장 가까운 날) 원/달러 종가. 못 받으면 fallback."""
    try:
        import fabot_v2 as v2
        closes = sorted(v2.fetch_daily_closes("KRW=X", "3mo"))
    except Exception as exc:
        print(f"원/달러 종가 조회 실패({exc}) — 고정 환율 {fallback:,.2f} 사용")
        return {d: fallback for d in days}
    out = {}
    for d in days:
        prev = [c for cd, c in closes if cd <= d]
        out[d] = prev[-1] if prev else fallback
    return out


def _record(env: dict, account: str, ticker: str, label: str, start_floor: date, yesterday: date,
            krw_cash_by_day, dry_run: bool) -> None:
    last = _last_interest_date(env, account, ticker)
    start = max(start_floor, last + timedelta(days=1)) if last else start_floor
    if start > yesterday:
        print(f"{account}({label}): {yesterday} 까지 이미 기록됨 — 건너뜁니다.")
        return
    days = _days(start, yesterday)
    cash = krw_cash_by_day(days)
    amount = round(sum(cash[d] * DAILY_RATE for d in days))
    avg = sum(cash.values()) / len(days)
    period = f"{start}" if start == yesterday else f"{start}~{yesterday}"
    memo = (f"추정 현금 이자(RP 연 3% 일 단위: 하루 끝 {label} 현금 × 3% ÷ 365, {period} {len(days)}일, "
            f"평균 현금 {avg:,.0f}원)")
    if amount <= 0:
        print(f"{account}({label}): {period} 현금 없음 — 기록하지 않습니다.")
        return
    if dry_run:
        print(f"{account}({label}): [dry-run] {period} {len(days)}일 이자 {amount:,}원 (평균 현금 {avg:,.0f}원)")
        return
    _insert(env, account, ticker, yesterday, amount, memo)
    print(f"{account}({label}): {period} {len(days)}일 이자 {amount:,}원 기록 (평균 현금 {avg:,.0f}원)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="계산만 하고 실제 기록은 하지 않음")
    args = parser.parse_args()
    env = _load_journal_env()
    yesterday = datetime.now(KST).date() - timedelta(days=1)
    ok = True

    # 한투 v2 — 원화 칸·달러 칸
    try:
        import fabot_v2 as v2
        cfg = v2.resolve_capital(v2.load_config())
        trades = v2.fetch_v2_trades()
        floor = date.fromisoformat(cfg["start_date"])
        _record(env, v2.V2_ACCOUNT, CASH_INTEREST_TRADE_KEY_KRW, "원화", floor, yesterday,
                lambda ds: _cash_series(v2.build_ledger, cfg, trades, ds, "KRW"), args.dry_run)

        def usd_in_krw(ds):
            usd = _cash_series(v2.build_ledger, cfg, trades, ds, "USD")
            fx = _usdkrw_by_day(ds, float(cfg.get("fx") or 1380.3))
            return {d: usd[d] * fx[d] for d in ds}
        _record(env, v2.V2_ACCOUNT, CASH_INTEREST_TRADE_KEY_USD, "달러(원화 환산)", floor, yesterday,
                usd_in_krw, args.dry_run)
    except Exception as exc:
        ok = False
        print(f"한투 v2 이자 실패: {exc}")

    # KIS 모의투자 — 한투 계좌 원화 중 v2에 넣지 않은 나머지(이 돈은 매매하지 않는다)
    try:
        import fabot_v2 as v2
        cfg = v2.resolve_capital(v2.load_config())
        v2_krw_now = sum(p.cash for p in v2.build_ledger(cfg, v2.fetch_v2_trades()).values() if p.sleeve.currency == "KRW")
        broker_krw = float(_inquire_balance_raw()["output2"][0]["dnca_tot_amt"])
        remainder = max(0.0, broker_krw - max(0.0, v2_krw_now))
        _record(env, "KIS 모의투자", CASH_INTEREST_TRADE_KEY_KRW, "원화(v2 밖 나머지)",
                date.fromisoformat(KIS_REMAINDER_START), yesterday, lambda ds: {d: remainder for d in ds}, args.dry_run)
    except Exception as exc:
        ok = False
        print(f"KIS 모의투자 이자 실패: {exc}")

    # 키움 국내포트 — 원화 현금(국내 ETF만)
    try:
        import fabot_kr as kr
        cfg = kr.load_config()
        trades = kr.fetch_trades()
        _record(env, kr.ACCOUNT, CASH_INTEREST_TRADE_KEY_KRW, "원화", date.fromisoformat(cfg["start_date"]), yesterday,
                lambda ds: _cash_series(kr.build_ledger, cfg, trades, ds, None), args.dry_run)
    except Exception as exc:
        ok = False
        print(f"키움 국내포트 이자 실패: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
