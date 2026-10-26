"""계좌 체크 — 장부(Supabase)와 실제 계좌, 대시보드가 서로 맞는지 매일 확인한다.

2026-10-20 인선 요청("이런 일이 생기지 않도록 계좌 체크")으로 키움 v2용으로 만들었고,
2026-10-26 v2가 한투로 옮겨 가고 키움은 국내포트가 되면서 두 계좌를 함께 보도록 바꿨다.
두 계좌 모두 이제 전략 하나만 들어 있어 "실제 = 장부"가 그대로 맞아야 한다.

  한투(50220595) = 한투 v2   : 종목별 수량, 달러 현금 ≥ 장부 달러 현금, 원화 현금 ≥ 장부 원화 현금
  키움(81372441) = 국내포트  : 종목별 수량(418660은 두 칸 합), 원화 현금 ≈ 장부 원화 현금
  대시보드(demo_balance)     : 두 증권사 종목·수량이 실제와 같은지, 오래된 줄이 남았는지
문제가 있으면 🚨, 없으면 ✅ 한 줄을 텔레그램으로 보낸다. 주문은 내지 않는다(조회만).

    python account_check.py            # 체크 + 텔레그램
    python account_check.py --no-send  # 화면에만
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

import cooldown
import fabot_kr as kr
import fabot_v2 as v2

KST = timezone(timedelta(hours=9))
USD_TOL_FRAC = .01        # 장부는 수수료를 안 빼서 실제보다 조금 크다 — 재원의 1%까지는 봐준다
KRW_TOL_FRAC = .005       # 국내 수수료·세금 몫 — 재원의 0.5%까지는 봐준다
STALE_HOURS = 3           # 같은 증권사 다른 줄보다 이만큼 오래 안 바뀐 줄은 지워졌어야 할 줄


def _num(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def dashboard_rows() -> list[dict]:
    env = cooldown._load_journal_env()
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/demo_balance",
                     params={"select": "broker,ticker,quantity,updated_at"},
                     headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
                     timeout=15)
    r.raise_for_status()
    return r.json()


def check_kis(problems: list[str], info: list[str]) -> dict[str, int]:
    """한투 v2. 돌려주는 값: 실제 보유 {종목: 수량}(대시보드 대조용)."""
    import live_order_executor as L
    import overseas_order_executor as O
    cfg = v2.resolve_capital(v2.load_config())
    pos = v2.build_ledger(cfg, v2.fetch_v2_trades())
    actual: dict[str, int] = {}
    for s in v2.SLEEVES:
        book = int(round(pos[s.key].qty))
        if s.currency == "USD":
            h = O.get_overseas_holding(s.code, v2.KIS_EXCG[s.code])
        else:
            h = L.get_holding(s.code)
        real = h["qty"] if h else 0
        if real:
            actual[s.code] = real
        if real != book:
            problems.append(f"한투 {s.code}: 실제 {real}주 ≠ v2 장부 {book}주")
    for row in L._inquire_balance_raw()["output1"]:       # v2 칸에 없는 국내 종목
        q = int(_num(row.get("hldg_qty")))
        if q and row["pdno"] not in {s.code for s in v2.SLEEVES}:
            actual[row["pdno"]] = q
            problems.append(f"한투 {row['pdno']}: v2 칸에 없는 종목 {q}주")
    usd_real = O.get_overseas_order_capacity("TQQQ", "NASD", 100.0)["cash"]
    usd_book = sum(p.cash for p in pos.values() if p.sleeve.currency == "USD")
    tol = USD_TOL_FRAC * float(cfg["usd_capital"])
    if usd_book > usd_real + tol:
        problems.append(f"한투 달러: v2 장부 현금 ${usd_book:,.2f} > 실제 주문가능 ${usd_real:,.2f} — v2가 없는 돈을 쓰려 할 수 있음")
    elif usd_real > usd_book + max(tol, 100):
        problems.append(f"한투 달러: 실제 ${usd_real:,.2f}가 v2 장부 ${usd_book:,.2f}보다 ${usd_real - usd_book:,.0f} 많음 — 기록이 빠졌을 수 있음")
    krw_real = L.get_cash_balance()
    krw_book = sum(p.cash for p in pos.values() if p.sleeve.currency == "KRW")
    if krw_book > krw_real + 1:
        problems.append(f"한투 원화: v2 장부 현금 {krw_book:,.0f}원 > 실제 {krw_real:,.0f}원")
    info.append(f"한투 v2: 달러 ${usd_real:,.0f}(장부 ${usd_book:,.0f}) · 원화 칸 장부 {krw_book:,.0f}원(계좌 {krw_real:,.0f}원, v2 밖 원화 포함)")
    return actual


def check_kiwoom(problems: list[str], info: list[str]) -> dict[str, int]:
    """키움 국내포트."""
    import kiwoom_client as K
    cfg = kr.load_config()
    pos = kr.build_ledger(cfg, kr.fetch_trades())
    h = K.get_domestic_holdings(mode="demo")
    actual = {str(x["stk_cd"]).lstrip("A"): int(_num(x["rmnd_qty"])) for x in h.get("acnt_evlt_remn_indv_tot", [])
              if _num(x.get("rmnd_qty")) > 0}
    book: dict[str, int] = {}
    for s in kr.SLEEVES:
        book[s.code] = book.get(s.code, 0) + int(round(pos[s.key].qty))
    for code in sorted(set(book) | set(actual)):
        if actual.get(code, 0) != book.get(code, 0):
            what = "국내포트 칸에 없는 종목" if code not in book else "장부"
            problems.append(f"키움 {code}: 실제 {actual.get(code, 0)}주 ≠ {what} {book.get(code, 0)}주")
    c = K.get_domestic_cash_balance(mode="demo")
    krw_real = _num(c.get("d2_entra") or c.get("ord_alow_amt"))
    krw_book = sum(p.cash for p in pos.values())
    tol = KRW_TOL_FRAC * float(cfg["krw_capital"])
    if abs(krw_real - krw_book) > tol:
        problems.append(f"키움 원화: 실제(결제 뒤) {krw_real:,.0f}원 ≠ 장부 {krw_book:,.0f}원 (차이 {krw_real - krw_book:+,.0f}원)")
    info.append(f"키움 국내포트: 원화 {krw_real:,.0f}원(장부 {krw_book:,.0f}원)")
    return actual


def check() -> tuple[list[str], list[str]]:
    problems, info = [], []
    actual = {}
    for broker, fn in (("KIS", check_kis), ("Kiwoom", check_kiwoom)):
        try:
            actual[broker] = fn(problems, info)
        except Exception as exc:                   # 한쪽 조회가 실패해도 다른 쪽은 본다
            problems.append(f"{broker} 체크 실패: {exc}")
    rows = dashboard_rows()
    for broker, real in actual.items():
        shown = {r["ticker"]: r for r in rows if r["broker"] == broker}
        for code, q in real.items():
            r = shown.get(code)
            if r is None:
                problems.append(f"대시보드: {broker} {code} {q}주가 화면에 없음")
            elif int(_num(r["quantity"])) != q:
                problems.append(f"대시보드: {broker} {code} 화면 {int(_num(r['quantity']))}주 ≠ 실제 {q}주")
        for code, r in shown.items():
            if code not in real:
                problems.append(f"대시보드: {broker} {code} {int(_num(r['quantity']))}주가 화면에 남아 있음(실제 0주)")
    for broker in {r["broker"] for r in rows}:
        ts = {r["ticker"]: datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")) for r in rows if r["broker"] == broker}
        newest = max(ts.values())
        for t, u in ts.items():
            if newest - u > timedelta(hours=STALE_HOURS):
                problems.append(f"대시보드: {broker} {t} 줄이 {(newest - u).total_seconds() / 3600:.0f}시간 동안 안 바뀜(지워졌어야 할 줄일 수 있음)")
    return problems, info


def send(text: str) -> None:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("(텔레그램 설정 없음 — 보내지 않음)")
        return
    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text[:4000]}, timeout=20)


def main() -> None:
    now = datetime.now(KST).strftime("%m-%d %H:%M KST")
    try:
        problems, info = check()
    except Exception as exc:                       # 조회 자체가 실패해도 조용히 넘어가지 않는다
        problems, info = [f"체크 실행 실패: {exc}"], []
    title = f"[계좌 체크 · 한투 v2 + 키움 국내포트 · {now}]"
    if problems:
        text = "\n".join([f"🚨 {title}", *[f"- {p}" for p in problems], *info])
    else:
        text = "\n".join([f"✅ {title} 장부·계좌·대시보드 일치", *info])
    print(text)
    if "--no-send" not in sys.argv:
        send(text)


if __name__ == "__main__":
    main()
