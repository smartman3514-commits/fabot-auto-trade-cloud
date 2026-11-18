"""FA 에이전트 실행을 Langfuse 트레이스로 남긴다(365 챌린지 11/30 주 'Langfuse 기록', 2026-11-18).

실행 한 번 = 트레이스 하나(fa-agent-daily, agent) 그 아래:
  - claude-agent (generation) : 모델·토큰·비용·최종 보고서
  - tool:이름 (tool)          : 도구마다 입력·결과(길면 자름)·실패면 WARNING
점수: notified(알림 보냄), tools(도구 쓴 횟수), 평가 실행이면 scenario-pass(통과) + 틀린 항목.

- LANGFUSE_PUBLIC_KEY·LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 켠다. 없으면 기록만 꺼지고 에이전트는 그대로 돈다.
- 키 앞뒤 공백·줄바꿈·따옴표를 지운다(과정 10: Secret Manager 값에 보이지 않는 글자가 섞여 401).
- 보내기 전에 전화번호·이메일·카드 번호 모양은 가린다(dict·list 안쪽까지). 비밀값은 애초에 도구 결과에 없다.
- environment: FA_AGENT_ENV(github / eval) 없으면 local. 버전: GITHUB_SHA 앞 7자리 없으면 local.
근거: friend-agent/app/tracing.py (과정 10), langfuse 5.1.0
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[카드번호]"),
    (re.compile(r"\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b"), "[전화번호]"),
]
_client = None


def _key(name: str) -> str:
    return os.getenv(name, "").strip().strip("\"'").strip()


def enabled() -> bool:
    return bool(_key("LANGFUSE_PUBLIC_KEY") and _key("LANGFUSE_SECRET_KEY"))


def mask(*, data, **kwargs):
    if isinstance(data, str):
        for p, label in _PATTERNS:
            data = p.sub(label, data)
        return data
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


def env_name() -> str:
    return (os.getenv("FA_AGENT_ENV") or "local").strip().lower()


def version() -> str:
    return (os.getenv("GITHUB_SHA") or "local")[:7]


def _ours(span) -> bool:
    """우리가 연 기록(langfuse-sdk)만 보낸다. 11-18 첫 기록에서 MCP 라이브러리(scope mcp-python-sdk)가 도구마다
    따로 트레이스를 하나씩 더 만들어(실행 한 번에 7개로 흩어짐) 막음 — 같은 내용은 tool:이름 칸에 이미 있다."""
    scope = getattr(getattr(span, "instrumentation_scope", None), "name", "") or ""
    return scope == "langfuse-sdk"


def lf():
    global _client
    if _client is None:
        from langfuse import Langfuse
        on = enabled()
        if not on:
            logging.getLogger("langfuse").setLevel(logging.ERROR)
        base = _key("LANGFUSE_BASE_URL") or None
        _client = Langfuse(public_key=_key("LANGFUSE_PUBLIC_KEY") or None, secret_key=_key("LANGFUSE_SECRET_KEY") or None,
                           base_url=base, tracing_enabled=on, mask=mask, environment=env_name(),
                           should_export_span=_ours)
    return _client


def _short(x, limit: int = 6000):
    s = json.dumps(x, ensure_ascii=False, default=str)
    return x if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit}자)"


class Run:
    """실행 한 번의 트레이스. 키가 없으면 모든 메서드가 아무것도 안 한다."""

    def __init__(self, prompt: str, model: str, scenario: str | None = None):
        self.root = self.gen = None
        self._attrs = None
        if not enabled():
            return
        from langfuse import propagate_attributes
        day = datetime.now().strftime("%Y-%m-%d")
        tags = [env_name()] + ([f"scenario:{scenario}"] if scenario else [])
        self._attrs = propagate_attributes(user_id="operator", session_id=f"fa-agent-{day}", version=version(),
                                           tags=tags, trace_name="fa-agent-daily")
        self._attrs.__enter__()
        self.root = lf().start_observation(name="fa-agent-daily", as_type="agent", input={"prompt": prompt},
                                           metadata={"model": model, "scenario": scenario, "version": version()})
        self.gen = self.root.start_observation(name="claude-agent", as_type="generation", model=model, input=prompt)

    @contextmanager
    def tool(self, name: str, args):
        if self.root is None:
            yield None
            return
        obs = self.root.start_observation(name=f"tool:{name}", as_type="tool", input=args)
        try:
            yield obs
        finally:
            obs.end()

    def tool_result(self, obs, result=None, error: str | None = None):
        if obs is None:
            return
        if error:
            obs.update(output={"error": error}, level="WARNING", status_message=error[:200])
        else:
            obs.update(output=_short(result))

    def finish(self, report: str | None, usage: dict | None, raw_usage: dict | None, notified: bool, calls: list[str],
               scores: dict | None = None):
        if self.root is None:
            return
        u = raw_usage or {}
        details = {k: int(u[s]) for k, s in (("input", "input_tokens"), ("output", "output_tokens"),
                                               ("cache_read_input_tokens", "cache_read_input_tokens"),
                                               ("cache_creation_input_tokens", "cache_creation_input_tokens")) if u.get(s)}
        cost = (usage or {}).get("cost_usd")
        self.gen.update(output=report, usage_details=details or None, cost_details={"total": float(cost)} if cost else None,
                        level="ERROR" if (usage or {}).get("is_error") or not report else None)
        self.gen.end()
        self.root.update(output={"report": report, "notified": notified, "tool_calls": calls, "usage": usage})
        self.root.score_trace(name="notified", value=1 if notified else 0, data_type="BOOLEAN")
        self.root.score_trace(name="tools", value=len(calls), data_type="NUMERIC")
        for name, (value, comment) in (scores or {}).items():
            self.root.score_trace(name=name, value=value, data_type="BOOLEAN", comment=comment)
        self.root.end()
        if self._attrs is not None:
            self._attrs.__exit__(None, None, None)
        lf().flush()

    @property
    def trace_id(self):
        return getattr(self.root, "trace_id", None)
