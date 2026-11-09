"""키움 모의(국내) 계좌가 KRX 금현물 주문(kt50000 매수)을 받는지 확인하는 1회성 시험. 체결되면 안 된다.

    python probe_kiwoom_gold_order.py --mode reject   # 하한가보다 낮은 값 → 무조건 거절. 거절 메시지로 '금현물 주문 TR 지원 여부'를 본다(장 끝난 뒤에도 됨)
    python probe_kiwoom_gold_order.py --mode test     # 장중: 1g 을 현재가 −8%(하한가 위)로 매수 접수 → 바로 취소(kt50003) → 결과 출력

- 종목: 금 99.99_1kg(M04020000, 1g 단위). 수량은 늘 1g.
- 응답은 결과 코드·메시지·주문번호만 출력한다(계좌·비밀값 없음).
- 취소가 실패해도 −8% 지정가라 그날 체결될 가능성은 낮고, 당일 주문이라 장이 끝나면 사라진다 — 그래도 실패하면 바로 사람이 확인한다.
"""
import argparse
import ast
import sys

import requests

from kiwoom_client import _post, _post_readonly, abs_price

CODE = "M04020000"
TICK = 10


def call(api_id: str, body: dict) -> dict:
    """주문 TR 호출. kiwoom_client._post 는 실패 응답에서 예외를 내므로, 여기서 받아 결과 코드·메시지로 돌려준다(재시도 없음)."""
    try:
        return _post("demo", "domestic", api_id, "/api/dostk/ordr", body, retries=0)
    except requests.exceptions.HTTPError as e:
        return {"return_code": f"HTTP {e.response.status_code}", "return_msg": e.response.text[:300]}
    except RuntimeError as e:
        txt = str(e)
        try:
            d = ast.literal_eval(txt[txt.index("{"):])
            return {"return_code": d.get("return_code"), "return_msg": d.get("return_msg")}
        except Exception:
            return {"return_code": "?", "return_msg": txt[:300]}


def order_buy(price: int, trde_tp: str) -> dict:
    return call("kt50000", {"stk_cd": CODE, "ord_qty": "1", "ord_uv": str(price), "trde_tp": trde_tp})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["reject", "test"], required=True)
    a = ap.parse_args()
    q = _post_readonly("demo", "domestic", "ka10001", "/api/dostk/stkinfo", {"stk_cd": CODE})
    cur, low = abs_price(q["cur_prc"]), abs_price(q["lst_pric"])
    print(f"시세: 현재가 {cur:,.0f}원 · 하한가 {low:,.0f}원")
    if a.mode == "reject":
        price = int(low * 0.95 // TICK * TICK)
        print(f"[거절 시험] 하한가 아래 {price:,}원으로 1g 매수 — 거절되어야 정상")
    else:
        price = int(max(low + TICK, cur * 0.92) // TICK * TICK)
        print(f"[접수·취소 시험] 현재가 −8% 쯤 {price:,}원으로 1g 매수")
    r = None
    for tp in ("00", "0"):          # 매매구분 값 형식을 모르니 두 가지를 차례로(첫 응답이 매매구분 오류일 때만 다음)
        r = order_buy(price, tp)
        print(f"  trde_tp={tp!r} → return_code={r.get('return_code')} · {r.get('return_msg')} · ord_no={r.get('ord_no')}")
        if not (r.get("return_code") not in (0, "0") and "매매구분" in str(r.get("return_msg", ""))):
            break
    if a.mode == "reject":
        if r.get("return_code") in (0, "0"):
            print("!! 예상과 달리 접수됨 — 바로 취소 시도")
        else:
            return
    if r.get("return_code") not in (0, "0"):
        print("주문 접수 실패 — 취소할 것 없음"); sys.exit(1)
    ord_no = r["ord_no"]
    c = call("kt50003", {"orig_ord_no": ord_no, "stk_cd": CODE, "cncl_qty": "0"})
    print(f"  취소(kt50003) → return_code={c.get('return_code')} · {c.get('return_msg')}")
    if c.get("return_code") not in (0, "0"):
        print(f"!! 취소 실패 — 주문번호 {ord_no} 를 영웅문/앱에서 직접 취소할 것"); sys.exit(1)


if __name__ == "__main__":
    main()
