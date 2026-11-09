"""키움 모의(국내) 계좌로 KRX 금현물 시세를 받을 수 있는지 확인하는 1회성 조회 — 주문은 내지 않는다.

확인할 것: 국내 모의 키로 금현물 종목코드의 기본정보(ka10001)·호가(ka10004)가 오는지.
- M04020000: 금 99.99_1kg (1g 단위 거래)
- M04020100: 미니금 99.99_100g
응답에서는 결과 코드·메시지·이름·가격 관련 필드와 필드 이름 목록만 출력한다(계좌·비밀값 없음).
"""
import json

from kiwoom_client import _post_readonly

CODES = {"M04020000": "금 99.99_1kg", "M04020100": "미니금 99.99_100g"}
CALLS = [("ka10001", "/api/dostk/stkinfo", "기본정보"), ("ka10004", "/api/dostk/mrkcond", "호가")]
SHOW = ("return_code", "return_msg", "stk_cd", "stk_nm", "cur_prc", "base_pric", "pred_pre", "flu_rt", "trde_qty",
        "upl_pric", "lst_pric", "sel_fpr_bid", "buy_fpr_bid", "bid_req_base_tm")

for code, name in CODES.items():
    for api_id, path, label in CALLS:
        print(f"\n== {code} {name} · {label}({api_id}) ==")
        try:
            r = _post_readonly("demo", "domestic", api_id, path, {"stk_cd": code})
        except Exception as e:  # 실패해도 다음 조회는 계속
            print("  실패:", str(e)[:300])
            continue
        print("  ", json.dumps({k: r.get(k) for k in SHOW if k in r}, ensure_ascii=False))
        print("   필드 이름:", ", ".join(sorted(r.keys()))[:600])
