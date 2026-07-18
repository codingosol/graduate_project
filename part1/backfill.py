import os
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from psycopg2.pool import ThreadedConnectionPool

from collect import (
    CHANNELS,
    COMMENT_INSERT_SQL,
    build_comment_row,
    fetch_top_comments,
    fetch_video_categories,
    is_political_title,
    make_youtube_client,
)
from db import ensure_test_database

# 2차 실행에서 남은 TV조선(뉴스TVCHOSUN)만 다시 처리.
# 버그: outlet_name='TV조선'이 channels 테이블에 두 행(뉴스TVCHOSUN 정상 + TVCHOSUN 제외된 잔여) 있어서
# outlet_name만으로 조회하면 잘못된 채널이 뽑힐 수 있음 -> collect.py의 CHANNELS(정답)를 기준으로 channel_id를 직접 지정.
TARGET_OUTLETS = ["TV조선"]

PER_CHANNEL_QUOTA_BUDGET = 450
MAX_PAGES_PER_CHANNEL = 20  # quota로도 못 멈추는 경우를 대비한 2차 안전장치 (20페이지 = 최대 1,000개 영상)


class QuotaTracker:
    def __init__(self, budget):
        self.budget = budget
        self.used = 0

    def spend(self, units=1):
        self.used += units

    def exhausted(self):
        return self.used >= self.budget


def get_target_channels(conn):
    """outlet_name만으로 조회하면 같은 이름을 가진 잔여(제외된) 채널 행과 충돌할 수 있으므로,
    collect.py의 CHANNELS(정답 매핑)에서 channel_id를 직접 얻어 그 채널 하나만 조회한다."""
    results = []
    for outlet_name in TARGET_OUTLETS:
        key_type, key_value = CHANNELS[outlet_name][0]
        if key_type != "id":
            raise ValueError(
                f"{outlet_name}은 CHANNELS에 channel_id가 아니라 {key_type}로 등록돼 있어 "
                "channels 테이블에서 channel_id로 바로 조회할 수 없음 - CHANNELS 값을 확인할 것"
            )
        channel_id = key_value
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.outlet_name, c.channel_id, c.uploads_playlist_id, MIN(v.published_at)
                FROM channels c LEFT JOIN videos v ON v.channel_id = c.channel_id
                WHERE c.channel_id = %s
                GROUP BY c.outlet_name, c.channel_id, c.uploads_playlist_id
                """,
                (channel_id,),
            )
            row = cur.fetchone()
        if row:
            results.append(row)
    return results


def backfill_channel(youtube, conn, tracker, outlet_name, channel_id, uploads_playlist_id, oldest_known):
    new_video_count = 0
    political_count = 0
    page_token = None
    page_count = 0

    while not tracker.exhausted() and page_count < MAX_PAGES_PER_CHANNEL:
        resp = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist_id, maxResults=50, pageToken=page_token
        ).execute()
        tracker.spend(1)
        page_count += 1

        items = resp.get("items", [])
        if not items:
            break

        # 이미 수집된 구간(oldest_known 이후)은 건너뛰고, 그보다 오래된 것만 신규 처리
        older_items = []
        for item in items:
            published_at = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
            if oldest_known is not None and published_at >= oldest_known:
                continue
            older_items.append(item)

        if older_items:
            video_ids = [it["snippet"]["resourceId"]["videoId"] for it in older_items]
            categories = fetch_video_categories(youtube, video_ids)
            tracker.spend((len(video_ids) + 49) // 50)

            for item in older_items:
                video_id = item["snippet"]["resourceId"]["videoId"]
                title = item["snippet"]["title"]
                category_id = categories.get(video_id)
                published_at = item["snippet"]["publishedAt"]

                # 필터를 통과한 영상만 저장한다. (예전엔 필터와 무관하게 전부 INSERT해서
                # 분석에 안 쓰는 비정치 영상 4,677건이 DB에 쌓였음)
                if category_id != "25" or not is_political_title(title):
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO videos (video_id, channel_id, title, published_at, category_id)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (video_id) DO NOTHING
                        """,
                        (video_id, channel_id, title, published_at, category_id),
                    )
                new_video_count += 1

                if not tracker.exhausted():
                    comments, error_reason = fetch_top_comments(youtube, video_id)
                    tracker.spend(1)
                    political_count += 1

                    if error_reason is not None:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE videos SET comments_disabled = TRUE WHERE video_id = %s", (video_id,)
                            )
                    elif comments:
                        rows = [build_comment_row(c, video_id) for c in comments]
                        with conn.cursor() as cur:
                            psycopg2.extras.execute_values(cur, COMMENT_INSERT_SQL, rows)

                if tracker.exhausted():
                    break

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    print(
        f"  {outlet_name}: 신규 영상 {new_video_count}개 처리, 그중 정치뉴스(댓글수집) {political_count}개 "
        f"(누적 사용 quota 약 {tracker.used} unit)"
    )
    return new_video_count, political_count


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key or not database_url:
        raise SystemExit("YOUTUBE_API_KEY / DATABASE_URL 환경변수가 필요합니다.")

    test_database_url = ensure_test_database(database_url)
    db_pool = ThreadedConnectionPool(1, 4, test_database_url)

    conn = db_pool.getconn()
    conn.autocommit = True

    targets = get_target_channels(conn)
    print(
        f"대상 outlet {len(targets)}개(비율 순위 순), 채널당 quota 상한 {PER_CHANNEL_QUOTA_BUDGET} unit "
        f"+ 페이지 상한 {MAX_PAGES_PER_CHANNEL}개\n"
    )

    youtube = make_youtube_client(api_key)

    total_new = 0
    total_political = 0
    total_used = 0
    for outlet_name, channel_id, uploads_playlist_id, oldest_known in targets:
        tracker = QuotaTracker(PER_CHANNEL_QUOTA_BUDGET)  # 채널마다 독립된 예산 (한 채널이 전체를 독점 못 하도록)
        n, p = backfill_channel(youtube, conn, tracker, outlet_name, channel_id, uploads_playlist_id, oldest_known)
        total_new += n
        total_political += p
        total_used += tracker.used

    db_pool.putconn(conn)
    db_pool.closeall()

    print(f"\n=== 완료: 신규 영상 {total_new}개, 정치뉴스(댓글수집) {total_political}개, 총 quota 사용량 약 {total_used} unit ===")


if __name__ == "__main__":
    main()
