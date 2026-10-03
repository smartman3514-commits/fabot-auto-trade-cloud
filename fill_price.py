"""매매기록에 남길 "실제 체결가"를 증권사 체결내역에서 조회한다(2026-10-03).

왜 필요한가: 예전엔 체결 뒤 보유내역의 평균단가(pchs_avg_pric / pur_pric)를 그대로 매수
가격으로 기록했다. 그건 "이번 매수의 체결가"가 아니라 "계좌 전체 평균단가"라서, 예를 들어
KIS 08-13 TIGER 배당커버드콜액티브는 실제 20,484원에 샀는데 21,174.536원으로 기록됐고,
KIS 10-01 TQQQ는 실제 $79.06인데 $71.489(그 전 평균)로 기록됐다 — KIS 모의 해외는
평균단가가 당일엔 갱신도 안 된다. 현금 역산·수익 계산이 다 틀어진다.

이 모듈은 이번 실행에서 낸 주문번호들의 체결 수량×체결가를 가중평균해서 돌려준다.
조회가 실패하면 None을 돌려주고, 호출하는 쪽은 예전처럼 평균단가로 기록한다 — 체결가
조회 실패 때문에 매매기록 자체를 못 남기는 일은 없어야 한다(2026-09-18 stex_tp 사고 교훈).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _norm(n) -> str:
    return str(n or "").strip().lstrip("0")


def _weighted(rows: list[tuple[float, float]]) -> float | None:
    q = sum(r[0] for r in rows)
    return sum(r[0] * r[1] for r in rows) / q if q > 0 else None


def kis_domestic(pdno: str, order_nos: list) -> float | None:
    import live_order_executor as L
    want = {_norm(n) for n in order_nos}
    today = datetime.now(KST).strftime("%Y%m%d")
    params = {
        "CANO": L._cano(), "ACNT_PRDT_CD": L.ACNT_PRDT_CD, "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "PDNO": pdno, "CCLD_DVSN": "01", "INQR_DVSN": "00", "INQR_DVSN_3": "00",
        "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "EXCG_ID_DVSN_CD": "KRX",
    }
    L._throttle()
    r = L._get_with_retry(f"{L.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                          headers={**L._headers("VTTC0081R"), "tr_cont": ""}, params=params)
    rows = [(float(x["tot_ccld_qty"]), float(x["avg_prvs"])) for x in r.json().get("output1", [])
            if _norm(x.get("odno")) in want and float(x.get("tot_ccld_qty") or 0) > 0]
    return _weighted(rows)


def kis_overseas(pdno: str, excg: str, order_nos: list) -> float | None:
    import overseas_order_executor as O
    want = {_norm(n) for n in order_nos}
    now = datetime.now(KST)
    params = {
        "CANO": O._cano(), "ACNT_PRDT_CD": O.ACNT_PRDT_CD, "PDNO": pdno,
        "ORD_STRT_DT": (now - timedelta(days=2)).strftime("%Y%m%d"), "ORD_END_DT": now.strftime("%Y%m%d"),
        "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "01", "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
        "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "", "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
    }
    O._throttle()
    r = O._get_with_retry(f"{O.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-ccnl",
                          headers={**O._headers("VTTS3035R"), "tr_cont": ""}, params=params)
    rows = [(float(x["ft_ccld_qty"]), float(x["ft_ccld_unpr3"])) for x in r.json().get("output", [])
            if _norm(x.get("odno")) in want and float(x.get("ft_ccld_qty") or 0) > 0]
    return _weighted(rows)


def kiwoom_domestic(stk_cd: str, order_nos: list) -> float | None:
    import kiwoom_client as K
    want = {_norm(n) for n in order_nos}
    d = K._post_readonly("demo", "domestic", "kt00007", "/api/dostk/acnt", {
        "ord_dt": datetime.now(KST).strftime("%Y%m%d"), "qry_tp": "1", "stk_bond_tp": "1", "sell_tp": "0",
        "stk_cd": stk_cd, "fr_ord_no": "", "dmst_stex_tp": "%"})
    rows = [(float(x["cntr_qty"]), float(x["cntr_uv"])) for x in d.get("acnt_ord_cntr_prps_dtl", [])
            if _norm(x.get("ord_no")) in want and float(x.get("cntr_qty") or 0) > 0]
    return _weighted(rows)


def from_holding_delta(before: dict | None, after: dict | None, filled_qty: float) -> float | None:
    """체결내역 API가 없는 경로(키움 해외)용: 매수 전후 평균단가×수량 차이로 이번 체결가를 역산."""
    if not after or filled_qty <= 0:
        return None
    b_cost = (before["avg_price"] * before["qty"]) if before else 0.0
    price = (after["avg_price"] * after["qty"] - b_cost) / filled_qty
    return price if price > 0 else None


def safe(fn, *args) -> float | None:
    try:
        return fn(*args)
    except Exception as exc:
        print(f"  (체결가 조회 실패 — 평균단가로 기록합니다: {exc})")
        return None
