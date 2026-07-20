"""정치뉴스 영상을 과거로 소급 수집해 outlet 간 표본 불균형을 해소하는 도구.

매일 24시간치만 모으는 collect.py와 별개의 일회성 도구.

설계 원칙 (과거 사고에서 얻은 것):
  1. 채널마다 **독립된 quota 예산**을 준다. 전체 공용 예산 하나만 뒀다가 한 채널(오마이TV)이
     예산을 독점해버려 나머지 채널은 시작도 못 한 사고가 있었음.
  2. quota 외에 **채널당 페이지 상한**도 둔다 (quota로 못 멈추는 경우 대비 2차 안전장치).
  3. 채널 조회는 outlet_name이 아니라 **CHANNELS의 channel_id로 직접** 한다.
     outlet_name은 DB에서 유일하지 않아 엉뚱한 채널을 긁은 사고가 있었음.
  4. 채널을 **순차가 아니라 병렬**로 처리한다 (순차 처리는 너무 느림).
     각 채널이 독립 예산을 쓰므로 스레드 간 공유 상태가 없어 경쟁 조건도 없다.
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

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
    sanitize_text,
)
from db import ensure_test_database

CHANNEL_WORKERS = 6  # 동시에 처리할 채널 수
VIDEO_WORKERS = 5  # 채널 내부에서 댓글을 동시에 조회할 영상 수 (총 스레드 6x5=30)
MAX_PAGES_PER_CHANNEL = 60  # quota로 못 멈추는 경우를 대비한 2차 안전장치 (60페이지 = 최대 3,000개 영상)

# 소급 수집 하한선: 1년보다 오래된 영상은 수집하지 않는다.
# 업로드 빈도가 낮은 시사 채널(YTN 시사 2.5개/일, KBS시사 4.6개/일)은 같은 예산으로도
# 과거로 훨씬 멀리 가버려서(실측 YTN 시사 638일치) 채널 간 수집 기간이 크게 어긋남.
# 기간을 1년으로 통일해 outlet 간 비교 가능성을 확보한다.
MAX_BACKFILL_AGE = timedelta(days=365)

# 채널별 quota 예산 (unit). 정치 비율이 낮아 많이 훑어야 하는 채널일수록 크게,
# 이미 표본이 충분한 채널은 작게 배분한다. 총합이 하루 한도(10,000)를 넘지 않도록 설계.
#   현재 정치 영상 보유량: TV조선 459 / 채널A 395 / JTBC 345 / MBN 305 / MBC 232
#                          연합뉴스TV 15 / YTN 12 / SBS 10 / KBS 4   <- 이쪽을 끌어올려야 함
#   (오마이TV 7,817은 이미 과다 수집돼 이번 대상에서 제외)
CHANNEL_BUDGETS = {
    # (outlet, channel_type) -> quota unit
    ("KBS News", "opinion"): 800,      # KBS시사 (정치 46%) - KBS 보강의 주력
    ("YTN", "opinion"): 800,           # YTN 시사 (48%) - YTN 보강의 주력
    ("SBS 뉴스", "opinion"): 800,       # SBS 시사교양 라디오 (70%) - SBS 보강의 주력
    ("연합뉴스TV", "news"): 900,        # 보강용 시사 채널이 없어 본 채널로만 채워야 함 (8%)
    ("KBS News", "news"): 350,         # 본 채널은 정치 비율이 낮아(3.8%) 효율이 나쁨 - 보조로만
    ("YTN", "news"): 350,              # (5.8%)
    ("SBS 뉴스", "news"): 350,          # (9.5%)
    ("MBC 뉴스", "news"): 300,          # 23.3%, 232건 보유 - 소폭 보강
    ("MBN News", "news"): 250,         # 30.7%, 305건 보유
    ("JTBC News", "news"): 250,        # 35.0%, 345건 보유
    ("채널A News", "news"): 200,        # 40.6%, 395건 보유
    ("TV조선", "news"): 200,            # 46.6%, 459건 보유 - 이미 가장 많음
}
EXCLUDED_OUTLETS = {"오마이TV"}  # 이미 7,817건으로 과다 수집됨


class QuotaTracker:
    """채널별 quota 예산 추적기. 스레드 안전.

    현재는 채널마다 하나씩 쓰여 스레드 간 공유되지 않지만, 나중에 채널 내부를 영상 단위로
    병렬화하면 여러 스레드가 같은 트래커를 건드리게 된다. 그때 두 가지 경쟁 조건이 생긴다:
      1) `used += 1`은 읽기·더하기·쓰기 3단계라 원자적이지 않아 증가분이 유실됨
         (실측: 간격이 벌어진 상황에서 5,000회 호출 중 4,444회 유실 -> 예산의 9배를 써버림)
      2) `if not exhausted(): spend()` 사이에 여러 스레드가 동시에 통과해 예산 초과
         (실측: 예산 100인데 109 사용)
    호출당 quota는 항상 1로 고정이라 비용 변동 때문이 아니라, 순전히 카운터 갱신 문제다.
    확인과 차감을 `try_spend()` 하나로 합쳐 락 안에서 처리하면 두 문제가 동시에 해결된다.
    """

    def __init__(self, budget):
        self.budget = budget
        self.used = 0
        self._lock = threading.Lock()

    def try_spend(self, units=1):
        """예산이 남아 있으면 차감하고 True, 부족하면 아무것도 하지 않고 False."""
        with self._lock:
            if self.used + units > self.budget:
                return False
            self.used += units
            return True

    def spend(self, units=1):
        """예산 확인 없이 차감 (이미 호출한 API 비용을 사후 기록할 때)."""
        with self._lock:
            self.used += units

    def exhausted(self):
        with self._lock:
            return self.used >= self.budget


def resolve_targets(conn):
    """CHANNELS(정답 매핑)를 기준으로 대상 채널과 예산을 확정한다.
    각 채널이 지금까지 어디까지 수집했는지(가장 오래된 published_at)도 함께 조회."""
    targets = []
    for outlet_name, keys in CHANNELS.items():
        if outlet_name in EXCLUDED_OUTLETS:
            continue
        for key_type, key_value, channel_type in keys:
            budget = CHANNEL_BUDGETS.get((outlet_name, channel_type))
            if not budget:
                continue
            targets.append(
                {
                    "outlet_name": outlet_name,
                    "key_type": key_type,
                    "key_value": key_value,
                    "channel_type": channel_type,
                    "budget": budget,
                }
            )
    return targets


def backfill_channel(api_key, db_pool, target):
    """채널 하나를 독립 예산으로 소급 수집한다 (스레드에서 실행)."""
    outlet_name = target["outlet_name"]
    tracker = QuotaTracker(target["budget"])
    youtube = make_youtube_client(api_key)
    conn = db_pool.getconn()
    conn.autocommit = True

    scanned = new_videos = comment_videos = 0
    try:
        # 채널 메타데이터 조회 후 등록 (channel_id 확정)
        from collect import fetch_channel

        channel = fetch_channel(youtube, target["key_type"], target["key_value"])
        tracker.spend(1)
        if channel is None:
            return {**target, "error": "채널을 찾을 수 없음", "used": tracker.used}

        channel_id = channel["id"]
        channel_title = channel["snippet"]["title"]
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channels (channel_id, outlet_name, title, uploads_playlist_id, channel_type)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                    SET title = EXCLUDED.title,
                        uploads_playlist_id = EXCLUDED.uploads_playlist_id,
                        channel_type = EXCLUDED.channel_type
                """,
                (channel_id, outlet_name, sanitize_text(channel_title), uploads, target["channel_type"]),
            )
            # 재개 지점은 backfill_cursor(실제로 훑은 가장 오래된 지점)를 쓴다.
            # MIN(published_at)을 쓰면 "저장된 정치 영상 중 가장 오래된 것"이라, 정치 영상이
            # 없는 구간을 훑고 지나가도 커서가 안 움직여서 매일 같은 구간을 다시 훑게 되고,
            # 최악의 경우 예산이 다 떨어질 때까지 정치 영상을 못 찾으면 영영 과거로 못 간다.
            # (커서가 아직 없는 기존 채널은 MIN(published_at)로 1회 폴백)
            cur.execute(
                """
                SELECT COALESCE(backfill_cursor, (SELECT MIN(published_at) FROM videos WHERE channel_id = %s))
                FROM channels WHERE channel_id = %s
                """,
                (channel_id, channel_id),
            )
            resume_from = cur.fetchone()[0]

        floor = datetime.now(timezone.utc) - MAX_BACKFILL_AGE

        # 이미 1년 하한선까지 훑은 채널은 더 볼 게 없다. playlistItems는 항상 최신순으로만
        # 페이지를 넘길 수 있어서(날짜로 바로 점프하는 파라미터가 없음) 재개 지점까지 가는 데만
        # 수십 페이지를 써야 하므로, 여기서 조기 종료해 그 낭비를 막는다.
        if resume_from is not None and resume_from <= floor:
            return {
                **target,
                "channel_title": channel_title,
                "scanned": 0,
                "new_videos": 0,
                "comment_videos": 0,
                "used": tracker.used,
                "cursor": resume_from,
                "reached_floor": True,
                "error": None,
            }

        page_token = None
        pages = 0
        oldest_scanned = resume_from
        reached_floor = False

        while not tracker.exhausted() and pages < MAX_PAGES_PER_CHANNEL and not reached_floor:
            resp = youtube.playlistItems().list(
                part="snippet", playlistId=uploads, maxResults=50, pageToken=page_token
            ).execute()
            tracker.spend(1)
            pages += 1

            items = resp.get("items", [])
            if not items:
                break

            # 이미 수집한 구간은 건너뛰고, 그보다 오래되면서 1년 하한선 안쪽인 것만 처리
            older = []
            for it in items:
                pub = datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z", "+00:00"))
                if resume_from is not None and pub >= resume_from:
                    continue
                if pub < floor:
                    # 업로드 재생목록은 최신순이므로 하한선을 만나면 이 채널은 더 볼 것이 없음
                    reached_floor = True
                    break
                older.append(it)
                if oldest_scanned is None or pub < oldest_scanned:
                    oldest_scanned = pub
            scanned += len(older)

            if older:
                vids = [x["snippet"]["resourceId"]["videoId"] for x in older]
                categories = fetch_video_categories(youtube, vids)
                tracker.spend((len(vids) + 49) // 50)

                # 이 페이지에서 필터를 통과한 영상만 추린다
                political = [
                    it
                    for it in older
                    if categories.get(it["snippet"]["resourceId"]["videoId"]) == "25"
                    and is_political_title(it["snippet"]["title"])
                ]

                # 영상 저장은 한 번에 일괄 삽입 (페이지당 DB 왕복 1회)
                if political:
                    video_rows = [
                        (
                            it["snippet"]["resourceId"]["videoId"],
                            channel_id,
                            sanitize_text(it["snippet"]["title"]),
                            it["snippet"]["publishedAt"],
                            "25",
                        )
                        for it in political
                    ]
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(
                            cur,
                            """
                            INSERT INTO videos (video_id, channel_id, title, published_at, category_id)
                            VALUES %s ON CONFLICT (video_id) DO NOTHING
                            """,
                            video_rows,
                        )
                    new_videos += len(political)

                # 댓글 조회는 영상 단위로 병렬 처리한다. 페이지네이션은 nextPageToken 때문에
                # 순차일 수밖에 없지만, 한 페이지에서 추려진 영상들의 댓글 조회는 서로 독립적이라
                # 동시에 보낼 수 있다. 이것이 채널당 처리량(직전 실측 2.44 unit/s)의 병목이었음.
                # quota는 제출 전에 try_spend로 미리 확보하므로 예산 초과가 발생하지 않는다.
                jobs = []
                for it in political:
                    if not tracker.try_spend(1):
                        break
                    jobs.append(it["snippet"]["resourceId"]["videoId"])

                if jobs:
                    with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as vex:
                        fetched = list(
                            vex.map(lambda vid: (vid, *fetch_top_comments(make_youtube_client(api_key), vid)), jobs)
                        )
                    comment_videos += len(fetched)

                    disabled = [vid for vid, _, err in fetched if err is not None]
                    if disabled:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE videos SET comments_disabled = TRUE WHERE video_id = ANY(%s)", (disabled,)
                            )

                    rows = [
                        build_comment_row(c, vid)
                        for vid, comments, err in fetched
                        if err is None and comments
                        for c in comments
                    ]
                    if rows:
                        with conn.cursor() as cur:
                            psycopg2.extras.execute_values(cur, COMMENT_INSERT_SQL, rows)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # 실제로 훑은 가장 오래된 지점을 커서로 저장 -> 다음 실행은 여기서 이어감
        if oldest_scanned is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE channels SET backfill_cursor = %s WHERE channel_id = %s",
                    (oldest_scanned, channel_id),
                )

        return {
            **target,
            "channel_title": channel_title,
            "scanned": scanned,
            "new_videos": new_videos,
            "comment_videos": comment_videos,
            "used": tracker.used,
            "cursor": oldest_scanned,
            "reached_floor": reached_floor,
            "error": None,
        }
    except Exception as e:
        return {**target, "error": f"{type(e).__name__}: {e}", "used": tracker.used}
    finally:
        db_pool.putconn(conn)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    start = time.perf_counter()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key or not database_url:
        raise SystemExit("YOUTUBE_API_KEY / DATABASE_URL 환경변수가 필요합니다.")

    test_database_url = ensure_test_database(database_url)
    # minconn = maxconn으로 둔다. psycopg2의 putconn은 보유 연결이 minconn 이상이면 반납된 연결을
    # 보관하지 않고 close()하므로, minconn이 작으면 매번 새 연결을 만들게 된다
    # (Neon까지 새 연결 생성에 평균 1.7초 — collect.py에서 최대 병목이었음).
    pool_size = CHANNEL_WORKERS + 4
    db_pool = ThreadedConnectionPool(pool_size, pool_size, test_database_url)

    conn = db_pool.getconn()
    conn.autocommit = True
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO outlets (outlet_name) VALUES %s ON CONFLICT DO NOTHING",
            [(name,) for name in CHANNELS],
        )
    targets = resolve_targets(conn)
    db_pool.putconn(conn)

    total_budget = sum(t["budget"] for t in targets)
    print(f"대상 채널 {len(targets)}개 (오마이TV 제외), 총 예산 {total_budget:,} unit, 동시 {CHANNEL_WORKERS}개 처리\n")

    results = []
    with ThreadPoolExecutor(max_workers=CHANNEL_WORKERS) as ex:
        futures = [ex.submit(backfill_channel, api_key, db_pool, t) for t in targets]
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            if r.get("error"):
                print(f"  [실패] {r['outlet_name']}({r['channel_type']}): {r['error']} (사용 {r['used']})")
            else:
                floor_mark = " [1년 하한 도달]" if r.get("reached_floor") else ""
                cur_date = r["cursor"].date() if r.get("cursor") else "-"
                print(
                    f"  {r['outlet_name']}({r['channel_type']}) {r['channel_title'][:18]:20} "
                    f"신규 {r['new_videos']:>4} / 훑음 {r['scanned']:>5} / quota {r['used']:>4} / 커서 {cur_date}{floor_mark}"
                )

    db_pool.closeall()
    used = sum(r["used"] for r in results)
    new = sum(r.get("new_videos", 0) for r in results)
    print(f"\n=== 완료: 신규 정치영상 {new:,}건, quota 사용 약 {used:,} unit, {time.perf_counter()-start:.0f}초 ===")


if __name__ == "__main__":
    main()
