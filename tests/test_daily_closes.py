"""야후 일봉 마지막 칸이 비었을 때(장 마감 뒤에도 None) meta 종가로 채우는지, 장중에는 채우지 않는지 확인.
2026-11-05: 장 마감 13시간 뒤에도 SCHD·TQQQ 11-05 칸이 None 이라 RSI 가 11-04 기준으로 계산됐다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402

D4, D5 = 1793802600, 1793889000          # 2026-11-04·05 14:30 UTC (일봉 칸 시각)
END5 = 1793912400                        # 2026-11-05 21:00 UTC (뉴욕 16:00, 장 끝)


def chart(last_close, market_time, price=35.42):
    return {"timestamp": [D4, D5], "indicators": {"quote": [{"close": [36.21, last_close]}]},
            "meta": {"regularMarketPrice": price, "regularMarketTime": market_time,
                     "currentTradingPeriod": {"regular": {"start": D5, "end": END5}}}}


def test_empty_last_bar_after_the_close_takes_the_market_close():
    out = v2.closes_from_chart(chart(None, END5))
    assert [(d.isoformat(), c) for d, c in out] == [("2026-11-04", 36.21), ("2026-11-05", 35.42)]


def test_empty_last_bar_during_the_session_is_left_out():
    out = v2.closes_from_chart(chart(None, END5 - 3600))     # 장 끝 1시간 전
    assert [d.isoformat() for d, _ in out] == ["2026-11-04"]


def test_filled_last_bar_is_kept_as_is():
    out = v2.closes_from_chart(chart(35.40, END5))
    assert out[-1][1] == 35.40 and len(out) == 2


def test_period_of_another_day_does_not_fill():
    c = chart(None, END5 + 86400)
    c["meta"]["currentTradingPeriod"]["regular"] = {"start": D5 + 86400, "end": END5 + 86400}   # 다음 날 장
    assert [d.isoformat() for d, _ in v2.closes_from_chart(c)] == ["2026-11-04"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
