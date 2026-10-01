"""crisis_plan.walk()가 백테스트(run2, combo="pace2", fgb=35, W=0.5)와 날짜별 한도가 똑같은지 확인.

fixtures/crisis_parity.csv: 2007-09~2026-09 실제 F&G(2011년 전은 가격 기반 대체지표)와, 그날
백테스트가 계산한 TQQQ 한도(총자산 대비). 규칙을 고쳤는데 이 테스트가 깨지면 백테스트와
실제 규칙이 어긋난 것이다 — 백테스트도 같이 다시 돌려 확인할 것.

실행: python tests/test_crisis_plan.py  (또는 pytest)
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crisis_plan  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "crisis_parity.csv"


def test_matches_backtest_every_day():
    rows = list(csv.DictReader(open(FIXTURE, encoding="utf-8")))
    states = list(crisis_plan.walk([float(r["fg"]) for r in rows]))
    bad = [(r["date"], float(r["limit"]), s.limit) for r, s in zip(rows, states) if abs(float(r["limit"]) - s.limit) > 1e-6]
    assert not bad, f"{len(bad)}일 불일치, 처음 5개: {bad[:5]}"


def test_example_from_design_doc():
    # 설계안 예시: 1월 첫날 33 → 6.25%, 같은 달 24 → 25%(즉시, 대기 기간 무시), 21거래일 뒤 28 → 37.5%
    scores = [60, 33] + [34] * 5 + [24] + [34] * 14 + [28]
    st = list(crisis_plan.walk(scores))
    assert abs(st[1].limit - 0.0625) < 1e-9
    assert abs(st[7].limit - 0.25) < 1e-9 and st[7].deepened
    assert abs(st[22].limit - 0.375) < 1e-9 and st[22].month == 1


def test_resets_at_50():
    st = list(crisis_plan.walk([30, 40, 49, 50, 34]))
    assert st[2].limit > 0 and st[3].limit == 0 and st[3].episode_start is None
    assert abs(st[4].limit - 0.0625) < 1e-9


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"통과: {name}")
