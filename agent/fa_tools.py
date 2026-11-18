"""FA 투자 에이전트의 도구 6개 — 이미 매일 돌고 있는 자동매매 함수(fabot_v2·fabot_kr·account_check·cooldown)를
그대로 부르는 얇은 층이다. 2026-11-18 인선: "다음 주 진도(에이전트 뼈대) 해도 돼요" → 자동매매 저장소 안 agent/ 로.

지키는 선(계획서 '안전선'과 같음)
- 주문 수량은 규칙 엔진(fabot_v2.plan_us / fabot_kr.plan)만 정한다. 이 파일에는 주문을 내는 코드가 없다.
  실제 주문은 지금처럼 정해진 시각에 GitHub Actions(auto-trade.yml)가 낸다.
- 알림(텔레그램)은 사람이 실행할 때 --send 를 붙였을 때만 나간다(언어 모델이 혼자 정할 수 없게 코드로 막음).
- 비밀값은 읽어서 환경변수로만 넘기고, 화면·로그·결과에 절대 내보내지 않는다.

증권사 키는 기본으로 쓰지 않는다(FA_AGENT_USE_BROKER=1 일 때만): 가격은 야후 종가로 대신 계산하고(미리 보기용),
실제 계좌 대조는 건너뛴다. 키가 지금 계좌 것인 GitHub Actions 에서 켜면 실제 증권사 가격·계좌로 돈다.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KST = timezone(timedelta(hours=9))
ACCOUNTS = ("한투 v2", "키움 국내포트")
# 비밀값을 찾는 곳(값은 읽기만 하고 내보내지 않음). 이미 환경변수에 있으면 그대로 쓴다.
ENV_FILES = [ROOT / ".env", ROOT.parent / "fabot-trade-journal" / ".env", ROOT.parent / "fabot-daily-briefing-agent" / ".env"]
ENV_NAMES = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
             "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_STOCK",
             "KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_APP_SECRET")
WINDOWS = {"us": "04:40~06:10 KST(미국장 마감 직전, 서머타임에 따라 05:00/06:00 마감)", "kr": "15:05~15:35 KST(국내장 마감 동시호가)"}


def load_env() -> dict[str, bool]:
    """환경변수가 비어 있으면 ENV_FILES 에서 채운다. 돌려주는 값은 '있다/없다'뿐(값은 절대 안 돌려줌)."""
    for f in ENV_FILES:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            k = k.strip()
            if sep and k in ENV_NAMES and not os.environ.get(k) and v.strip():
                os.environ[k] = v.strip().strip('"').strip("'")
    return {k: bool(os.environ.get(k)) for k in ENV_NAMES}


def broker_keys() -> dict[str, bool]:
    """증권사 키를 쓸지. 기본은 안 씀 — 2026-11-18 실측: 인선 PC 환경변수에 예전 모의 키가 남아 있어(한투 계좌 조회
    '모의투자 신청내역을 확인하세요', 키움 8001 인증 실패) 지금 계좌와 다르다. 키가 지금 계좌 것인 곳(GitHub Actions)에서만
    FA_AGENT_USE_BROKER=1 로 켠다."""
    if os.environ.get("FA_AGENT_USE_BROKER") != "1":
        return {"kis": False, "kiwoom": False}
    return {"kis": all(os.environ.get(k) for k in ("KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_STOCK")),
            "kiwoom": all(os.environ.get(k) for k in ("KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_APP_SECRET"))}


# ── 증권사 키가 없을 때: 가격만 야후 종가로 바꿔 끼운다(규칙 계산은 그대로) ───────────────────────────
_YAHOO_US = {"BRK/B": "BRK-B", "BRKb": "BRK-B"}


def _yahoo_last(ticker: str) -> float:
    import fabot_v2 as v2
    return float(v2.fetch_daily_closes(ticker, "5d")[-1][1])


def _use_yahoo_prices() -> str:
    """가격 출처를 돌려준다. 키가 없으면 fabot_v2/fabot_kr 의 가격 함수만 야후로 바꾼다(이 프로세스 안에서만)."""
    import fabot_kr as kr
    import fabot_v2 as v2
    keys = broker_keys()
    src = []
    if not keys["kis"]:
        v2.kis_us_price = lambda code: _yahoo_last(_YAHOO_US.get(code, code))
        v2.kis_kr_price = lambda code: _yahoo_last(f"{code}.KS")
        src.append("한투 가격 = 야후 종가(증권사 키 안 씀)")
    if not keys["kiwoom"]:
        kr.kiwoom_price = lambda code: _yahoo_last(f"{code}.KS")
        src.append("키움 가격 = 야후 종가(증권사 키 안 씀)")
    return " · ".join(src) or "증권사 실시간 가격"


# ── ① 시장 신호 ─────────────────────────────────────────────────────────────

def market_signal() -> dict:
    """오늘의 공포·탐욕 지수(CNN), 공포 점수 S·FA곡선 목표 비중, TQQQ RSI, CAPE 막기."""
    import crisis_plan
    import fabot_v2 as v2
    import today_signal
    info = today_signal.get_cnn_score()
    hist = crisis_plan.fetch_cnn_history()
    tq = v2.fetch_daily_closes("TQQQ")
    fear = v2.fear_score(info["score"], hist, tq[-1][1], tq)
    rsi = v2.rsi_wilder([c for _, c in tq])
    cape = v2.cape_block()
    fg = float(info["score"])
    word = "극단적 공포" if fg < 25 else "공포" if fg < 45 else "중립" if fg <= 55 else "탐욕" if fg < 75 else "극단적 탐욕"
    flags = [f for f, on in (("탐욕 매도 조건(F&G ≥ %d)" % v2.SELL_HALF, fg >= v2.SELL_HALF),
                             ("커버드콜 평시 매수 조건(F&G ≤ %d)" % v2.PEACE_HI, fg <= v2.PEACE_HI),
                             ("CAPE 막기 중 공포 매수 조건(F&G ≤ %d, 나스닥 낙폭 등 더 봄)" % v2.DIP_FG, fg <= v2.DIP_FG)) if on]
    zone = word + (" · " + " · ".join(flags) if flags else "")
    return {"fg": round(fg, 1), "fg_date": str(info.get("date") or info.get("timestamp") or ""), "zone": zone,
            "fear_S": round(fear["S"], 2), "curve_target": round(fear["target"], 3), "tqqq_rsi": round(rsi, 1),
            "tqqq_close": [str(tq[-1][0]), tq[-1][1]],
            "cape": cape.get("cape"), "cape_line": cape.get("line") and round(cape["line"], 1), "cape_block": bool(cape.get("hot")),
            "cape_text": v2.cape_text(cape),
            "rules": {"sell_half_fg": v2.SELL_HALF, "sell_all_fg": v2.SELL_ALL, "sell_gap_days": v2.SELL_GAP_DAYS,
                      "dip_fg": v2.DIP_FG, "cape_k": v2.CAPE_K, "peace_fg_max": v2.PEACE_HI}}


# ── ② 규칙 엔진 ─────────────────────────────────────────────────────────────

def _orders_out(orders, sleeves, currency_of) -> list[dict]:
    names = {s.key: s.ledger for s in sleeves}
    out = []
    for o in orders:
        out.append({"칸": o.sleeve, "상품": names.get(o.sleeve, o.sleeve), "할 일": {"buy": "매수", "sell": "매도", "hold": "대기"}.get(o.side, o.side),
                    "금액": round(o.amount, 2) if o.side == "buy" else None, "통화": currency_of(o.sleeve),
                    "수량": o.qty, "이유": o.note})
    return out


def rule_plan(account: str = "한투 v2") -> dict:
    """규칙 엔진이 정한 오늘의 할 일(살 것·팔 것·대기와 이유). 계산만 하고 주문은 내지 않는다."""
    import fabot_kr as kr
    import fabot_v2 as v2
    import rebalance as RB
    price_src = _use_yahoo_prices()
    if account == "한투 v2":
        cfg = v2.resolve_capital(v2.load_config())
        ctx = v2.gather(cfg, v2.fetch_v2_trades())
        cur = {s.key: s.currency for s in v2.SLEEVES}
        us = v2.plan_us(ctx["pos"], ctx["fg"], ctx["fear"], ctx["cape"], ctx["prices"], ctx["today_us"], tq_closes=ctx.get("tq_closes"),
                        brk_hist=ctx["brk_hist"], start=date.fromisoformat(cfg["start_date"]), ndx_closes=ctx.get("ndx_closes"))
        gstart = date.fromisoformat(cfg.get("gold_start") or cfg["start_date"])
        krw = v2.plan_kr(ctx["pos"], ctx["fg"], ctx["today_kst"], gold={"start": gstart, "rsi": ctx["gold_rsi"], "price": ctx["prices"]["GOLD"]})
        rb_days = {"USD": RB.last_trading_day(ctx["today_us"].year, "USD"), "KRW": RB.last_trading_day(ctx["today_kst"].year, "KRW")}
        return {"account": account, "price_source": price_src, "summary": v2.summary_lines(ctx["pos"], ctx["prices"], cfg["fx"]),
                "us_close_orders": _orders_out(us, v2.SLEEVES, cur.get), "kr_close_orders": _orders_out(krw, v2.SLEEVES, cur.get),
                "windows": WINDOWS, "year_end_rebalance_days": rb_days,
                "note": "미국 칸 주문은 미국장 마감 창, 원화 칸 주문은 국내장 마감 창에 GitHub Actions 가 낸다. 연말 비중 맞추기 날에는 그 계획이 먼저다."}
    if account == "키움 국내포트":
        cfg = kr.load_config()
        ctx = kr.gather(cfg, kr.fetch_trades())
        orders = kr.plan(ctx["pos"], ctx["fg"], ctx["fear"], ctx["cape"], ctx["prices"], ctx["today"], ctx["div_hist"],
                         date.fromisoformat(cfg["start_date"]), gold_start=date.fromisoformat(cfg.get("gold_start") or cfg["start_date"]),
                         gold_rsi=ctx["gold_rsi"], ndx_closes=ctx.get("ndx_closes"), tq_closes=ctx.get("tq_kr"))
        tot = sum(p.cash + p.qty * ctx["prices"][k] for k, p in ctx["pos"].items())
        return {"account": account, "price_source": price_src, "summary": [f"국내포트 평가 {tot:,.0f}원 (재원 {cfg['krw_capital']:,.0f}원)"],
                "kr_close_orders": _orders_out(orders, kr.SLEEVES, lambda k: "KRW"), "windows": {"kr": WINDOWS["kr"]},
                "year_end_rebalance_days": {"KRW": RB.last_trading_day(ctx["today"].year, "KRW")},
                "note": "주문은 국내장 마감 창에 GitHub Actions 가 낸다. 연말 비중 맞추기 날에는 그 계획이 먼저다."}
    raise ValueError(f"계좌는 {ACCOUNTS} 중 하나")


# ── ③ 계좌 조회·대조 ─────────────────────────────────────────────────────────

def account_check() -> dict:
    """실제 증권사 계좌와 장부(Supabase trades)가 맞는지 — 조회만, 주문 없음. 키가 없는 쪽은 건너뛴다."""
    import account_check as AC
    keys = broker_keys()
    problems: list[str] = []
    info: list[str] = []
    skipped = []
    for name, ok, fn in (("한투", keys["kis"], AC.check_kis), ("키움", keys["kiwoom"], AC.check_kiwoom)):
        if not ok:
            skipped.append(f"{name}: 증권사 키를 쓰지 않는 실행이라 실제 계좌 대조를 건너뜀(GitHub Actions 의 계좌 체크는 매일 돎)")
            continue
        try:
            fn(problems, info)
        except Exception as exc:  # noqa: BLE001 — 한쪽이 실패해도 다른 쪽은 본다
            problems.append(f"{name} 체크 실패: {type(exc).__name__}: {str(exc)[:200]}")
    return {"ok": not problems, "problems": problems, "info": info, "skipped": skipped}


# ── ④ 장부 ──────────────────────────────────────────────────────────────────

def ledger(days: int = 14, account: str | None = None) -> dict:
    """Supabase trades 의 최근 매매·배당 기록(두 모의 계좌)."""
    import requests

    import cooldown
    env = cooldown._load_journal_env()
    since = (datetime.now(KST).date() - timedelta(days=max(1, min(int(days), 120)))).isoformat()
    params = {"select": "trade_date,account,ticker,action,quantity,price,fg_score,memo",
              "trade_date": f"gte.{since}", "order": "trade_date.desc,created_at.desc", "limit": "60"}
    params["account"] = f"eq.{account}" if account else f"in.({','.join(ACCOUNTS)})"
    r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/trades", params=params,
                     headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"}, timeout=15)
    r.raise_for_status()
    rows = r.json()
    for x in rows:
        x["memo"] = (x.get("memo") or "")[:120]
    return {"since": since, "count": len(rows), "rows": rows}


# ── ⑤ 주문 (미리 보기·실행 상태만) ────────────────────────────────────────────

def order_gate() -> dict:
    """주문이 언제·어떻게 나가는지, 승인 상태, 오늘 정시 실행이 실제로 돌았는지(스케줄 기록).
    이 도구는 주문을 내지 않는다 — 에이전트에는 주문 코드가 없다."""
    import json

    import requests

    import cooldown
    import fabot_kr as kr
    import fabot_v2 as v2
    appr = {"한투 v2": (v2.load_config().get("approval") or {}), "키움 국내포트": (kr.load_config().get("approval") or {})}
    env = cooldown._load_journal_env()
    since = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    try:
        r = requests.get(f"{env['SUPABASE_URL']}/rest/v1/schedule_heartbeats",
                         params={"select": "job,ran_at,detail", "ran_at": f"gte.{since}", "order": "ran_at.desc", "limit": "40"},
                         headers={"apikey": env["SUPABASE_SERVICE_KEY"], "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}"}, timeout=15)
        r.raise_for_status()
        beats = [{"job": b["job"], "ran_at_kst": datetime.fromisoformat(b["ran_at"].replace("Z", "+00:00")).astimezone(KST).strftime("%m-%d %H:%M"),
                  "detail": (b.get("detail") or "")[:80]} for b in r.json()]
    except Exception as exc:  # noqa: BLE001
        beats = [{"error": f"스케줄 기록을 못 읽음: {type(exc).__name__}"}]
    return {"agent_places_orders": False,
            "executor": "GitHub Actions auto-trade.yml — 정해진 시각에만, 규칙 엔진 수량으로만",
            "windows": WINDOWS,
            "approval": {k: {"required": bool(a.get("required")), "approved": bool(a.get("approved")), "by": a.get("by"), "at": a.get("at")}
                         for k, a in appr.items()},
            "recent_runs_30h": beats,
            "stop_request": "멈추기 신청은 아직 화면이 없음(11/30 주 할 일) — 지금은 approval 값으로만 멈출 수 있음"}


# ── ⑥ 알림 ──────────────────────────────────────────────────────────────────

SEND_ALLOWED = False   # fa_agent.py 가 사람이 --send 를 붙였을 때만 True 로 바꾼다


def notify(text: str) -> dict:
    """텔레그램으로 보낸다. 사람이 --send 로 허락한 실행에서만 실제로 나가고, 아니면 미리 보기만 돌려준다."""
    text = (text or "").strip()[:3500]
    if not text:
        return {"sent": False, "reason": "빈 내용"}
    if not SEND_ALLOWED:
        return {"sent": False, "reason": "이번 실행은 --send 없이 돌아서 보내지 않음(미리 보기)", "preview": text}
    import requests
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return {"sent": False, "reason": "텔레그램 설정이 없음"}
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=20)
    return {"sent": r.ok, "status": r.status_code}
