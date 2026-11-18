# FA 투자 에이전트 (뼈대)

365 챌린지 11/23 주 할 일 — "도구 6개 연결, 터미널에서 '오늘 할 일'이 한 번 끝까지 돈다".
매일 돌고 있는 자동매매 함수를 그대로 도구로 감싸서, 에이전트가 골라 쓰고 쉬운 말로 정리한다.

| 도구 | 하는 일 | 부르는 기존 코드 |
|---|---|---|
| `fa_market_signal` | F&G·공포 점수·FA곡선 목표·TQQQ RSI·CAPE 막기 | `today_signal`, `crisis_plan`, `fabot_v2.fear_score/cape_block` |
| `fa_rule_plan` | 계좌별 오늘 살 것·팔 것·대기와 이유 | `fabot_v2.plan_us/plan_kr`, `fabot_kr.plan` |
| `fa_account_check` | 실제 계좌 ↔ 장부 대조(조회만) | `account_check.check_kis/check_kiwoom` |
| `fa_ledger` | 최근 매매·배당·이자 기록 | Supabase `trades` |
| `fa_order_gate` | 주문 시각·승인 상태·정시 실행 기록(주문 안 냄) | 설정 `approval`, Supabase `schedule_heartbeats` |
| `fa_notify` | 텔레그램(사람이 `--send` 붙였을 때만) | Telegram Bot API |

## 실행

```
pip install -r agent/requirements.txt
python agent/fa_agent.py               # 오늘 할 일(알림은 미리 보기만)
python agent/fa_agent.py --send        # 텔레그램으로도
python agent/fa_agent.py --tools-only  # 언어 모델 없이 도구 결과만
pytest tests/test_fa_agent_tools.py    # 안전선 시험
```

언어 모델은 Claude Agent SDK(로컬은 Claude Code 로그인 사용, `CLAUDE_CLI_PATH`로 경로 지정 가능, `FA_AGENT_MODEL`로 모델 지정).

## 지키는 선

- 에이전트에는 **주문 도구가 없다.** 주문 수량은 규칙 엔진만 정하고, 주문은 GitHub Actions(`auto-trade.yml`)가 정해진 시각에만 낸다. `tests/test_fa_agent_tools.py`가 주문 함수를 부르지 않는지 코드로 확인한다.
- 텔레그램은 `--send`가 있을 때만 코드가 허락한다(언어 모델이 정할 수 없음).
- 기본 도구(파일·셸)는 주지 않는다(`tools=[]`). 쓸 수 있는 건 위 6개뿐.
- 증권사 키는 기본으로 안 쓴다 — 인선 PC 환경변수에 예전 모의 키가 남아 있어서(2026-11-18 실측) 가격은 야후 종가로 미리 보기, 계좌 대조는 건너뛴다. 키가 지금 계좌 것인 곳에서만 `FA_AGENT_USE_BROKER=1`.
- 비밀값은 환경변수로만 넘기고 화면·결과에 내지 않는다.

## 아직 안 한 것 (다음 주 이후)

- 시작·멈추기 신청 화면, 성적표, Langfuse 기록, 판단 시나리오 10개(11/30 주)
- GitHub Actions 에서 매일 아침 자동 실행(키가 있는 곳에서 실제 계좌 대조까지)
- 연말 비중 맞추기 날의 계획 내용 보여 주기(지금은 그날이라는 것만 알림)
