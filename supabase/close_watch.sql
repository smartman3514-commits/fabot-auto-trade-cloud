-- FABOT 마감 알림 감시 (2026-10-06)
--
-- 왜: 10-06 04:11~07:49 KST GitHub Actions 장애로 미국장 마감 자동매매 실행 8번이 전부 취소됐고,
-- 감시 워크플로(schedule-watchdog)도 같은 GitHub 위에서 돌아 함께 멈춰서 아무 알림 없이 지나갔다.
-- 브리핑도 cron-job.org → GitHub → Render 경로라 GitHub이 멈추면 같이 멈춘다.
-- 그래서 감시를 GitHub·Render 둘 다와 무관한 Supabase 안(pg_cron + pg_net)에서 돌린다.
--
-- 동작: auto-trade.yml이 마감 알림을 실제로 보낸 뒤 schedule_heartbeats에 close_notified_us / _kr를
-- 남긴다. 마감 뒤 이 함수가 오늘 그 표시가 있는지 보고, 없으면 텔레그램으로 대체 알림을 보낸다
-- (같은 날 같은 시장은 한 번만). 대체 알림에는 그 시각 F&G와 "규칙상 매매가 필요했는지"를 담는다.
--
-- 설치(Supabase 대시보드에서 한 번):
--   1) Database → Extensions에서 pg_cron, pg_net 켜기 (또는 아래 create extension 실행)
--   2) Project Settings → Vault(또는 SQL)에서 비밀 2개 만들기 — 값은 GitHub secrets와 같은 것:
--        select vault.create_secret('<텔레그램 봇 토큰>', 'fabot_telegram_bot_token');
--        select vault.create_secret('<텔레그램 채팅 ID>', 'fabot_telegram_chat_id');
--   3) 이 파일 나머지를 SQL Editor에서 실행
--   4) 시험: select public.fabot_close_watch('us');  → 오늘 표시가 없으면 대체 알림이 실제로 간다
--
-- 규칙 숫자는 today_signal.py(BUY_THRESHOLDS·SELL_THRESHOLDS·COVERED_CALL_ZONE)와 같아야 한다.
-- 규칙을 바꾸면 이 파일의 case 문도 같이 바꾼다.

create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.fabot_close_watch(p_market text)
returns text
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_today_kst date := (now() at time zone 'Asia/Seoul')::date;
  v_since timestamptz;
  v_label text;
  v_fg numeric;
  v_fg_at timestamptz;
  v_judge text;
  v_token text;
  v_chat text;
  v_text text;
begin
  if p_market = 'us' then
    -- 미국장 마감은 KST 새벽(05:00 서머타임 / 06:00 겨울). 오늘 03:00 KST 이후 기록을 본다.
    v_since := (v_today_kst + time '03:00') at time zone 'Asia/Seoul';
    v_label := '미국장 마감 (TQQQ)';
  elsif p_market = 'kr' then
    -- 국내장 마감 실행은 15:15~15:35 KST. 오늘 15:00 KST 이후 기록을 본다.
    v_since := (v_today_kst + time '15:00') at time zone 'Asia/Seoul';
    v_label := '국내장 마감 (커버드콜)';
  else
    raise exception 'p_market은 us 또는 kr';
  end if;

  if exists (select 1 from schedule_heartbeats where job = 'close_notified_' || p_market and ran_at >= v_since) then
    return 'ok: 오늘 마감 알림이 이미 나감';
  end if;
  if exists (select 1 from schedule_heartbeats where job = 'close_watch_alert_' || p_market and ran_at >= v_since) then
    return 'skip: 오늘 대체 알림을 이미 보냄';
  end if;

  select score, computed_at into v_fg, v_fg_at
  from live_scores where source = 'cnn_real'
  order by computed_at desc limit 1;

  v_judge := case
    when v_fg is null then 'F&G 값을 읽지 못했습니다 — 수동 확인이 필요합니다.'
    when p_market = 'us' and v_fg <= 30 then 'TQQQ 매수 구간(F&G 30 이하)입니다 — 매수가 빠졌을 수 있어 수동 확인이 필요합니다.'
    when p_market = 'us' and v_fg >= 72 then 'TQQQ 매도 구간(F&G 72 이상)입니다 — 매도가 빠졌을 수 있어 수동 확인이 필요합니다.'
    when p_market = 'us' then 'TQQQ 대기 구간(31~71)이라 규칙상 매매는 없었을 것입니다.'
    when v_fg between 35 and 65 then '커버드콜 추가매수 구간(F&G 35~65)입니다 — 매수가 빠졌을 수 있어 수동 확인이 필요합니다.'
    else '커버드콜 추가매수 구간이 아니라 규칙상 매매는 없었을 것입니다.'
  end;

  select decrypted_secret into v_token from vault.decrypted_secrets where name = 'fabot_telegram_bot_token';
  select decrypted_secret into v_chat from vault.decrypted_secrets where name = 'fabot_telegram_chat_id';
  if v_token is null or v_chat is null then
    return 'error: Vault에 fabot_telegram_bot_token / fabot_telegram_chat_id가 없음';
  end if;

  v_text := '⚠️ FABOT 대체 알림 — ' || v_label || E'\n\n'
    || '오늘 마감 자동매매 결과 알림이 오지 않았습니다(GitHub 실행 실패·지연 가능성).' || E'\n\n'
    || 'F&G ' || coalesce(round(v_fg, 1)::text, '?')
    || coalesce(' (' || to_char(v_fg_at at time zone 'Asia/Seoul', 'MM-DD HH24:MI') || ' KST 기준)', '') || E'\n'
    || '판정: ' || v_judge || E'\n'
    || '규칙: TQQQ 매수 30/25/20 이하 · 매도 72/77 이상 · 커버드콜 F&G 35~65' || E'\n\n'
    || '실행 기록: https://github.com/smartman3514-commits/fabot-auto-trade-cloud/actions';

  perform net.http_post(
    url := 'https://api.telegram.org/bot' || v_token || '/sendMessage',
    body := jsonb_build_object('chat_id', v_chat, 'text', v_text),
    headers := '{"Content-Type": "application/json"}'::jsonb
  );
  insert into schedule_heartbeats (job, detail) values ('close_watch_alert_' || p_market, v_judge);
  return 'sent: ' || v_judge;
end;
$$;

-- 웹(PostgREST)에서 아무나 불러 텔레그램을 보내지 못하게 막는다. pg_cron과 SQL Editor(postgres)만 실행.
revoke execute on function public.fabot_close_watch(text) from public, anon, authenticated;

-- 미국장: KST 화~토 06:40 (= UTC 월~금 21:40). 겨울(EST) 마감 06:00 + 실행 창 20분 뒤.
-- 국내장: KST 월~금 15:50 (= UTC 06:50). 실행 창 15:15~15:35 뒤.
select cron.schedule('fabot_close_watch_us', '40 21 * * 1-5', $$select public.fabot_close_watch('us')$$);
select cron.schedule('fabot_close_watch_kr', '50 6 * * 1-5', $$select public.fabot_close_watch('kr')$$);
