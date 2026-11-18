"""판단 시나리오 10개를 돌려 점수표를 만든다(기준 CRITERIA.md).

    python agent/eval/run_eval.py            # 10개 전부
    python agent/eval/run_eval.py 2 9        # 고른 것만

도구 6개를 상황별 자료로 바꿔 끼우고(실제 조회·발송 없음) 에이전트를 돌린다. 결과: agent/eval/out/scorecard.md · results.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import fa_agent as A  # noqa: E402
import fa_tools as T  # noqa: E402
from scenarios import NOW, SCENARIOS  # noqa: E402


def patch(data: dict, notes: list):
    def get(key):
        def f(*a, **k):
            if key in data["errors"]:
                raise RuntimeError(data["errors"][key])
            return data[key]
        return f
    T.market_signal = get("market_signal")
    T.rule_plan = lambda account="한투 v2": get(f"rule_plan_{account}")()
    T.account_check = get("account_check")
    T.ledger = lambda days=14, account=None: get("ledger")()
    T.order_gate = get("order_gate")

    def notify(text):
        notes.append(text)
        return {"sent": False, "reason": "평가 실행이라 보내지 않음"}
    T.notify = notify


def score(sc: dict, report: str, notified: bool) -> list[tuple[str, bool]]:
    r = report or ""
    checks = []
    if sc["notify"] is not None:
        checks.append((f"알림 {'보냄' if sc['notify'] else '안 보냄'}", notified == sc["notify"]))
    for group in sc["must"]:
        checks.append((f"있음: {' / '.join(group)}", any(g in r for g in group)))
    for bad in sc["none"]:
        checks.append((f"없음: {bad}", bad not in r))
    checks.append(("보고서 있음", bool(r.strip())))
    return checks


def main():
    import os
    os.environ.setdefault("FA_AGENT_ENV", "eval")   # Langfuse 에서 운영 기록과 나눠 보기
    T.load_env()                                      # Langfuse 키(있으면)
    pick = {int(x) for x in sys.argv[1:]}
    A.VERBOSE = False
    # 고른 것만 돌리면 out/partial 에 남겨 전체 점수표(out/scorecard.md)를 덮지 않는다(11-18 한 번 덮어씀)
    out_dir = HERE / "out" / ("partial" if pick else ""); out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for sc in SCENARIOS:
        if pick and sc["id"] not in pick:
            continue
        notes: list[str] = []
        patch(sc["make"](), notes)
        t0 = time.time()
        try:
            res = anyio.run(lambda: A.run_agent(NOW, scenario=str(sc['id']), finish=False))
        except Exception as exc:  # noqa: BLE001
            res = {"report": "", "tool_calls": [], "usage": {"error": f"{type(exc).__name__}: {exc}"}}
        checks = score(sc, res["report"], bool(notes))
        ok = all(c for _, c in checks)
        if res.get("trace") is not None:   # Langfuse: 채점 결과를 그 실행의 트레이스에 점수로 붙인다
            bad = '; '.join(n for n, c in checks if not c) or None
            res["trace"].finish(res["report"], res["usage"], res.get("raw_usage"), bool(notes), res["tool_calls"],
                                {"scenario-pass": (1 if ok else 0, bad)})
        results.append({"id": sc["id"], "name": sc["name"], "pass": ok, "checks": checks, "notified": bool(notes),
                        "tools": res["tool_calls"], "usage": res["usage"], "trace_id": res.get("trace_id"), "seconds": round(time.time() - t0, 1), "report": res["report"]})
        print(f"{'통과' if ok else '틀림'}  {sc['id']:>2}. {sc['name']}  ({sum(c for _, c in checks)}/{len(checks)})  알림={bool(notes)}  "
              f"도구 {len(res['tool_calls'])}번  {res['usage']}", flush=True)
    n_ok = sum(r["pass"] for r in results)
    lines = [f"# 판단 시나리오 점수표 — {n_ok}/{len(results)} 통과", "",
             f"모델 {A.MODEL} · 기준 CRITERIA.md · 바탕 자료 2026-11-18 실제 도구 결과", "",
             "| # | 상황 | 결과 | 항목 | 알림 | 도구 | 시간 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['name']} | {'통과' if r['pass'] else '**틀림**'} | {sum(c for _, c in r['checks'])}/{len(r['checks'])} | "
                     f"{'보냄' if r['notified'] else '안 보냄'} | {len(r['tools'])} | {r['seconds']}초 |")
    lines += ["", "## 틀린 항목"]
    bad = [(r, n) for r in results for n, c in r["checks"] if not c]
    lines += [f"- {r['id']}. {r['name']}: {n}" for r, n in bad] or ["- 없음"]
    lines += ["", "## 보고서 원문"]
    for r in results:
        lines += ["", f"### {r['id']}. {r['name']}", "", "```", (r["report"] or "(없음)").strip(), "```"]
    (out_dir / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n{n_ok}/{len(results)} 통과 → {out_dir / 'scorecard.md'}")


if __name__ == "__main__":
    main()
