"""v2 시작 전 소액 시험 주문(2026-10-15 인선 허락): 처음 쓰는 종목을 1주 사고 바로 판다.

미국: QQQ(ND)·버크셔 B주(BRKb, NY)·QLD(NY) / 국내: TIGER 미국나스닥100타겟데일리커버드콜(486290).
v2 장부에 섞이지 않게 매매기록은 account='키움 v2 시험'으로 남긴다. 시험이 끝나면 이 파일과
워크플로(v2-order-test.yml)는 지운다.

    python v2_order_test.py --market us
    python v2_order_test.py --market kr
"""
import argparse
import os
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8")

import kiwoom_client as K
from auto_trade_loop_kiwoom import KiwoomChaseOrder, KiwoomOverseasChaseOrder, _overseas_holding
from cooldown import log_trade

ACCOUNT = "키움 v2 시험"
US = [("QQQ", "ND"), ("BRKb", "NY"), ("QLD", "NY")]
KR = ["486290"]
lines: list[str] = []


def note(msg: str) -> None:
    print(msg)
    lines.append(msg)


def test_us() -> None:
    for t, ex in US:
        try:
            book = K.get_overseas_orderbook(t, exchange=ex, mode="demo")
            ask = abs(float(book.get("sel_1bid", "0") or "0"))
            bid = abs(float(book.get("buy_1bid", "0") or "0"))
            if ask <= 0:
                note(f"{t}: 호가 0 — 미국 정규장이 아니라 건너뜀")
                continue
            before = _overseas_holding(t, ex)
            b = KiwoomOverseasChaseOrder("buy", 1, max_reprices=10, max_seconds=120.0, poll_interval=3.0,
                                         qty_before=before["qty"] if before else 0, ticker=t, exchange=ex)
            b.run()
            if b.filled_qty < 1:
                note(f"{t}: 매수 1주 체결 안 됨 (주문 {getattr(b, 'order_nos', [])})")
                continue
            log_trade(ticker=t, action="buy", quantity=1, price=b.order["price"], memo="v2 시험 주문", account=ACCOUNT)
            mid = _overseas_holding(t, ex)
            s = KiwoomOverseasChaseOrder("sell", 1, max_reprices=10, max_seconds=120.0, poll_interval=3.0,
                                         qty_before=mid["qty"] if mid else 1, ticker=t, exchange=ex)
            s.run()
            if s.filled_qty >= 1:
                log_trade(ticker=t, action="sell", quantity=1, price=s.order["price"], memo="v2 시험 주문", account=ACCOUNT)
            note(f"{t}: 매수 1주 @ ${b.order['price']} 체결 → 매도 {'체결' if s.filled_qty >= 1 else '안 됨(1주 남음)'}"
                 + (f" @ ${s.order['price']}" if s.filled_qty >= 1 else "") + f" (호가 ${bid}/${ask})")
        except Exception as exc:  # noqa: BLE001
            note(f"{t}: 오류 — {exc}")


def test_kr() -> None:
    for code in KR:
        try:
            book = K.get_domestic_orderbook(code, mode="demo")
            ask = int(K.abs_price(book.get("sel_fpr_bid", "0") or "0"))
            if ask <= 0:
                note(f"{code}: 호가 0 — 국내 정규장이 아니라 건너뜀")
                continue
            b = KiwoomChaseOrder(code, "buy", 1, max_reprices=20, max_seconds=180.0, poll_interval=3.0)
            b.run()
            if b.filled_qty < 1:
                note(f"{code}: 매수 1주 체결 안 됨")
                continue
            log_trade(ticker=code, action="buy", quantity=1, price=b.order["price"], memo="v2 시험 주문", account=ACCOUNT)
            s = KiwoomChaseOrder(code, "sell", 1, max_reprices=20, max_seconds=180.0, poll_interval=3.0)
            s.run()
            if s.filled_qty >= 1:
                log_trade(ticker=code, action="sell", quantity=1, price=s.order["price"], memo="v2 시험 주문", account=ACCOUNT)
            note(f"{code}: 매수 1주 @ {b.order['price']:,}원 체결 → 매도 {'체결' if s.filled_qty >= 1 else '안 됨(1주 남음)'}")
        except Exception as exc:  # noqa: BLE001
            note(f"{code}: 오류 — {exc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["us", "kr"], required=True)
    args = ap.parse_args()
    test_us() if args.market == "us" else test_kr()
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        text = f"[v2 시험 주문 · {'미국' if args.market == 'us' else '국내'}]\n" + "\n".join(lines)
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": text}, timeout=15)


if __name__ == "__main__":
    main()
