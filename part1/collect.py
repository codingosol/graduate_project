import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from psycopg2.pool import ThreadedConnectionPool

from db import ensure_test_database
from keywords import load_political_keywords

POLITICAL_KEYWORDS, FOREIGN_LEADER_EXCLUDE = load_political_keywords()

RECENT_WINDOW = timedelta(hours=24)
CHANNEL_WORKERS = 5  # 1단계: 채널별 메타데이터/영상목록 조회 동시 실행 수
VIDEO_WORKERS = 10  # 2단계: 영상별 댓글 조회 동시 실행 수
DB_POOL_SIZE = VIDEO_WORKERS + 4  # ThreadedConnectionPool은 풀이 바닥나면 대기 없이 바로 예외를 던지므로,
# 동시 작업자 수(VIDEO_WORKERS)보다 넉넉하게 잡아야 함. Neon 무료 티어 direct 한도(~100)엔 충분히 여유 있음.

# outlet_name -> [(key_type, key_value), ...]
# key_type: "handle"(@핸들) | "id"(채널ID) | "username"(레거시 /user/ 이름)
# 실제 채널과 다르면([실패] 로그로 확인) 여기 값만 고치면 됨.
# TV조선은 원래 계열사 채널 2개(TVCHOSUN+뉴스TVCHOSUN)였으나, TVCHOSUN(메인)은 예능/생활 콘텐츠
# 위주라 정치뉴스 수집 효율을 위해 제외 — 뉴스 전용인 뉴스TVCHOSUN만 수집 대상으로 유지.
CHANNELS = {
    "TV조선": [("id", "UCWlV3Lz_55UaX4JsMj-z__Q")],  # 뉴스TVCHOSUN
    "MBC 뉴스": [("username", "MBCNEWS")],
    "YTN": [("handle", "@ytnnews24")],
    "SBS 뉴스": [("id", "UCkinYTS9IHqOEwR1Sze2JTw")],
    "JTBC News": [("handle", "@jtbc_news")],
    "KBS News": [("handle", "@newskbs")],
    "채널A News": [("handle", "@channelA-news")],
    "MBN News": [("id", "UCG9aFJTZ-lMCHAiO1KJsirg")],
    "오마이TV": [("id", "UClAfLVQYZSLrMAQQ_SXPVZw")],
    "연합뉴스TV": [("handle", "@yonhapnewstv23")],
}


def make_youtube_client(api_key):
    # googleapiclient의 내부 http 객체는 스레드 간 공유가 안전하지 않으므로
    # 스레드(작업)마다 새로 생성한다. 네트워크 호출 없이 로컬에서 생성되는 작업이라 비용은 없음.
    return build("youtube", "v3", developerKey=api_key)


def fetch_channel(youtube, key_type, key_value):
    kwargs = {"part": "snippet,contentDetails"}
    if key_type == "handle":
        kwargs["forHandle"] = key_value
    elif key_type == "username":
        kwargs["forUsername"] = key_value
    elif key_type == "id":
        kwargs["id"] = key_value
    resp = youtube.channels().list(**kwargs).execute()
    items = resp.get("items", [])
    return items[0] if items else None


def fetch_category_names(youtube, region_code="KR"):
    resp = youtube.videoCategories().list(part="snippet", regionCode=region_code).execute()
    return {item["id"]: item["snippet"]["title"] for item in resp.get("items", [])}


def fetch_recent_videos(youtube, uploads_playlist_id, cutoff):
    """업로드 재생목록은 최신순이므로, cutoff보다 오래된 영상을 만나는 즉시 멈춘다."""
    videos = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        stop = False
        for item in resp.get("items", []):
            published_at = datetime.fromisoformat(
                item["snippet"]["publishedAt"].replace("Z", "+00:00")
            )
            if published_at < cutoff:
                stop = True
                break
            videos.append(item)

        page_token = resp.get("nextPageToken")
        if stop or not page_token:
            break
    return videos


def fetch_video_categories(youtube, video_ids):
    """videos.list로 categoryId를 배치(최대 50개)로 조회."""
    categories = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            categories[item["id"]] = item["snippet"].get("categoryId")
    return categories


def is_political_title(title):
    """제목에 해외 정상 이름이 있으면 무조건 비정치 처리(트럼프 등 국제정치 오탐 방지),
    그 외엔 정치 키워드(정당명/기구·용어/한자 축약/정치인 이름) 중 하나라도 포함되면 정치로 판단.
    실제 데이터(category=25, 1047건)로 검증: 매치율 12.7%, 해외 정상 오탐 없음."""
    if any(fl in title for fl in FOREIGN_LEADER_EXCLUDE):
        return False
    return any(kw in title for kw in POLITICAL_KEYWORDS)


def apply_filters(recent_items, categories):
    """1차 필터: categoryId가 25(News & Politics)가 아니면 제외.
    2차 필터: 제목에 국내 정당정치 키워드가 없으면 제외 (keywords.json/keywords.py 참고)."""
    filtered = []
    for item in recent_items:
        video_id = item["snippet"]["resourceId"]["videoId"]
        title = item["snippet"]["title"]
        if categories.get(video_id) != "25":
            continue
        if not is_political_title(title):
            continue
        filtered.append(item)
    return filtered


def fetch_top_comments(youtube, video_id, max_results=100):
    """댓글 좋아요 랭킹(likeCount) 직접 정렬 파라미터는 API에 없고, order='relevance'가
    유튜브 자체의 '인기 댓글' 순서에 가장 가까움. maxResults=100으로 1페이지만 받아
    영상당 쿼터를 1 unit으로 고정(댓글 폭증 영상도 추가 페이지네이션 없음)."""
    try:
        resp = youtube.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance", maxResults=max_results
        ).execute()
    except HttpError as e:
        reason = json.loads(e.content)["error"]["errors"][0]["reason"]
        return None, reason
    return resp.get("items", []), None


def process_channel(api_key, outlet_name, key_type, key_value, cutoff):
    """1단계 작업 (스레드에서 실행): 채널 메타데이터 + 최근 영상 목록 + 카테고리 조회."""
    try:
        youtube = make_youtube_client(api_key)
        channel = fetch_channel(youtube, key_type, key_value)
        if channel is None:
            return {
                "outlet_name": outlet_name,
                "key_type": key_type,
                "key_value": key_value,
                "error": "채널을 찾을 수 없음",
            }

        channel_id = channel["id"]
        channel_title = channel["snippet"]["title"]
        uploads_playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]

        recent_items = fetch_recent_videos(youtube, uploads_playlist_id, cutoff)
        video_ids = [it["snippet"]["resourceId"]["videoId"] for it in recent_items]
        categories = fetch_video_categories(youtube, video_ids) if video_ids else {}
        recent_items = apply_filters(recent_items, categories)

        return {
            "outlet_name": outlet_name,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "uploads_playlist_id": uploads_playlist_id,
            "recent_items": recent_items,
            "categories": categories,
            "error": None,
        }
    except Exception as e:
        return {
            "outlet_name": outlet_name,
            "key_type": key_type,
            "key_value": key_value,
            "error": f"예외 발생: {e}",
        }


def process_video(api_key, db_pool, channel_id, item, category_id, category_name):
    """2단계 작업 (스레드에서 실행): 영상 1건의 댓글 수집 + DB 저장(영상 1행 + 댓글 일괄 삽입)."""
    video_id = item["snippet"]["resourceId"]["videoId"]
    title = item["snippet"]["title"]
    published_at = item["snippet"]["publishedAt"]
    label = f"{category_id} {category_name}"

    conn = db_pool.getconn()
    try:
        conn.autocommit = True
        youtube = make_youtube_client(api_key)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (video_id, channel_id, title, published_at, category_id, raw)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (video_id) DO NOTHING
                """,
                (video_id, channel_id, title, published_at, category_id, psycopg2.extras.Json(item)),
            )

        comments, error_reason = fetch_top_comments(youtube, video_id)

        if error_reason is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE videos SET comments_disabled = TRUE WHERE video_id = %s",
                    (video_id,),
                )
            return {"title": title, "label": label, "comment_count": 0, "error": error_reason}

        if comments:
            rows = []
            for c in comments:
                top = c["snippet"]["topLevelComment"]
                snippet = top["snippet"]
                rows.append(
                    (
                        top["id"],
                        video_id,
                        snippet["textDisplay"],
                        snippet["publishedAt"],
                        snippet["likeCount"],
                        psycopg2.extras.Json(c),
                    )
                )
            # 댓글 개수만큼 INSERT를 따로 보내지 않고 한 번에 일괄 삽입 (영상당 DB 왕복 1회)
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO comments (comment_id, video_id, text, published_at, like_count, raw)
                    VALUES %s
                    ON CONFLICT (comment_id) DO NOTHING
                    """,
                    rows,
                )

        return {"title": title, "label": label, "comment_count": len(comments), "error": None}
    finally:
        db_pool.putconn(conn)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    start_time = time.perf_counter()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key or not database_url:
        raise SystemExit("YOUTUBE_API_KEY / DATABASE_URL 환경변수가 필요합니다.")

    print("=== 테스트 DB 준비 (운영 DB에는 적재하지 않음) ===")
    test_database_url = ensure_test_database(database_url)

    bootstrap_youtube = make_youtube_client(api_key)
    category_names = fetch_category_names(bootstrap_youtube)

    now = datetime.now(timezone.utc)
    cutoff = now - RECENT_WINDOW
    print(f"\n수집 기준 시각(UTC): {now.isoformat()}")
    print(f"24시간 컷오프(UTC): {cutoff.isoformat()}")

    db_pool = ThreadedConnectionPool(1, DB_POOL_SIZE, test_database_url)

    conn = db_pool.getconn()
    conn.autocommit = True
    with conn.cursor() as cur:
        for outlet_name in CHANNELS:
            cur.execute(
                "INSERT INTO outlets (outlet_name) VALUES (%s) ON CONFLICT DO NOTHING",
                (outlet_name,),
            )
    db_pool.putconn(conn)

    # ---- 1단계: 채널별 메타데이터 + 최근 영상 목록 (채널 단위 병렬) ----
    channel_jobs = [
        (outlet_name, key_type, key_value)
        for outlet_name, keys in CHANNELS.items()
        for key_type, key_value in keys
    ]

    print(f"\n[1단계] 채널 {len(channel_jobs)}개 메타데이터/영상목록 조회 (동시 {CHANNEL_WORKERS}개)")
    channel_results = []
    with ThreadPoolExecutor(max_workers=CHANNEL_WORKERS) as executor:
        futures = [
            executor.submit(process_channel, api_key, outlet_name, key_type, key_value, cutoff)
            for outlet_name, key_type, key_value in channel_jobs
        ]
        for future in as_completed(futures):
            channel_results.append(future.result())

    conn = db_pool.getconn()
    conn.autocommit = True
    video_jobs = []
    for result in channel_results:
        if result["error"] is not None:
            print(f"  [실패] {result['outlet_name']} ({result['key_type']}={result['key_value']}): {result['error']}")
            continue

        print(
            f"  채널: {result['channel_title']} ({result['channel_id']}) "
            f"- outlet: {result['outlet_name']} - 24시간 내 {len(result['recent_items'])}개"
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channels (channel_id, outlet_name, title, uploads_playlist_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                    SET title = EXCLUDED.title,
                        uploads_playlist_id = EXCLUDED.uploads_playlist_id
                """,
                (result["channel_id"], result["outlet_name"], result["channel_title"], result["uploads_playlist_id"]),
            )

        for item in result["recent_items"]:
            video_id = item["snippet"]["resourceId"]["videoId"]
            category_id = result["categories"].get(video_id)
            category_name = category_names.get(category_id, "?")
            video_jobs.append((result["channel_id"], item, category_id, category_name))
    db_pool.putconn(conn)

    # ---- 2단계: 전체 채널의 영상을 한 리스트로 모아 영상 단위 병렬 처리 ----
    print(f"\n[2단계] 영상 {len(video_jobs)}개 댓글 수집 (동시 {VIDEO_WORKERS}개)\n")
    with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as executor:
        futures = [
            executor.submit(process_video, api_key, db_pool, channel_id, item, category_id, category_name)
            for channel_id, item, category_id, category_name in video_jobs
        ]
        for future in as_completed(futures):
            r = future.result()
            status = f"댓글 {r['comment_count']}개" if r["error"] is None else f"실패({r['error']})"
            print(f"  - [{r['label']}] {r['title']} → {status}")

    db_pool.closeall()
    elapsed = time.perf_counter() - start_time
    print(f"\n=== 완료: collect_test DB에 저장됨 (운영 DB 아님) — 총 소요시간 {elapsed:.1f}초 ===")


if __name__ == "__main__":
    main()
