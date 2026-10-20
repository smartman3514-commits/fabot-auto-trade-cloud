"""키움 v2 계좌 체크 — 장부(Supabase)와 실제 계좌, 대시보드가 서로 맞는지 매일 확인한다.

2026-10-20 인선 요청("이런 일이 생기지 않도록 계좌 체크"). 그날 발견한 세 가지를 다시 못 지나가게 한다.
  1) v2 재원이 실제 달러보다 컸다 — 미결제 매수대금이 빠지지 않은 d0 예수금을 썼다.
  2) 대시보드에 이미 판 TQQQ 1407주가 남았다.
  3) 대시보드 현금에서 달러 예수금이 빠졌다.

확인하는 것
  - 종목별 수량: 실제 계좌 = v2 장부 + 옛 규칙 몫(v2_config "legacy_krw_qty")
  - 달러: 결제 뒤 실제 달러(ust21160 마지막 dN) ≥ v2 장부의 달러 현금
  - 원화: 실제 원화(d2) ≥ v2 장부의 원화 현금
  - 대시보드(demo_balance): 키움 종목·수량이 실제와 같은지, 오래된 줄이 남았는지
문제가 있으면 🚨, 없으면 ✅ 한 줄을 텔레그램으로 보낸다. 주문은 내지 않는다(조회만).

    python account_check.py            # 체크 + 텔레그램
    python account_check.py --no-send  # 화면에만
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import cooldown
import fabot_v2 as v2
import kiwoom_client as K

KST = timezone(timedelta(hours=9))
USD_TOL_FRAC = .01        # 장부는 수수료를 안 빼서 실제보다 조금 크다 — 재원의 1%까지는 봐준다
KIWOOM_USD_START = 100_000.0   # 키움 해외 모의계좌 시작 달러(대시보드 CASH_RECONSTRUCTION과 같은 값)
FEE = .0025               # 키움 모의 해외 수수료(816주 매수 $55,292.16 → 정산 $55,430.47 실측)
RECORD_TOL = 1_000.0      # 기록 가격은 주문가라 실제 체결가와 조금 다를 수 있다
STALE_HOURS = 3           # 같은 증권사 다른 줄보다 이만큼 오래 안 바뀐 줄은 지워졌어야 할 줄


def _num(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def settled_usd() -> tuple[float, float, float]:
    """(d0 달러, 결제 뒤 달러, 환율). 결제 뒤 = d4→d0 중 처음 값이 있는 것(가장 늦은 결제일)."""
    d = K.get_overseas_cash_balance(mode="demo")
    last = next((d[k] for k in ("d4_usd_fx_entr", "d3_usd_fx_entr", "d2_usd_fx_entr", "d1_usd_fx_entr", "d0_usd_fx_entr")
                 if str(d.get(k, "")).strip()), "0")
    return _num(d.get("d0_usd_fx_entr")), _num(last), _num(d.get("usd_exch_rate"))


def overseas_qty(code: str, exchange: str) -> int:
    data = K.get_overseas_balance(stk_cd=code, stex_tp=exchange, mode="demo")
    return sum(int(_num(h.get("poss_qty"))) for h in data.get("result_list", []) if h.get("stk_cd") == code)


def domestic_state() -> tuple[dict[str, int], float]:
    h = K.get_domestic_holdings(mode="demo")
    qty = {str(x["stk_cd"]).lstrip("A"): int(_num(x["rmnd_qty"])) for x in h.get("acnt_evlt_remn_indv_tot", [])
           if _num(x.get("rmnd_qty")) > 0}
    c = K.get_domestic_cash_balance(mode="demo")
    return qty, _num(c.get("d2_entra") or c.get("ord_alow_amt"))


def dashboard_rows() -> list[dict]:
    env = cooldown._load_journal_env()
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/demo_balance",
                     params={"select": "broker,ticker,quantity,updated_at"},
                     headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
                     timeout=15)
    r.raise_for_status()
    return r.json()


def usd_from_records() -> float:
    """키움 계좌들의 해외 매매기록만으로 계산한 달러: 시작 $100,000 − 매수(수수료 포함) + 매도(수수료 뺌)."""
    env = cooldown._load_journal_env()
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/trades",
                     params={"select": "account,ticker,action,quantity,price", "account": "like.키움*",
                             "action": "in.(buy,sell)"},
                     headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"},
                     timeout=15)
    r.raise_for_status()
    usd = KIWOOM_USD_START
    for t in r.json():
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", str(t["ticker"])):   # 미국 종목(TQQQ·BRK.B 등)만, 국내는 원화
            continue
        amt = float(t["quantity"]) * float(t["price"])
        usd += -amt * (1 + FEE) if t["action"] == "buy" else amt * (1 - FEE)
    return usd


def check() -> tuple[list[str], list[str]]:
    problems, info = [], []
    cfg = v2.resolve_capital(v2.load_config())
    pos = v2.build_ledger(cfg, v2.fetch_v2_trades())
    legacy = {str(k): int(n) for k, n in (cfg.get("legacy_krw_qty") or {}).items()}

    # 1) 종목별 수량
    actual_all: dict[str, int] = {}
    dom_qty, krw_cash = domestic_state()
    for s in v2.SLEEVES:
        book = int(round(pos[s.key].qty))
        if s.currency == "USD":
            real = overseas_qty(s.code, s.exchange)
            expect = book
        else:
            real = dom_qty.get(s.code, 0)
            expect = book + legacy.get(s.code, 0)
        if real:
            actual_all[s.code] = real
        if real != expect:
            extra = f" (v2 {book} + 옛 규칙 {legacy.get(s.code, 0)})" if s.currency == "KRW" else ""
            problems.append(f"{s.code}: 실제 {real}주 ≠ 장부 {expect}주{extra}")
    for code, q in dom_qty.items():           # v2 칸에 없는 국내 종목
        if code not in {s.code for s in v2.SLEEVES}:
            actual_all[code] = q
            if q != legacy.get(code, 0):
                problems.append(f"{code}: v2 칸에 없는 종목 {q}주 (옛 규칙 몫 {legacy.get(code, 0)}주로 적혀 있음)")

    # 2) 달러
    d0, usd_real, fx = settled_usd()
    usd_book = sum(p.cash for p in pos.values() if p.sleeve.currency == "USD")
    tol = USD_TOL_FRAC * float(cfg.get("usd_capital") or 0)
    if usd_book > usd_real + tol:
        problems.append(f"달러: v2 장부 현금 ${usd_book:,.2f} > 결제 뒤 실제 ${usd_real:,.2f} — v2가 없는 돈을 쓰려 할 수 있음")
    elif usd_real > usd_book + max(tol, 100):
        problems.append(f"달러: 결제 뒤 실제 ${usd_real:,.2f}가 v2 장부 ${usd_book:,.2f}보다 ${usd_real - usd_book:,.0f} 많음 — 재원이 덜 잡혔거나 v2 밖의 돈")
    usd_rec = usd_from_records()
    if abs(usd_rec - usd_real) > RECORD_TOL:
        problems.append(f"달러: 매매기록으로 계산하면 ${usd_rec:,.0f}인데 증권사 결제 뒤 값은 ${usd_real:,.0f} (차이 ${usd_real - usd_rec:+,.0f})")
    if abs(d0 - usd_real) > 1:
        info.append(f"결제 대기 있음: 지금 ${d0:,.2f} → 결제 뒤 ${usd_real:,.2f}")

    # 3) 원화
    krw_book = sum(p.cash for p in pos.values() if p.sleeve.currency == "KRW")
    if krw_book > krw_cash + 1:
        problems.append(f"원화: v2 장부 현금 {krw_book:,.0f}원 > 실제 {krw_cash:,.0f}원")

    # 4) 대시보드
    rows = dashboard_rows()
    kw = {r["ticker"]: r for r in rows if r["broker"] == "Kiwoom"}
    for code, q in actual_all.items():
        r = kw.get(code)
        if r is None:
            problems.append(f"대시보드: 키움 {code} {q}주가 화면에 없음")
        elif int(_num(r["quantity"])) != q:
            problems.append(f"대시보드: 키움 {code} 화면 {int(_num(r['quantity']))}주 ≠ 실제 {q}주")
    for code, r in kw.items():
        if code not in actual_all:
            problems.append(f"대시보드: 키움 {code} {int(_num(r['quantity']))}주가 화면에 남아 있음(실제 0주)")
    for broker in {r["broker"] for r in rows}:
        ts = {r["ticker"]: datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")) for r in rows if r["broker"] == broker}
        newest = max(ts.values())
        for t, u in ts.items():
            if newest - u > timedelta(hours=STALE_HOURS):
                problems.append(f"대시보드: {broker} {t} 줄이 {(newest - u).total_seconds() / 3600:.0f}시간 동안 안 바뀜(지워졌어야 할 줄일 수 있음)")

    info.append(f"달러 ${usd_real:,.0f}(장부 ${usd_book:,.0f}) · 원화 v2 {krw_book:,.0f}원 · 환율 {fx:,.1f}")
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
    if problems:
        text = "\n".join([f"🚨 [계좌 체크 · 키움 v2 · {now}]", *[f"- {p}" for p in problems], *info])
    else:
        text = "\n".join([f"✅ [계좌 체크 · 키움 v2 · {now}] 장부·계좌·대시보드 일치", *info])
    print(text)
    if "--no-send" not in sys.argv:
        send(text)


if __name__ == "__main__":
    main()
