"""미국 조기 폐장일엔 실행 창이 13:00 뉴욕(겨울 03:00 KST) 앞뒤로 옮겨 가야 한다 — 2026-11-26 인선."""
from datetime import datetime, timezone

import auto_trade_loop as kis
import auto_trade_loop_kiwoom as kiw


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_early_close_window_moves_to_0300_kst():
    for m in (kis, kiw):
        assert m._within_tqqq_close_window(utc(2026, 11, 27, 17, 45))       # 11-28 02:45 KST — 13:00 뉴욕 15분 전
        assert m._within_tqqq_close_window(utc(2026, 11, 27, 18, 5))        # 03:05 KST — 마감 5분 뒤
        assert not m._within_tqqq_close_window(utc(2026, 11, 27, 20, 45))   # 평소 창(05:45 KST)은 그날 닫힘
        assert m._within_tqqq_close_window(utc(2026, 12, 24, 17, 45))       # 성탄절 전날


def test_normal_day_unchanged():
    for m in (kis, kiw):
        assert m._within_tqqq_close_window(utc(2026, 11, 25, 20, 45))       # 11-26 05:45 KST, 16:00 뉴욕 15분 전
        assert not m._within_tqqq_close_window(utc(2026, 11, 25, 17, 45))   # 평일 02:45 KST 는 건너뜀
