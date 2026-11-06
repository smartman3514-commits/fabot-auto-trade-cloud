"""CAPE 를 multpl 첫 화면(사람이 보는 숫자)과 똑같이 읽고, 종가/장중을 구분해 밝히는지 확인.

2026-11-05: 계좌 체크가 미국장 중간(10:25 ET)에 돌아 장중 값 40.13 을 시각 없이 보여 줬고,
장 마감 뒤 사이트는 종가 38.79 를 보여 줘서 '틀린 숫자' 로 보였다.
실행: python tests/test_cape_site.py  (또는 pytest)
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fabot_v2 as v2  # noqa: E402
import performance  # noqa: E402
import account_check  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "multpl_current_close.html"

INTRADAY = """<div id="current">
<b>Current<span class="currentTitle">
Shiller PE Ratio</span>:</b>
40.13
<span class="neg">
-0.49
(-1.21%)
</span>
<div id="timestamp">
10:25 AM EST, Thu Nov 5
</div>
</div>"""


def test_reads_the_close_exactly_as_the_site_shows():
    c = v2.parse_cape_now(FIX.read_text(encoding="utf-8"))
    assert c == {"value": 38.79, "change": -1.83, "prev_close": 40.62, "stamp": "4:00 PM EST, Thu Nov 5", "closed": True}


def test_intraday_value_is_marked_and_shows_the_previous_close():
    c = v2.parse_cape_now(INTRADAY)
    assert c["closed"] is False and c["value"] == 40.13 and c["prev_close"] == 40.62
    block = {"cape": 40.13, "line": 36.15, "hot": True, "stamp": c["stamp"], "closed": False, "prev_close": 40.62,
             "as_of": date(2026, 11, 5), "mismatch": None}
    text = v2.cape_text(block)
    assert "10:25 AM EST, Thu Nov 5 장중" in text and "직전 종가 40.62" in text and text.endswith("막힘")


def test_close_text_matches_site_number():
    block = {"cape": 38.79, "line": 36.15, "hot": True, "stamp": "4:00 PM EST, Thu Nov 5", "closed": True,
             "prev_close": 40.62, "as_of": date(2026, 11, 5), "mismatch": None}
    assert v2.cape_text(block) == "CAPE 38.79(multpl 4:00 PM EST, Thu Nov 5 종가) / 기준선 36.1 → 막힘"


def test_positive_change_and_missing_change():
    up = INTRADAY.replace('class="neg"', 'class="pos"').replace("-0.49", "+0.49").replace("(-1.21%)", "(+1.21%)")
    assert v2.parse_cape_now(up)["prev_close"] == 39.64
    flat = INTRADAY.replace('<span class="neg">\n-0.49\n(-1.21%)\n</span>', "")
    assert v2.parse_cape_now(flat)["prev_close"] == 40.13


def test_rsi_bar_is_called_intraday_while_the_session_is_open():
    ny = ZoneInfo("America/New_York")
    d = date(2026, 11, 5)
    assert performance.session_open("SCHD", d, datetime(2026, 11, 5, 10, 25, tzinfo=ny)) is True
    assert performance.session_open("SCHD", d, datetime(2026, 11, 5, 16, 1, tzinfo=ny)) is False
    seoul = ZoneInfo("Asia/Seoul")
    assert performance.session_open("458730.KS", d, datetime(2026, 11, 6, 0, 25, tzinfo=seoul)) is False


def test_late_schedule_is_announced(monkeypatch):
    monkeypatch.setenv("EVENT", "schedule")
    monkeypatch.setenv("SCHEDULE", "10 7 * * 1-5")
    note = account_check.late_note(datetime(2026, 11, 5, 15, 25, tzinfo=timezone.utc))
    assert "예정 16:10보다 8시간 15분 늦게" in note
    assert account_check.late_note(datetime(2026, 11, 5, 7, 20, tzinfo=timezone.utc)) == ""
    monkeypatch.setenv("EVENT", "repository_dispatch")
    assert account_check.late_note(datetime(2026, 11, 5, 15, 25, tzinfo=timezone.utc)) == ""


# ── 11-06: CAPE 를 놓치지 않게(다시 시도 · 실러 직접 계산 보험 · 대조) — 인터넷 없이 가짜 값으로 ──
from datetime import date as _date  # noqa: E402

_MULTPL = [(_date(1871 + i // 12, i % 12 + 1, 1), 15.0 * (1.0012 ** i)) for i in range(1858)] + [(_date(2026, 11, 5), 38.79)]
_SHILLER = [(d, v * 0.999, 7722.72) for d, v in _MULTPL[:-1]]


def _patch(monkeypatch, multpl=True, shiller=True, now=38.79, own=38.77):
    def boom(*a, **k):
        raise RuntimeError("막힘")
    monkeypatch.setattr(v2, "fetch_cape_monthly", (lambda: list(_MULTPL)) if multpl else boom)
    monkeypatch.setattr(v2, "fetch_cape_now", (lambda: {"value": now, "change": -1.83, "prev_close": 40.62,
                                                       "stamp": "4:00 PM EST, Thu Nov 5", "closed": True}) if multpl else boom)
    monkeypatch.setattr(v2, "fetch_shiller_monthly", (lambda: list(_SHILLER)) if shiller else boom)
    monkeypatch.setattr(v2, "own_cape", lambda sh: own)


def test_normal_uses_multpl_and_shows_own(monkeypatch):
    _patch(monkeypatch)
    c = v2.cape_block()
    assert c["source"] == "multpl" and c["cape"] == 38.79 and c["own"] == 38.77 and c["mismatch"] is None
    assert "직접 계산 38.77" in v2.cape_text(c)


def test_multpl_down_falls_back_to_own(monkeypatch):
    _patch(monkeypatch, multpl=False)
    c = v2.cape_block()
    assert c["cape"] == 38.77 and c["source"] != "multpl" and c["error"] is None and c["line"] > 0
    assert "직접 계산 값으로 판정" in c["mismatch"]


def test_both_down_blocks(monkeypatch):
    _patch(monkeypatch, multpl=False, shiller=False)
    c = v2.cape_block()
    assert c["hot"] is True and c["cape"] is None and "막는 쪽" in c["error"]


def test_big_gap_is_flagged(monkeypatch):
    _patch(monkeypatch, own=35.0)
    assert "2% 넘게 다름" in v2.cape_block()["mismatch"]


def test_retry_then_success(monkeypatch):
    n = {"k": 0}

    class R:
        status_code = 200
        def raise_for_status(self):
            pass

    def get(url, **k):
        n["k"] += 1
        if n["k"] < 3:
            raise v2.requests.Timeout("시간 초과")
        return R()
    monkeypatch.setattr(v2.requests, "get", get)
    monkeypatch.setattr(v2.time, "sleep", lambda s: None)
    assert v2._get("http://x").status_code == 200 and n["k"] == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
