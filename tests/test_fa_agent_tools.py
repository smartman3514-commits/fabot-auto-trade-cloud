"""FA 에이전트 안전선 시험(2026-11-18): 에이전트는 주문을 낼 수 없고, 알림은 --send 일 때만, 증권사 키는 켰을 때만."""
import ast
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))
import fa_tools as T  # noqa: E402

ORDER_NAMES = {"_place_order", "_amend_order", "_cancel_order", "ChaseOrder", "OverseasChaseOrder",
               "place_domestic_order", "place_overseas_order", "run_live", "_execute",
               "_kis_overseas_buy", "_kis_overseas_sell", "_kis_domestic_buy", "_kis_domestic_sell",
               "_overseas_buy", "_overseas_sell", "_domestic_buy", "log_trade"}


@pytest.mark.parametrize("fname", ["fa_tools.py", "fa_agent.py"])
def test_agent_code_never_touches_order_functions(fname):
    tree = ast.parse((AGENT / fname).read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not (used & ORDER_NAMES), f"주문·장부 쓰기 함수를 부름: {used & ORDER_NAMES}"
    assert not ({"live_order_executor", "overseas_order_executor"} & imported), "주문 도구 모듈을 직접 불러옴"


def test_notify_blocked_without_send(monkeypatch):
    import requests
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append(1))
    monkeypatch.setattr(T, "SEND_ALLOWED", False)
    out = T.notify("시험 보고서")
    assert out["sent"] is False and out["preview"] == "시험 보고서" and not called


def test_notify_sends_only_when_allowed(monkeypatch):
    import requests

    class R:
        ok, status_code = True, 200
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: sent.append(json) or R())
    monkeypatch.setattr(T, "SEND_ALLOWED", True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    assert T.notify("보고서")["sent"] is True and sent[0]["text"] == "보고서"


def test_broker_keys_off_by_default(monkeypatch):
    for k in ("KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_STOCK", "KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("FA_AGENT_USE_BROKER", raising=False)
    assert T.broker_keys() == {"kis": False, "kiwoom": False}
    monkeypatch.setenv("FA_AGENT_USE_BROKER", "1")
    assert T.broker_keys() == {"kis": True, "kiwoom": True}


def test_load_env_reports_presence_only(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("SUPABASE_URL=https://example\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(T, "ENV_FILES", [f])
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    out = T.load_env()
    assert out["SUPABASE_URL"] is True and all(isinstance(v, bool) for v in out.values())
    assert "OTHER" not in out


def test_tracing_off_without_keys(monkeypatch):
    import tracing as TR
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert TR.enabled() is False
    run = TR.Run("프롬프트", "claude-sonnet-5-5")
    with run.tool("fa_ledger", [7]) as obs:
        run.tool_result(obs, {"rows": []})
    run.finish("보고서", {"cost_usd": 0.01}, {"input_tokens": 1}, False, ["fa_ledger"])   # 아무것도 안 하고 끝나야 함
    assert run.trace_id is None


def test_tracing_mask_inside_dicts():
    import tracing as TR
    out = TR.mask(data={"a": ["연락 010-1234-5678", {"b": "x@y.com"}]})
    assert out == {"a": ["연락 [전화번호]", {"b": "[이메일]"}]}


def test_tracing_strips_key_whitespace(monkeypatch):
    import tracing as TR
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", ' "pk-lf-1"\n')
    assert TR._key("LANGFUSE_PUBLIC_KEY") == "pk-lf-1"


def test_tracing_exports_only_our_spans():
    import tracing as TR

    class Scope:
        def __init__(self, n):
            self.name = n

    class Span:
        def __init__(self, n):
            self.instrumentation_scope = Scope(n)
    assert TR._ours(Span("langfuse-sdk")) is True
    assert TR._ours(Span("mcp-python-sdk")) is False
