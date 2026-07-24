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
from datetime import datetime, timezone

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
    select_comments,
)
from db import ensure_test_database

CHANNEL_WORKERS = 6  # 동시에 처리할 채널 수
VIDEO_WORKERS = 5  # 채널 내부에서 댓글을 동시에 조회할 영상 수 (총 스레드 6x5=30)
MAX_PAGES_PER_CHANNEL = 600  # 2차 안전장치. 페이지도 quota에서 차감(tracker.spend)되므로
# 실제 제한은 예산이고, 이 값은 예산보다 크게 둬서 "예산이 먼저 소진되도록" 한다.
# (너무 작으면 예산을 다 쓰기 전에 페이지 상한에 걸려 소급이 얕게 끝남)

# 소급 수집 하한선. prune.py와 **같은 값을 공유**한다.
# 하한을 두 곳에 따로 적으면 backfill이 긁어온 구간을 prune이 곧바로 지우거나 그 반대가 되므로,
# 반드시 한 곳에서만 정의한다.
#
# 1년(365일)에서 3개월 고정 날짜로 바꾼 이유:
#   무료 티어 512MB에서 1년치는 물리적으로 불가능하다. 실측으로 영상 1편 = 약 8.5KB인데
#   news 채널은 월 700~1,850편을 올린다. 9개 채널을 1년까지 채우면 12만~14만 편(1.3GB급)이라
#   담을 수 있는 최대치의 4배다. 쿼터가 아니라 저장 용량이 먼저 터진다.
#   목적이 '채널 간 비교'인 이상 깊이보다 **모든 채널이 같은 기간을 커버하는 것**이 중요하므로,
#   전 채널 공통 하한을 2026-04-21(약 3개월)로 통일했다.
from prune import RETENTION_FLOOR

BACKFILL_FLOOR = datetime.fromisoformat(RETENTION_FLOOR).replace(tzinfo=timezone.utc)

# 한 번 실행에 쓸 총 quota. 일일 수집분(약 300~500 unit)을 남기려고 8,000 아래로 둔다.
#   ※ 페이지·카테고리·댓글이 모두 예산에서 차감되므로 이 숫자가 곧 실제 quota 소모 상한이다.
TOTAL_BUDGET = 7920

# 대상 채널 (outlet, channel_type). 예산은 실행할 때마다 **남은 기간에 따라 자동 배분**한다.
#
# 대상에서 빠진 채널:
#   - KBS시사·YTN시사·SBS시교라(opinion): 이미 하한까지 내려가 있다.
#   - 오마이TV: 과다 수집(프루닝 후에도 4,054편)이라 EXCLUDED_OUTLETS로 제외.
TARGET_CHANNELS = {
    ("연합뉴스TV", "news"), ("KBS News", "news"), ("YTN", "news"),
    ("SBS 뉴스", "news"), ("MBC 뉴스", "news"), ("MBN News", "news"),
    ("JTBC News", "news"), ("채널A News", "news"), ("TV조선", "news"),
}
EXCLUDED_OUTLETS = {"오마이TV"}  # 프루닝 후에도 4,054편으로 다른 채널의 2~4배

# 하루치 소급에 드는 quota. 실측(2026-07-24): 7,373 unit으로 9채널 합계 100일 진행 = 74 unit/일.
# 채널별로 58~98로 갈리므로(영상 밀도 차이) 여유를 둬 90으로 잡는다.
UNITS_PER_DAY = 90

# 채널당 최소 예산. playlistItems는 날짜로 점프할 수 없어 **매 실행마다 1페이지부터 다시 넘겨**
# 커서 위치까지 도달해야 한다. 커서가 깊을수록 이 통과 비용만 100 unit을 넘으므로,
# 이보다 적게 주면 커서에 닿기도 전에 예산이 끝나 아무 진전이 없다.
MIN_CHANNEL_BUDGET = 300


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


def resolve_targets(conn, total_budget=TOTAL_BUDGET):
    """대상 채널을 고르고 **남은 기간에 비례해 예산을 자동 배분**한다.

    왜 자동인가:
      예전에는 채널마다 880씩 손으로 적어 뒀는데, 하한에 도달한 채널이 생기면 그 몫이
      그대로 놀았다(완주 채널은 1 unit만 쓰고 조기 종료하므로). 2026-07-24에 SBS·MBN이
      완주해 다음 실행부터 1,758 unit이 낭비될 상황이었다. 남은 기간을 DB에서 읽어
      배분하면 상수를 매번 손볼 필요가 없고 낭비도 없다.

    배분 방식 — 완주에 가까운 채널부터 필요량을 채운다:
      1. 활성 채널마다 MIN_CHANNEL_BUDGET을 먼저 깔아 준다(커서까지 통과하는 비용).
      2. 남은 예산을 **필요량이 적은 채널부터** 순서대로 채운다.
      기간이 짧게 남은 채널을 먼저 끝내면 다음 실행부터 그 채널의 통과 비용이 통째로
      사라진다. 균등 배분은 모두를 어중간하게 남겨 그 낭비가 계속된다.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT outlet_name, channel_type,
               GREATEST((backfill_cursor::date - %s::date), 0) AS days_left
        FROM channels
        """,
        (RETENTION_FLOOR,),
    )
    # 커서가 없는 채널(아직 한 번도 안 훑음)은 전 구간이 남은 것으로 본다.
    full_span = (datetime.now(timezone.utc).date() - BACKFILL_FLOOR.date()).days
    days_left = {(o, t): (d if d is not None else full_span) for o, t, d in cur.fetchall()}

    active = []
    for outlet_name, keys in CHANNELS.items():
        if outlet_name in EXCLUDED_OUTLETS:
            continue
        for key_type, key_value, channel_type in keys:
            if (outlet_name, channel_type) not in TARGET_CHANNELS:
                continue
            left = days_left.get((outlet_name, channel_type), full_span)
            if left <= 0:
                continue  # 이미 하한 도달 — 예산을 주지 않는다
            active.append(
                {
                    "outlet_name": outlet_name,
                    "key_type": key_type,
                    "key_value": key_value,
                    "channel_type": channel_type,
                    "days_left": left,
                    "need": max(MIN_CHANNEL_BUDGET, left * UNITS_PER_DAY),
                }
            )

    if not active:
        return []

    # 1) 최소 예산을 모두에게 깔고, 2) 남은 것을 필요량이 적은 순서로 채운다.
    base = min(MIN_CHANNEL_BUDGET, total_budget // len(active))
    for t in active:
        t["budget"] = base
    remaining = total_budget - base * len(active)
    for t in sorted(active, key=lambda x: x["need"]):
        if remaining <= 0:
            break
        top_up = min(t["need"] - t["budget"], remaining)
        if top_up > 0:
            t["budget"] += top_up
            remaining -= top_up

    return active


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

        floor = BACKFILL_FLOOR

        # 이미 하한선까지 훑은 채널은 더 볼 게 없다. playlistItems는 항상 최신순으로만
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

            # 이미 수집한 구간은 건너뛰고, 그보다 오래되면서 하한선 안쪽인 것만 처리
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
                            True,  # 위 political 리스트가 이미 2차 필터를 통과한 것들
                        )
                        for it in political
                    ]
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(
                            cur,
                            """
                            INSERT INTO videos (
                                video_id, channel_id, title, published_at, category_id, is_political
                            )
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
                        for c in select_comments(comments)  # 영상당 20건만 저장 (collect.py 참고)
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

    if not targets:
        print(f"모든 대상 채널이 하한 {RETENTION_FLOOR}에 도달했습니다. 소급할 것이 없습니다.")
        db_pool.closeall()
        return

    total_budget = sum(t["budget"] for t in targets)
    print(f"대상 채널 {len(targets)}개 (하한 도달·제외 채널 빼고), 총 예산 {total_budget:,} unit,"
          f" 동시 {CHANNEL_WORKERS}개 처리")
    print(f"  {'채널':<22}{'남은일':>7}{'배정':>8}")
    for t in sorted(targets, key=lambda x: x["days_left"]):
        print(f"  {t['outlet_name'] + '/' + t['channel_type']:<22}"
              f"{t['days_left']:>7}{t['budget']:>8}")
    print()

    results = []
    with ThreadPoolExecutor(max_workers=CHANNEL_WORKERS) as ex:
        futures = [ex.submit(backfill_channel, api_key, db_pool, t) for t in targets]
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            if r.get("error"):
                print(f"  [실패] {r['outlet_name']}({r['channel_type']}): {r['error']} (사용 {r['used']})")
            else:
                floor_mark = f" [하한 {RETENTION_FLOOR} 도달]" if r.get("reached_floor") else ""
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
