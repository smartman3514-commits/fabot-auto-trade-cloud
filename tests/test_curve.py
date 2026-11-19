"""FA곡선 지수 0.8 — 2026-11-19 인선 결정(이전 11-17 직선 1.0). 엔진 fa-journey PR #32 와 같게."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402


def test_curve_pow_is_08():
    assert v2.CURVE_POW == 0.8


def test_half_fear_target_about_66_percent():
    t = v2.CURVE_BASE + (1 - v2.CURVE_BASE) * 0.5 ** v2.CURVE_POW
    assert abs(t - (0.2 + 0.8 * 0.5 ** 0.8)) < 1e-9 and 0.65 < t < 0.67   # 직선(1.0)이면 60%


def test_extremes_unchanged():
    f = lambda s: v2.CURVE_BASE + (1 - v2.CURVE_BASE) * s ** v2.CURVE_POW
    assert f(0.0) == 0.2 and abs(f(1.0) - 1.0) < 1e-12
