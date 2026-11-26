"""FA 투자 에이전트 — 도구 6개를 골라 쓰고, 오늘 할 일과 이상을 쉬운 말로 정리한다(365 챌린지 11/23 주 '에이전트 뼈대').

    python agent/fa_agent.py               # 에이전트가 도구로 확인하고 '오늘 할 일'을 화면에(알림은 미리 보기만)
    python agent/fa_agent.py --send        # 같은 내용을 텔레그램으로도 보냄(사람이 붙였을 때만)
    python agent/fa_agent.py --tools-only  # 언어 모델 없이 도구 6개 결과만(시험용)

언어 모델은 설명·요약·도구 고르기만 한다. 주문 수량은 규칙 엔진만 정하고, 주문은 GitHub Actions 가 정해진 시각에만 낸다
(이 에이전트에는 주문 도구가 없다 — order_gate 는 상태만 본다). 텔레그램은 --send 가 있을 때만 코드가 허락한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fa_tools as T  # noqa: E402
import tracing as TR  # noqa: E402

_WIN_CLI = Path.home() / ".local" / "bin" / "claude.exe"
CLI_PATH = os.environ.get("CLAUDE_CLI_PATH") or (str(_WIN_CLI) if _WIN_CLI.exists() else None)
MODEL = os.environ.get("FA_AGENT_MODEL") or "claude-sonnet-5-5"   # 11-18: 운영·평가를 같은 모델로 고정(기본 모델은 PC마다 달라 비용 $1.30 vs $0.05)

SYSTEM_PROMPT = """당신은 FA 투자 에이전트예요. 두 모의 계좌(한투 v2, 키움 국내포트)의 규칙 자동매매를 매일 운영하는 팀원이에요.

할 일: 도구로 실제 자료를 확인한 뒤 '오늘 할 일' 보고서를 써요.
1. fa_market_signal 로 공포·탐욕 지수(F&G)와 CAPE 막기를 봐요.
2. fa_rule_plan 을 계좌마다(한투 v2, 키움 국내포트) 불러 규칙 엔진이 정한 살 것·팔 것·대기와 이유를 봐요.
3. fa_account_check 로 실제 계좌와 장부가 맞는지, fa_ledger 로 최근 기록을, fa_order_gate 로 주문 시각·승인·정시 실행 기록을 봐요.
4. 이상을 찾아요. 예: 신호는 매수 구간인데 매수가 없음(이유가 규칙에 있으면 이상이 아님 — 이유를 설명), 정해진 창에 실행 기록이 없음,
   장부와 계좌가 다름, 자료 경고(값이 2% 넘게 다름 등), 도구 실패.
   CAPE 출처(fa_market_signal 의 cape_sources): 자동매매나 FA여정이 'multpl' 이 아닌 보험(실러 원자료)·seed 로 계산했으면 보고서에 한 줄 적어요.
   FA여정 CAPE 의 stale=true(마지막 달이 2달 넘게 지남)이거나 출처가 'seed만'·'조회 실패'면 고쳐야 할 이상이에요(알릴 날).
   알아 둘 것: auto_trade_workflow 는 서머타임 대응·보험 실행 때문에 하루 여러 번 돌고, 창 밖이면 아무것도 안 하고 끝나요 —
   창 밖 실행 자체는 정상이에요. 이상은 '정해진 창(미국 04:40~06:10, 국내 15:05~15:35) 안에 실행 기록이 하나도 없을 때'예요.
   승인: fa_order_gate 의 approval 에서 required=true 인데 approved=false 인 계좌는 승인 전까지 주문이 하나도 안 나가요(오늘 주문이 없어도).
   이건 고쳐야 할 이상이고, '사람이 할 일'에 그 계좌의 승인 여부를 정하라고 써요.
5. 알릴 날만 fa_notify 에 보고서 본문을 넣어 한 번 불러요(실제로 보낼지는 코드가 한 번 더 정해요).
   알릴 날 = 고쳐야 할 이상이 있거나, 장부에 어제·오늘 실제 매수·매도가 있는 날. 조용한 날(모두 대기, 이상 없음)은 부르지 않아요 —
   자주 보내면 지겹고, 안 보내면 잊어요(소통 밀당). 이미 설명된 경고(예: 규칙대로 쉰 것)는 '고쳐야 할 이상'이 아니에요.

지킬 것
- 숫자는 도구 결과에 있는 것만 써요. 지어내지 말아요. 도구가 실패했으면 숨기지 말고 "조회 실패"라고 써요.
- 주문 수량을 바꾸거나 새 주문을 제안하지 말아요. 주문은 규칙 엔진 수량으로 GitHub Actions 가 정해진 시각에만 내요.
- 상품은 이름을 먼저, 코드는 괄호로 써요. 예: TIGER 배당커버드콜액티브(472150). 'FABOT' 대신 'FA'라고 써요.
- 쉬운 해요체로, 초보자도 알게 용어는 짧게 풀어요(예: CAPE 막기 = 주식이 비싸서 평소 매수를 쉬는 것).
- 가격 출처가 야후 종가면 "미리 보기(야후 종가 기준)"라고 밝혀요.

보고서 형식(15줄 안쪽):
FA 오늘 할 일 (날짜)
시장: F&G·구간·CAPE 한두 줄
한투 v2: 미국장 마감(시각) 할 일 / 국내장 마감 할 일
키움 국내포트: 국내장 마감 할 일
이상: 없음 또는 목록(무엇이·왜·누가 볼 것)
사람이 할 일: 대개 "없음 — 기다리기". 꼭 필요할 때만 구체적으로.
마지막 답에는 보고서 본문만 담아요."""


def _ok(result) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}


def _err(exc: Exception) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, ensure_ascii=False)}],
            "is_error": True}


RUN: TR.Run | None = None   # 지금 실행의 Langfuse 트레이스(키가 없으면 아무것도 안 함)


async def _run(name: str, fn, *a):
    with (RUN.tool(name, list(a)) if RUN else _nullctx()) as obs:
        try:
            result = await anyio.to_thread.run_sync(lambda: fn(*a))
        except Exception as exc:  # noqa: BLE001 — 도구 실패는 에이전트에게 그대로 알린다
            if RUN:
                RUN.tool_result(obs, error=f"{type(exc).__name__}: {str(exc)[:300]}")
            return _err(exc)
        if RUN:
            RUN.tool_result(obs, result)
        return _ok(result)


class _nullctx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


@tool("fa_market_signal", "오늘의 공포·탐욕 지수(CNN F&G), 공포 점수·FA곡선 목표 비중, TQQQ RSI, CAPE 막기 여부와 지금 규칙 숫자.", {})
async def t_signal(args):
    return await _run("fa_market_signal", T.market_signal)


@tool("fa_rule_plan", "규칙 엔진이 정한 오늘 할 일(칸별 매수·매도·대기, 금액, 이유)과 평가 요약. account 는 '한투 v2' 또는 '키움 국내포트'. 주문은 내지 않음.",
      {"account": str})
async def t_plan(args):
    return await _run("fa_rule_plan", T.rule_plan, args.get("account") or "한투 v2")


@tool("fa_account_check", "실제 증권사 계좌 수량·현금과 장부(Supabase trades)가 맞는지 대조. 조회만 함.", {})
async def t_check(args):
    return await _run("fa_account_check", T.account_check)


@tool("fa_ledger", "최근 N일 매매·배당·이자 기록(두 모의 계좌). account 를 비우면 둘 다.", {"days": int, "account": str})
async def t_ledger(args):
    return await _run("fa_ledger", T.ledger, int(args.get("days") or 14), args.get("account") or None)


@tool("fa_order_gate", "주문이 언제·누가 내는지, 계좌별 승인 상태, 최근 30시간 정시 실행 기록. 이 도구는 주문을 내지 않음.", {})
async def t_gate(args):
    return await _run("fa_order_gate", T.order_gate)


@tool("fa_notify", "보고서를 텔레그램으로 보냄. 사람이 --send 로 허락한 실행에서만 실제로 나가고, 아니면 미리 보기만 돌려줌.", {"text": str})
async def t_notify(args):
    return await _run("fa_notify", T.notify, args.get("text") or "")


VERBOSE = True
TOOLS = [t_signal, t_plan, t_check, t_ledger, t_gate, t_notify]


def options() -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name="fa", tools=TOOLS)
    kw = dict(cli_path=CLI_PATH, system_prompt=SYSTEM_PROMPT, mcp_servers={"fa": server},
              allowed_tools=[f"mcp__fa__{t.name}" for t in TOOLS], tools=[],   # 기본 도구(파일·셸 등)는 하나도 안 줌
              permission_mode="bypassPermissions", max_turns=14)
    if MODEL:
        kw["model"] = MODEL
    return ClaudeAgentOptions(**kw)


async def run_agent(now: str | None = None, scenario: str | None = None, finish: bool = True) -> dict:
    """finish=False 면 트레이스를 열어 둔 채 돌려준다(평가가 채점 점수를 붙인 뒤 out['trace'].finish(...))."""
    global RUN
    report, usage, raw, calls = None, None, None, []
    prompt = f"지금은 {now or format(datetime.now(T.KST), '%Y-%m-%d %H:%M')} KST 예요. 오늘 할 일 보고서를 써 줘요."
    RUN = TR.Run(prompt, MODEL, scenario)
    try:
        async for msg in query(prompt=prompt, options=options()):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        calls.append(b.name.removeprefix("mcp__fa__"))
                        if VERBOSE:
                            print(f"  · 도구: {calls[-1]} {json.dumps(b.input, ensure_ascii=False) if b.input else ''}", flush=True)
                    elif isinstance(b, TextBlock):
                        report = b.text
            elif isinstance(msg, ResultMessage):
                raw = msg.usage or {}
                usage = {"turns": msg.num_turns, "seconds": round((msg.duration_ms or 0) / 1000, 1),
                         "cost_usd": msg.total_cost_usd, "is_error": msg.is_error}
    except Exception as exc:  # noqa: BLE001 — 실패도 트레이스에 남기고 다시 던진다
        usage = {"error": f"{type(exc).__name__}: {str(exc)[:300]}", "is_error": True}
        RUN.finish(report, usage, raw, "fa_notify" in calls, calls)
        raise
    out = {"report": report, "tool_calls": calls, "usage": usage, "notified": "fa_notify" in calls,
           "trace_id": RUN.trace_id, "trace": RUN, "raw_usage": raw}
    if finish:
        RUN.finish(report, usage, raw, out["notified"], calls)
    return out


def tools_only() -> None:
    for name, fn in [("시장 신호", T.market_signal), ("규칙 엔진 · 한투 v2", lambda: T.rule_plan("한투 v2")),
                     ("규칙 엔진 · 키움 국내포트", lambda: T.rule_plan("키움 국내포트")), ("계좌 대조", T.account_check),
                     ("장부 7일", lambda: T.ledger(7)), ("주문 상태", T.order_gate)]:
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"\n=== {name} ===\n{json.dumps(out, ensure_ascii=False, indent=1, default=str)[:3000]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="FA 투자 에이전트 — 오늘 할 일")
    ap.add_argument("--send", action="store_true", help="보고서를 텔레그램으로도 보냄")
    ap.add_argument("--tools-only", action="store_true", help="언어 모델 없이 도구 결과만")
    a = ap.parse_args()
    have = T.load_env()
    print(f"FA 투자 에이전트 · 설정 {sum(have.values())}/{len(have)}개 있음 · 증권사 키 사용 {T.broker_keys()}")
    if a.tools_only:
        tools_only()
        return
    T.SEND_ALLOWED = a.send
    out = anyio.run(run_agent)
    print("\n" + "=" * 40 + "\n" + (out["report"] or "(보고서 없음)") + "\n" + "=" * 40)
    print(f"도구 {len(out['tool_calls'])}번: {', '.join(out['tool_calls'])}")
    print(f"사용량: {out['usage']}")
    missing = {"fa_market_signal", "fa_rule_plan", "fa_order_gate"} - set(out["tool_calls"])
    if missing or not out["report"]:
        print(f"⚠️ 꼭 써야 할 도구를 안 썼거나 보고서가 없음: {sorted(missing)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
