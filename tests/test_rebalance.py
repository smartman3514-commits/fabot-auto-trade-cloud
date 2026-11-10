"""연말 비중 맞추기 계획(rebalance.py) — 키움 국내포트 11-10 장부로 손계산과 같은지."""
import fabot_kr as K
import rebalance as R

TR = [{"trade_date": "2026-11-05", "ticker": "TIGER 배당커버드콜액티브(472150)", "action": "buy", "quantity": 546, "price": 8575470 / 546},
      {"trade_date": "2026-11-05", "ticker": "TIGER 미국나스닥100타겟데일리커버드콜(486290)", "action": "buy", "quantity": 693, "price": 6758212 / 693},
      {"trade_date": "2026-11-10", "ticker": "ACE KRX금현물(411060)", "action": "buy", "quantity": 1138, "price": 24750}]
PX = {"TQ": 20000, "QL": 20000, "QQ": 25000, "DIV": 15000, "CCD": 14705, "CCN": 9340, "GOLD": 24750}


def test_kr_plan_matches_hand_calc():
    pos = K.build_ledger({"krw_capital": 500_000_000, "start_date": "2026-10-27", "gold_start": "2026-11-10"}, TR)
    steps, total = R.plan_kr(pos, PX)
    assert round(total) == 499_167_868
    st = {s.sleeve: s for s in steps}
    assert st["CCD"].action == "sell" and 225 <= st["CCD"].qty <= 235      # 약 230주
    assert st["CCN"].action == "sell" and 190 <= st["CCN"].qty <= 200      # 약 196주
    assert st["GOLD"].action == "buy" and 225 <= st["GOLD"].qty <= 235     # 약 232주
    assert st["TQ"].action in ("cash_out", "none") and st["QQ"].action in ("cash_out", "none")
    # 칸별 목표 합 = 전체
    assert abs(sum(s.target for s in steps) - total) < 1


def test_small_drift_does_nothing():
    pos = K.build_ledger({"krw_capital": 500_000_000, "start_date": "2026-10-27"}, [])
    steps, _ = R.plan_kr(pos, PX)
    assert all(s.action == "none" for s in steps)
