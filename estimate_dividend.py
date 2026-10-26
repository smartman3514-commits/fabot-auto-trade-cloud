"""472150(TIGER 배당커버드콜액티브)의 실제 배당은 KIS 예탁원정보(배당일정) API로 정확히
조회할 수 있다(2026-09-20 확인 — record_date/주당배당금/지급일이 실제 값 그대로 나옴,
kis_price_client.fetch_dividend_schedule 참고). 그래서 "연 15% 가정 월할" 어림값 대신,
이 실제 배당률과 그 기준일(record_date) 시점의 실제 보유수량을 곱해서 정확한 배당액을
계산해 기록한다.

이 API는 계좌별이 아니라 종목 자체의 실제 배당 데이터라 KIS/키움 두 계좌 모두 이 값으로
계산한다 — 실제로 계좌 현금에 반영됐는지 여부와 무관하게 기록해야 한다: 대시보드의
"총수익"은 krwProfit(보유종목 매입가 대비 평가손익, 현금과 무관)에 이 기록된 배당 누적을
더해서 계산하므로, 기록하지 않으면 실제로 받은 배당이 총수익에서 빠진다. (2026-09-19에
"KIS는 실제로 배당이 현금에 반영된다"는 걸 확인하고 이중 계상을 걱정해서 KIS 기록을
잠깐 껐었는데, krwProfit이 애초에 현금을 안 보는 값이라 이중 계상이 아니었다 — 다시 켠다,
2026-09-20.)

보유수량(2026-10-03 수정): 예전엔 매매기록(trades)의 "매수"만 더해서 기준일 보유수량을
계산했는데, (1) 매도를 빼지 않았고 (2) 대화로 낸 7월 테스트 주문처럼 매매기록에 안 남은
체결이 있으면 실제보다 적게 잡혔다(KIS 실제 11,743주 vs 기록 11,353주 → 10월 배당
165,750원 과소). 이제는 **증권사가 알려주는 지금 실제 보유수량**에서 기준일 이후의 매매기록
(매수 − 매도)을 거꾸로 빼서 기준일 보유수량을 구한다. 증권사 조회가 실패할 때만 매매기록
(매수 − 매도)으로 계산하고, 두 값이 다르면 그 차이를 로그에 남긴다.

기록은 실제 지급일(pay_date)로 남긴다 — 이 스크립트를 실행한 날짜가 아니라. 같은
계좌·같은 지급일 기록이 이미 있으면 다시 기록하지 않는다(중복 방지, 매달 5일 실행 중에
과거분도 같이 훑기 때문에 안전하게 여러 번 돌려도 됨).
"""
import sys
from datetime import date

import requests

from cooldown import _load_journal_env
from kis_price_client import fetch_dividend_schedule

COVERED_CALL_STOCK_CODE = "472150"
COVERED_CALL_TRADE_KEY = "TIGER 배당커버드콜액티브(472150)"
# 2026-10-26: 옛 규칙 계좌는 끝났다(키움 10-24 만료·한투 10-26 새 계좌로 초기화). 옛 장부로 계속 세면
# 새 계좌에서 한투 v2가 산 472150을 옛 몫으로 잘못 센다 → 옛 계좌 배당 기록은 멈춘다(지난 기록은 그대로).
ACCOUNTS: list[str] = []
# 계좌 시작(KIS 07-23)보다 여유 있게 이른 조회 시작일 — 놓친 과거분도 같이 훑는다.
SCHEDULE_LOOKBACK_START = "20260701"
# 2026-10-20 인선 요청: 키움 v2(FA 새 규칙) 검증용 — v2가 산 커버드콜의 배당은 "키움 v2"로 따로 기록하고,
# 옛 계좌("키움 모의투자")에는 v2 몫을 뺀 옛 규칙 몫만 기록한다. 486290은 분배금에 15.4%가 원천징수된다
# (2026-07-02 실계좌 입금내역 실측) — 세후로 기록, 472150은 비과세.
# 2026-10-26: v2는 한투 새 계좌로 옮겨 10-27부터 새로 시작(장부 '한투 v2'), 키움은 '키움 국내포트'.
# 두 장부 모두 계좌에 그 전략 하나만 있어 매매기록의 기준일 보유수량 × 실제 주당배당금(세후)으로 기록한다.
V2_ACCOUNT = "한투 v2"
V2_LOOKBACK_START = "20261027"
TAXED = 1 - 0.154
V2_COVERED_CALLS = [
    ("472150", "TIGER 배당커버드콜액티브(472150)", 1.0),
    ("486290", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", TAXED),
]
KR_ACCOUNT = "키움 국내포트"
KR_DISTRIBUTIONS = [   # 국내 상장 해외 ETF 분배금은 15.4% 원천징수, 472150만 비과세(2026-07-02 실계좌 실측)
    ("472150", "TIGER 배당커버드콜액티브(472150)", 1.0),
    ("486290", "TIGER 미국나스닥100타겟데일리커버드콜(486290)", TAXED),
    ("458730", "TIGER 미국배당다우존스(458730)", TAXED),
    ("379810", "KODEX 미국나스닥100(379810)", TAXED),
]


def _sb_headers(env: dict) -> dict:
    return {
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    }


def _get_trades(env: dict, account: str, ticker: str = COVERED_CALL_TRADE_KEY) -> list[dict]:
    """매수·매도 기록 전부(배당 기록 제외)."""
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    params = {
        "select": "trade_date,quantity,action",
        "ticker": f"eq.{ticker}",
        "action": "in.(buy,sell)",
        "account": f"eq.{account}",
        "order": "trade_date.asc",
    }
    resp = requests.get(url, params=params, headers=_sb_headers(env), timeout=10)
    resp.raise_for_status()
    return resp.json()


def _signed(t: dict) -> float:
    return float(t["quantity"]) * (1 if t["action"] == "buy" else -1)


def _shares_held_at(trades: list[dict], as_of_date: str) -> float:
    """매매기록만으로 계산한 기준일 보유수량(매수 − 매도)."""
    return sum(_signed(t) for t in trades if t["trade_date"] <= as_of_date)


def _broker_shares_now(account: str) -> float | None:
    """증권사가 알려주는 지금 실제 보유수량. 조회 실패 시 None."""
    try:
        if account == "KIS 모의투자":
            from live_order_executor import get_holding
            h = get_holding(COVERED_CALL_STOCK_CODE)
        else:
            import auto_trade_loop_kiwoom
            h = auto_trade_loop_kiwoom._current_holding()
        return float(h["qty"]) if h else 0.0
    except Exception as exc:
        print(f"{account}: 증권사 보유수량 조회 실패({exc}) — 매매기록으로 계산합니다.")
        return None


def _shares_at_record(trades: list[dict], broker_now: float | None, record_date: str) -> float:
    """지금 실제 보유수량 − 기준일 이후 매매기록 = 기준일 보유수량. 증권사 값이 없으면 매매기록만."""
    if broker_now is None:
        return _shares_held_at(trades, record_date)
    return broker_now - sum(_signed(t) for t in trades if t["trade_date"] > record_date)


def _existing_dividend_pay_dates(env: dict, account: str, ticker: str = COVERED_CALL_TRADE_KEY) -> set:
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    params = {
        "select": "trade_date",
        "ticker": f"eq.{ticker}",
        "action": "eq.dividend",
        "account": f"eq.{account}",
    }
    resp = requests.get(url, params=params, headers=_sb_headers(env), timeout=10)
    resp.raise_for_status()
    return {row["trade_date"] for row in resp.json()}


def _log_dividend(env: dict, account: str, pay_date: str, amount: int, memo: str,
                  ticker: str = COVERED_CALL_TRADE_KEY) -> None:
    url = f"{env['SUPABASE_URL']}/rest/v1/trades"
    body = {
        "trade_date": pay_date,
        "ticker": ticker,
        "action": "dividend",
        "quantity": 1,
        "price": amount,
        "fg_score": None,
        "memo": memo,
        "account": account,
    }
    headers = {**_sb_headers(env), "Content-Type": "application/json", "Prefer": "return=minimal"}
    resp = requests.post(url, json=body, headers=headers, timeout=10)
    resp.raise_for_status()


def main() -> int:
    env = _load_journal_env()
    schedule = fetch_dividend_schedule(
        COVERED_CALL_STOCK_CODE, SCHEDULE_LOOKBACK_START, date.today().strftime("%Y%m%d")
    )
    if not schedule:
        print("배당 일정 조회 결과가 없습니다.")
        return 0

    ok = True
    for account in ACCOUNTS:
        try:
            trades = _get_trades(env, account)
            if not trades:
                print(f"{account}: 472150 매수 기록 없음 — 건너뜁니다.")
                continue
            already = _existing_dividend_pay_dates(env, account)
            broker_now = _broker_shares_now(account)
            if broker_now is not None and account == "키움 모의투자":
                # 같은 키움 계좌에 v2가 산 몫이 섞여 있다 — 옛 규칙 몫만 남긴다(v2 몫은 아래 _v2_dividends).
                v2_now = _shares_held_at(_get_trades(env, V2_ACCOUNT), "9999-12-31")
                broker_now -= v2_now
            if broker_now is not None:
                ledger_now = _shares_held_at(trades, "9999-12-31")
                if abs(ledger_now - broker_now) > 0.5:
                    print(f"{account}: ⚠️ 매매기록 {ledger_now:,.0f}주 vs 실제 보유 {broker_now:,.0f}주 — 실제 보유 기준으로 계산합니다(매매기록 보정 필요).")
            today_s = date.today().isoformat()
            for div in schedule:
                if div["pay_date"] in already:
                    continue
                if div["pay_date"] > today_s:
                    continue  # 2026-10-03: 평일 매일 실행으로 바꿔서, 지급일이 아직 안 된 배당은 기다린다
                shares = _shares_at_record(trades, broker_now, div["record_date"])
                if shares <= 0:
                    continue  # 기준일 이후에 처음 매수한 계좌는 그 배당 대상이 아님
                amount = round(shares * div["per_share"])
                if amount <= 0:
                    continue
                memo = (f"실제 배당(예탁원 배당일정 API, 기준일 {div['record_date']} 주당 "
                        f"{div['per_share']:,.0f}원 × 그날 실제 보유 {shares:,.0f}주, 지급일 {div['pay_date']})")
                _log_dividend(env, account, div["pay_date"], amount, memo)
                print(f"{account}: {div['pay_date']} 배당 {amount:,}원 기록 완료 "
                      f"(기준일 {div['record_date']}, {shares:,.0f}주 × {div['per_share']:,.0f}원)")
        except Exception as exc:
            ok = False
            print(f"{account} 배당 처리 실패: {exc}")

    for account, items in ((V2_ACCOUNT, V2_COVERED_CALLS), (KR_ACCOUNT, KR_DISTRIBUTIONS)):
        try:
            _v2_dividends(env, account, items)
        except Exception as exc:
            ok = False
            print(f"{account} 배당 처리 실패: {exc}")

    return 0 if ok else 1


def _v2_dividends(env: dict, account: str = V2_ACCOUNT, items: list = V2_COVERED_CALLS) -> None:
    """전략 장부가 산 종목의 배당 — 매매기록의 기준일 보유수량 × 실제 주당배당금(세후), 실제 지급일로 기록."""
    V2_ACCOUNT = account   # 아래 메시지·기록에 쓰는 이름
    today_s = date.today().isoformat()
    for code, key, net in items:
        trades = _get_trades(env, V2_ACCOUNT, key)
        if not trades:
            print(f"{V2_ACCOUNT}: {code} 매수 기록 없음 — 건너뜁니다.")
            continue
        schedule = fetch_dividend_schedule(code, V2_LOOKBACK_START, date.today().strftime("%Y%m%d"))
        already = _existing_dividend_pay_dates(env, V2_ACCOUNT, key)
        for div in schedule:
            if div["pay_date"] in already or div["pay_date"] > today_s:
                continue
            shares = _shares_held_at(trades, div["record_date"])
            amount = round(shares * div["per_share"] * net)
            if shares <= 0 or amount <= 0:
                continue
            tax = "" if net == 1.0 else ", 15.4% 원천징수 뒤"
            memo = (f"{account} 배당(예탁원 배당일정 API, 기준일 {div['record_date']} 주당 {div['per_share']:,.0f}원 × "
                    f"보유 {shares:,.0f}주{tax}, 지급일 {div['pay_date']})")
            _log_dividend(env, V2_ACCOUNT, div["pay_date"], amount, memo, ticker=key)
            print(f"{V2_ACCOUNT}: {code} {div['pay_date']} 배당 {amount:,}원 기록 완료 ({shares:,.0f}주)")


if __name__ == "__main__":
    sys.exit(main())
