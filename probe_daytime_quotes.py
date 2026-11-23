"""미국주식 '주간거래'(한국 낮 = 미국 밤) 시세를 한투·키움 모의 API가 주는지 확인한다 — 조회만, 주문 없음.

2026-11-23 인선: FA투자여정 '내 운용'에 인베스팅닷컴·영웅문처럼 장 밖 시세(주간거래)를 보여 주고 싶다.
- 한투: 해외주식 현재체결가(HHDFS00000300)의 EXCD=BAQ(나스닥 주간) vs NAS(정규)
- 키움: usa20100(현재가)·usa20101(10호가, 시각 포함) — 응답 칸 이름까지 출력해 주간 시세 칸이 있는지 본다
가격·시각·칸 이름만 출력한다(키·토큰은 출력하지 않는다).
"""
import json
from datetime import datetime, timezone, timedelta

import kis_price_client as kis
import kiwoom_client as kw

KST = timezone(timedelta(hours=9))
print("지금(KST):", datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))

print("\n[한투] HHDFS00000300 TQQQ")
for excd in ("NAS", "BAQ"):
    try:
        kis._throttle()
        r = kis._get_with_retry(f"{kis.BASE_URL}/uapi/overseas-price/v1/quotations/price",
                                kis._headers("HHDFS00000300"), {"AUTH": "", "EXCD": excd, "SYMB": "TQQQ"})
        d = r.json()
        o = d.get("output") or {}
        print(f"  EXCD={excd}: rt_cd={d.get('rt_cd')} msg={d.get('msg1', '').strip()} | last={o.get('last')} base={o.get('base')} diff={o.get('diff')} rate={o.get('rate')} tvol={o.get('tvol')} ordy={o.get('ordy')}")
    except Exception as e:  # noqa: BLE001
        print(f"  EXCD={excd}: 실패 {type(e).__name__}: {str(e)[:200]}")

print("\n[키움] TQQQ (모의 해외)")
for name, fn in (("usa20100 현재가", kw.get_overseas_quote), ("usa20101 10호가", kw.get_overseas_orderbook)):
    try:
        q = fn("TQQQ")
        keep = {k: v for k, v in q.items() if not isinstance(v, (list, dict)) and k not in ("return_msg",)}
        print(f"  {name}: return_code={q.get('return_code')} msg={q.get('return_msg')}")
        print("   ", json.dumps(keep, ensure_ascii=False)[:900])
    except Exception as e:  # noqa: BLE001
        print(f"  {name}: 실패 {type(e).__name__}: {str(e)[:200]}")
