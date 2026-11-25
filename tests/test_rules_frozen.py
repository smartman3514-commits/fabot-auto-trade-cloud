"""규칙 얼리기 (2026-11-25 인선, 비판 4번: 11월에 규칙을 너무 자주 바꿈 → 과거 맞추기 위험).

FA투자여정(fa-journey tests/test_rules_frozen.py)과 같은 값. 다음 정기 점검(2027-01-02) 전까지 모의 자동매매의 매매 규칙 숫자를 바꾸지 않는다.
꼭 바꿔야 하면 인선 승인 뒤 FA여정 엔진·이 파일·FA여정 시험을 같이 고치고, 이유를 회의록에 남긴다.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fabot_v2 as v2  # noqa: E402

FROZEN_UNTIL = date(2027, 1, 2)
FROZEN = {
    "CAPE_K": 1.5,
    "CURVE_BASE": 0.20, "CURVE_POW": 0.8,
    "RSI_ACCEL_TH": 30, "RSI_ACCEL_ADD": 0.40,
    "AB_BEAR_DD": 0.30, "AB_REFILL_DAYS": 378,
    "SELL_HALF": 71, "SELL_ALL": 78, "SELL_GAP_DAYS": 40,
    "DIP_FG": 10, "DIP_FG_PANIC": 10,
    "DIP_FRAC": 0.04, "DIP_FRAC_PANIC": 0.08, "DIP_BUDGET": 0.20,
    "DIP_NDX_DD": 0.07, "DIP_GAP_DAYS": 5, "DIP_RELEASE": 0.10, "DIP_NDX_DAYS": 756,
    "GOLD_RSI_MAX": 60, "GOLD_SLOTS": 6,
    "PEACE_HI": 50, "PEACE_FRAC": 0.10,
}


def test_rules_frozen_until_next_check():
    if date.today() >= FROZEN_UNTIL:
        return   # 정기 점검 뒤에는 새 값으로 이 파일을 갱신
    changed = [f"{k}: 얼린 값 {want} → 지금 {getattr(v2, k)}" for k, want in FROZEN.items() if getattr(v2, k) != want]
    assert not changed, "2027-01-02 정기 점검 전까지 규칙을 얼렸어요(11-25 인선): " + "; ".join(changed)
