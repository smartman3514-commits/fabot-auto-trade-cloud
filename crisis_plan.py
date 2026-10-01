"""새 위기 매수 규칙(설계안 2026-10-02) — "위기 때 총자산 최대 50%, F&G 35부터, 몇 달에 나눠 사기".

지금은 **그림자 운영 전용**이다: 실제 주문은 넣지 않고, TQQQ 마감 실행 때 "새 규칙이었다면
오늘 얼마를 더 샀을지"만 텔레그램에 한 줄로 보여 준다(4주 그림자 운영 뒤 실제 적용 여부 결정).

규칙(백테스트 run2(combo="pace2", fgb=35, W=0.5)와 한 줄씩 같은 순서로 계산한다):
  - F&G <= 35 가 되면 공포 구간 시작, F&G >= 50 이면 구간 종료(한도 0으로 초기화).
  - 공포 구간 안에서 F&G <= 35 인 날마다:
      그날 단계 = 25%(<=25) / 12.5%(<=30) / 6.25%(<=35) 의 "총자산 대비 한도 증가분"
      (W=0.5 × 0.5 / 0.25 / 0.125)
      * 구간 시작 후 21거래일 단위로 "몇 번째 달"을 센다. 새 달의 첫날이면 한도 += 그날 단계.
      * 같은 달 안에서 그달 단계보다 더 깊어지면 차액만큼 한도 += , 이날은 쿨다운 무시(deepened).
      * 한도는 최대 50%.
  - 그날 살 금액 = max(0, 한도 × 총자산 − 지금 TQQQ 평가액), 실제로는 달러 예수금 안에서만.

상태는 저장하지 않는다 — 매번 CNN F&G 일별 기록을 처음부터 다시 훑어서 계산한다. 실행이
하루 빠지거나 서버가 재시작돼도 상태가 꼬이지 않게 하기 위함이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

START_FG = 35      # 공포 구간 시작(이하)
END_FG = 50        # 공포 구간 종료(이상)
MAX_LIMIT = 0.5    # 총자산 대비 TQQQ 한도 최대치
MONTH_DAYS = 21    # "한 달" = 21거래일

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2020-09-18"
CNN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}
FALLBACK_USDKRW = 1380.3  # 대시보드 CASH_RECONSTRUCTION과 같은 근사치(환율 조회 실패 시에만)


def step_for(score: float, start: float = START_FG, w: float = MAX_LIMIT) -> float:
    """그날 F&G로 정해지는 한도 증가 단계(총자산 대비)."""
    if score <= start - 10:
        return w * 0.5
    if score <= start - 5:
        return w * 0.25
    return w * 0.125


@dataclass
class PlanState:
    limit: float = 0.0               # 총자산 대비 TQQQ 한도
    episode_start: int | None = None  # 공포 구간 시작 인덱스
    month: int = -1                  # 구간 시작 후 몇 번째 달(0부터)
    month_step: float = 0.0          # 그달에 이미 반영한 단계
    deepened: bool = False           # 오늘 더 깊은 단계로 내려갔나(쿨다운 무시)
    in_zone: bool = False            # 오늘 F&G가 시작 기준 이하인가


def walk(scores, start: float = START_FG, w: float = MAX_LIMIT):
    """일별 F&G를 순서대로 받아 매일의 PlanState를 돌려준다(백테스트 run2와 같은 순서)."""
    st = PlanState()
    for i, v in enumerate(scores):
        st.deepened = False
        if v >= END_FG:
            st.episode_start = None; st.limit = 0.0; st.month = -1; st.month_step = 0.0
        if v <= start and st.episode_start is None:
            st.episode_start = i
        st.in_zone = st.episode_start is not None and v <= start
        if st.in_zone:
            cur = step_for(v, start, w) / w   # 0.5 / 0.25 / 0.125 (run2의 cur)
            m = (i - st.episode_start) // MONTH_DAYS
            if m != st.month:
                st.limit = min(w, st.limit + w * cur); st.month_step = cur; st.month = m
            elif cur > st.month_step:
                st.limit = min(w, st.limit + w * (cur - st.month_step)); st.month_step = cur; st.deepened = True
        yield PlanState(**vars(st))


def fetch_cnn_history(timeout: int = 20) -> list[tuple[date, float]]:
    """CNN 일별 F&G 기록(2020-09-18~) + 지금 실시간 값. 실제 매매 신호와 같은 출처."""
    resp = requests.get(CNN_URL, headers=CNN_HEADERS, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    daily: dict[date, float] = {}
    for p in body["fear_and_greed_historical"]["data"]:
        d = datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).date()
        daily[d] = float(p["y"])
    now = body["fear_and_greed"]
    daily[datetime.fromisoformat(now["timestamp"]).date()] = float(now["score"])
    return sorted(daily.items())


def fetch_usdkrw(timeout: int = 10) -> float:
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=5d&interval=1d",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        r.raise_for_status()
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        return float(closes[-1])
    except Exception:
        return FALLBACK_USDKRW


def shadow_line(total_krw: float, tqqq_krw: float, cash_krw: float, usd_cash_usd: float,
                history: list[tuple[date, float]] | None = None, usdkrw: float | None = None) -> str:
    """텔레그램용 한 줄: 새 규칙이었다면 오늘 TQQQ를 얼마나 더 샀을지 + 현금 중 달러 비중."""
    history = history if history is not None else fetch_cnn_history()
    fx = usdkrw if usdkrw is not None else fetch_usdkrw()
    usd_krw = usd_cash_usd * fx
    usd_share = (usd_krw / cash_krw * 100) if cash_krw > 0 else 0.0
    dates = [d for d, _ in history]
    states = list(walk([s for _, s in history]))
    st = states[-1]
    tail = f" · 현금 중 달러 {usd_share:.0f}%" + (" (설계안 기준 50% 이상 권장)" if usd_share < 50 else "")
    if st.episode_start is None:
        return f"그림자(새 규칙): 공포 구간 아님 — 새로 살 것 없음{tail}"
    started = dates[st.episode_start].isoformat()
    now_pct = tqqq_krw / total_krw * 100 if total_krw > 0 else 0.0
    gap = max(0.0, st.limit * total_krw - tqqq_krw)
    can = min(gap, usd_krw)
    head = f"그림자(새 규칙): 공포 구간 {started}부터 {st.month + 1}개월째, TQQQ 한도 총자산의 {st.limit*100:.2f}% · 지금 {now_pct:.1f}%"
    if not st.in_zone:
        return f"{head} — 오늘 F&G가 {START_FG} 초과라 추가 매수 없음(구간은 {END_FG} 이상에서 끝남){tail}"
    if gap <= 0:
        return f"{head} — 이미 한도만큼 보유, 추가 매수 없음{tail}"
    short = "" if can >= gap else f", 달러 부족으로 {gap - can:,.0f}원어치는 못 삼"
    deep = " · 오늘 더 깊어져 대기 기간 무시" if st.deepened else ""
    return f"{head} → 약 {gap:,.0f}원어치 더 살 차례(달러 예수금으로 {can:,.0f}원{short}){deep}{tail}"
