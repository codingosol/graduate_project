-- 채널이 "재생목록 물리 한계"에 도달했음을 표시하는 플래그.
--
-- 왜 필요한가:
--   YouTube playlistItems는 업로드 재생목록을 최근 약 20,000편(=400페이지)까지만
--   페이지네이션한다. YTN(97만 편)·연합뉴스TV(71만 편)처럼 업로드가 많은 채널은
--   최근 20,000편이 하한선(2026-04-21)에 못 미치는 지점(YTN 04-24, 연합 05-05)까지밖에
--   닿지 않아, 그 너머는 API로 접근이 불가능하다.
--
--   backfill_cursor만으로는 이 상태를 "하한 미도달(계속 백필 대상)"과 구분할 수 없어,
--   매 실행마다 400페이지를 헛되이 넘기며 quota 약 401 unit을 낭비한다(실측).
--   이 플래그가 TRUE면 resolve_targets가 해당 채널을 대상에서 제외한다.
--
-- reached_floor(하한 도달)와는 다르다: 그쪽은 커서 <= 하한이라 이미 자연히 제외되고,
-- 이 플래그는 커서 > 하한인데도 더 못 내려가는 경우(=API 한계)를 표시한다.

ALTER TABLE channels ADD COLUMN backfill_exhausted BOOLEAN NOT NULL DEFAULT FALSE;
