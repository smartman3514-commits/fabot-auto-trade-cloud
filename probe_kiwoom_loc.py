"""키움 모의(해외)에서 LOC 주문(trde_tp=30)이 접수되는지 시험한다 — 2026-10-13 인선 허락.

SOXL 1주를 현재가의 절반 가격으로 LOC 매수 → 접수(주문번호) 확인 → 미체결 목록 확인 → 바로 취소 → 취소 확인.
체결될 수 없는 가격이라 돈은 나가지 않는다. 미국 정규장(22:30~05:00 KST, 서머타임)에만 의미가 있다.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import kiwoom_client as K

KST = timezone(timedelta(hours=9))
T, EX = "SOXL", "NY"


def show(label, obj):
    print(f"{label}: {json.dumps(obj, ensure_ascii=False)[:600]}", flush=True)


def main():
    print("시각", datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"), flush=True)
    q = K.get_overseas_quote(T, exchange=EX, mode="demo")
    px = K.abs_price(q["cur_prc"])
    book = K.get_overseas_orderbook(T, exchange=EX, mode="demo")
    ask = abs(float(book.get("sel_1bid", "0") or "0"))
    print(f"SOXL 현재가 ${px} · 매도1호가 ${ask}", flush=True)
    if ask <= 0:
        print("호가가 0 — 정규장이 아님. 시험 중단.")
        return
    limit = round(px * 0.5, 2)
    results = {}
    for tp, name in (("30", "LOC"), ):
        try:
            r = K.place_overseas_order(T, "buy", 1, price=limit, exchange=EX, trde_tp=tp, mode="demo")
            show(f"[{name}] 주문 응답", r)
        except Exception as exc:
            print(f"[{name}] 주문 실패: {exc}", flush=True)
            results[name] = f"실패: {exc}"
            continue
        ord_no = r.get("ord_no")
        results[name] = f"접수 {ord_no}" if r.get("return_code") == 0 else f"거부 {r.get('return_msg')}"
        time.sleep(4)
        try:
            u = K.get_overseas_unfilled_orders(stk_cd=T, stex_tp=EX, mode="demo")
            show(f"[{name}] 미체결 목록", u)
        except Exception as exc:
            print(f"[{name}] 미체결 조회 실패: {exc}", flush=True)
        if ord_no:
            try:
                c = K.cancel_overseas_order(ord_no, T, exchange=EX, mode="demo")
                show(f"[{name}] 취소 응답", c)
            except Exception as exc:
                print(f"[{name}] 취소 실패 — 직접 확인 필요: {exc}", flush=True)
            time.sleep(4)
            try:
                u = K.get_overseas_unfilled_orders(stk_cd=T, stex_tp=EX, mode="demo")
                show(f"[{name}] 취소 뒤 미체결 목록", u)
            except Exception as exc:
                print(f"[{name}] 취소 뒤 미체결 조회 실패: {exc}", flush=True)
    print("결과:", results, flush=True)


if __name__ == "__main__":
    main()
