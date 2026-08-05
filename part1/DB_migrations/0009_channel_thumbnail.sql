-- 채널 로고(썸네일) URL. 대시보드에서 채널 옆에 로고를 표시하기 위함.
-- YouTube channels.list(part=snippet).thumbnails 로 1회 채운다(viz/fetch_logos 참고).
ALTER TABLE channels ADD COLUMN thumbnail_url TEXT;
