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


# ── 승인 · 장부 옮김 · 실행일 (2026-11-10) ──────────────────────────────────
from datetime import date

CFG = {"krw_capital": 500_000_000, "start_date": "2026-10-27", "gold_start": "2026-11-10",
       "absorb_into_gold": {"CCD": 3925470, "CCN": 2108212}}


def _plan(tmp_path, approved):
    pos = K.build_ledger(CFG, TR)
    steps, _ = R.plan_kr(pos, PX)
    p = R.write_plan("kr", K.ACCOUNT, {"KRW": {"date": "2026-12-30", "steps": steps}}, PX, None, 2026, plan_dir=tmp_path)
    if approved:
        import json
        d = json.loads(p.read_text(encoding="utf-8")); d["approved"] = True; p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return steps


def test_ledger_cash_equals_account_cash():
    pos = K.build_ledger(CFG, TR)
    assert round(sum(p.cash for p in pos.values())) == 500_000_000 - 8575470 - 6758212 - 1138 * 24750


def test_unapproved_plan_never_runs_or_moves(tmp_path):
    _plan(tmp_path, approved=False)
    assert R.todays_orders("kr", "KRW", date(2026, 12, 30), plan_dir=tmp_path) is None
    pos = K.build_ledger(CFG, TR); before = {k: p.cash for k, p in pos.items()}
    R.apply_moves(pos, "kr", date(2027, 1, 5), plan_dir=tmp_path)
    assert {k: p.cash for k, p in pos.items()} == before


def test_approved_plan_runs_only_on_its_day(tmp_path):
    _plan(tmp_path, approved=True)
    assert R.todays_orders("kr", "KRW", date(2026, 12, 29), plan_dir=tmp_path) is None
    acts = R.todays_orders("kr", "KRW", date(2026, 12, 30), plan_dir=tmp_path)
    assert acts and acts[0]["action"] == "sell" and acts[-1]["action"] == "buy"   # 파는 것 먼저


def test_after_rebalance_each_sleeve_near_target(tmp_path):
    steps = _plan(tmp_path, approved=True)
    st = {s.sleeve: s for s in steps}
    done = TR + [{"trade_date": "2026-12-30", "ticker": K.SLEEVES[[s.key for s in K.SLEEVES].index(k)].ledger,
                  "action": st[k].action, "quantity": st[k].qty, "price": PX[k]} for k in st if st[k].action in ("sell", "buy")]
    pos = K.build_ledger(CFG, done)
    R.apply_moves(pos, "kr", date(2026, 12, 30), plan_dir=tmp_path)
    total = sum(p.cash + p.qty * PX[k] for k, p in pos.items())
    for k, p in pos.items():
        w = next(s.weight for s in K.SLEEVES if s.key == k)
        assert abs((p.cash + p.qty * PX[k]) - total * w) < PX[k] + 1        # 1주 값 안쪽으로 목표에 맞음
    assert all(p.cash >= -1 for p in pos.values())                          # 어느 칸도 현금이 음수가 아님


# ── 한 번 승인(standing, 2026-11-10 인선: "한 번 승인하면 모든 것이 승인되도록") ─────────────────
def _cfg(approved, required=False):
    return {**CFG, "approval": {"required": required, "approved": approved}}


def test_standing_runs_rebalance_without_asking(tmp_path):
    pos = K.build_ledger(CFG, TR)
    acts = R.orders_for_today("kr", K.ACCOUNT, "KRW", date(2026, 12, 30), _cfg(True), pos, PX, plan_dir=tmp_path)
    assert acts and acts[0]["action"] == "sell"
    import json
    plan = json.loads((tmp_path / "kr_2026.json").read_text(encoding="utf-8"))
    assert plan["approved"] is True and "한 번 승인" in plan["approved_note"]


def test_no_standing_no_plan_means_no_rebalance(tmp_path):
    pos = K.build_ledger(CFG, TR)
    assert R.orders_for_today("kr", K.ACCOUNT, "KRW", date(2026, 12, 30), _cfg(False), pos, PX, plan_dir=tmp_path) is None
    assert not (tmp_path / "kr_2026.json").exists()


def test_standing_does_nothing_on_other_days(tmp_path):
    pos = K.build_ledger(CFG, TR)
    assert R.orders_for_today("kr", K.ACCOUNT, "KRW", date(2026, 12, 29), _cfg(True), pos, PX, plan_dir=tmp_path) is None


def test_blocked_only_when_required_and_not_approved():
    assert R.blocked(_cfg(False, required=True)) is True
    assert R.blocked(_cfg(True, required=True)) is False
    assert R.blocked(_cfg(False, required=False)) is False    # 모의 계좌: 지금처럼 매매
