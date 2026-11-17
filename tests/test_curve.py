"""FA곡선 직선(지수 1.0) — 2026-11-17 인선 결정. 엔진 fa-journey PR #22 와 같게."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402


def test_curve_is_linear():
    assert v2.CURVE_POW == 1.0


def test_half_fear_targets_sixty_percent():
    t = v2.CURVE_BASE + (1 - v2.CURVE_BASE) * 0.5 ** v2.CURVE_POW
    assert abs(t - 0.60) < 1e-9          # 제곱일 때는 40%
