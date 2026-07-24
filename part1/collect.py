import hashlib
import json
import os
import sys
import threading
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
from keywords import load_keyword_groups, normalize

DOMESTIC_KEYWORDS, GENERIC_KEYWORDS, FOREIGN_LEADER_EXCLUDE = load_keyword_groups()

# 수집 재개 지점(cutoff)은 "고정된 24시간"이 아니라 **채널별로 DB에 저장된 가장 최신 영상의
# published_at**을 기준으로 계산한다. 이렇게 하면 실행이 며칠 밀리거나 한 번 건너뛰어도
# 그 사이 구간을 자동으로 따라잡는다(고정 24시간 창은 실행 간격이 24시간을 넘는 순간 영구 공백이 생김).
# 별도의 "수집 시각" 기록은 두지 않는다 — published_at이 이미 그 정보를 담고 있다.
#
# ⚠️ 반드시 채널별로 계산해야 한다. 업로드 빈도가 채널마다 크게 달라(MBC 뉴스 vs YTN 시사),
#    전체 MAX 하나를 공용 cutoff로 쓰면 고빈도 채널이 저빈도 채널의 재개 지점을 덮어
#    저빈도 채널이 놓친 구간을 영영 못 긁는다.
DEFAULT_WINDOW = timedelta(hours=24)   # 아직 저장된 정치 영상이 없는 채널의 fallback 창
OVERLAP = timedelta(hours=2)           # 재개 시 경계 누락을 막기 위해 살짝 겹쳐 훑는 여유
MAX_LOOKBACK = timedelta(days=7)       # 재개 구간 상한. 이보다 벌어지면 backfill의 영역이라 경고
CHANNEL_WORKERS = 5  # 1단계: 채널별 메타데이터/영상목록 조회 동시 실행 수
VIDEO_WORKERS = 10  # 2단계: 영상별 댓글 조회 동시 실행 수
DB_POOL_SIZE = VIDEO_WORKERS + 4  # ThreadedConnectionPool은 풀이 바닥나면 대기 없이 바로 예외를 던지므로,
# 동시 작업자 수(VIDEO_WORKERS)보다 넉넉하게 잡아야 함. Neon 무료 티어 direct 한도(~100)엔 충분히 여유 있음.

# outlet_name -> [(key_type, key_value, channel_type), ...]
# key_type: "handle"(@핸들) | "id"(채널ID) | "username"(레거시 /user/ 이름)
# channel_type: "news"(스트레이트 뉴스) | "opinion"(시사·논평·라디오 프로그램)
# 실제 채널과 다르면([실패] 로그로 확인) 여기 값만 고치면 됨.
#
# TV조선은 원래 계열사 채널 2개(TVCHOSUN+뉴스TVCHOSUN)였으나, TVCHOSUN(메인)은 예능/생활 콘텐츠
# 위주라 정치뉴스 수집 효율을 위해 제외 — 뉴스 전용인 뉴스TVCHOSUN만 수집 대상으로 유지.
#
# SBS/KBS/YTN은 24시간 종합뉴스 채널이라 날씨·사건사고 비중이 커서 정치 비율이 극히 낮음
# (실측 SBS 9.5% / KBS 3.8% / YTN 5.8%). 그래서 각 언론사의 시사 전문 채널을 보강용으로 추가
# (실측 SBS 시사교양 70% / KBS시사 46% / YTN 시사 48%). 나머지 outlet은 이미 20%를 넘어 추가 안 함.
# 이 3개는 논평 성격이라 channel_type='opinion'으로 구분해 저장 — 분석 시 장르를 나눠 볼 수 있게.
CHANNELS = {
    "TV조선": [("id", "UCWlV3Lz_55UaX4JsMj-z__Q", "news")],  # 뉴스TVCHOSUN
    "MBC 뉴스": [("username", "MBCNEWS", "news")],
    "YTN": [
        ("handle", "@ytnnews24", "news"),
        ("id", "UCvWUqUT10RiJ6W8XiruqAiQ", "opinion"),  # YTN 시사
    ],
    "SBS 뉴스": [
        ("id", "UCkinYTS9IHqOEwR1Sze2JTw", "news"),
        ("id", "UCLv3v82YNNsa8EsxrcPMjGQ", "opinion"),  # SBS 시사교양 라디오(시교라)
    ],
    "JTBC News": [("handle", "@jtbc_news", "news")],
    "KBS News": [
        ("handle", "@newskbs", "news"),
        ("id", "UCEb31RoX5RnfYENmnyokN8A", "opinion"),  # KBS시사
    ],
    "채널A News": [("handle", "@channelA-news", "news")],
    "MBN News": [("id", "UCG9aFJTZ-lMCHAiO1KJsirg", "news")],
    "오마이TV": [("id", "UClAfLVQYZSLrMAQQ_SXPVZw", "news")],
    "연합뉴스TV": [("handle", "@yonhapnewstv23", "news")],
}


_thread_local = threading.local()


def make_youtube_client(api_key):
    """스레드마다 클라이언트를 하나씩만 만들어 재사용한다.

    googleapiclient의 내부 http 객체는 스레드 간 공유가 안전하지 않아 스레드별로 따로 만들어야 하지만,
    예전엔 작업(영상)마다 새로 만들고 있었음. 객체 생성 자체는 3ms로 싸도, 새 객체는 곧 새 HTTP 연결이라
    호출마다 TLS 핸드셰이크가 새로 일어나 API 지연이 크게 늘어났음
    (실측: 클라이언트 재사용 116ms vs 매번 새로 생성 414ms — 3.6배).
    스레드 로컬에 캐싱해 '스레드 간 격리'와 '연결 재사용'을 동시에 만족시킨다.
    """
    client = getattr(_thread_local, "youtube", None)
    if client is None:
        client = build("youtube", "v3", developerKey=api_key)
        _thread_local.youtube = client
    return client


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
    """제목이 국내 정당정치를 다루는지 판단한다.

    판정 순서:
      1. 국내 고유 키워드(정당·별칭·한자 축약·정치인 이름)가 있으면 **정치**.
         해외 정상 이름이 같이 나와도 무시한다.
      2. 없고 일반 용어(대통령·선거·국회 등)만 있는데 해외 정상 이름이 있으면 **비정치**.
      3. 그 외 일반 용어가 있으면 정치.

    왜 1번이 필요한가 — 이전 버전은 해외 정상 이름이 있으면 **무조건** 비정치로 처리했다.
    그 결과 정상외교·국내 시사 프로그램이 통째로 탈락했다(실측 57건 중 23건이 명백한 국내
    정치): `공항 마중 정청래 '90도 폴더인사'…트럼프의 이란 빅딜 후폭풍`,
    `트럼프 대통령에 국방비 먼저 꺼낸 이재명`, `잼프의 김혜경 여사 소개에 트럼프 반응은`.
    반대로 `트럼프 부정선거 관련 대국민 연설`처럼 일반 용어만 걸린 순수 해외 뉴스(34건)는
    계속 제외되어야 하므로, 키워드를 두 갈래로 나눠 구분한다.

    제목은 비교 전에 NFC 정규화한다(호환용 한자 `李` U+F9E1 문제 — keywords.normalize 참고).
    """
    title = normalize(title)
    if any(kw in title for kw in DOMESTIC_KEYWORDS):
        return True
    if any(fl in title for fl in FOREIGN_LEADER_EXCLUDE):
        return False
    return any(kw in title for kw in GENERIC_KEYWORDS)


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


COMMENT_INSERT_SQL = """
    INSERT INTO comments (
        comment_id, video_id, text, published_at, like_count,
        author_channel_id, total_reply_count
    )
    VALUES %s
    ON CONFLICT (comment_id) DO NOTHING
"""


def sanitize_text(value):
    """PostgreSQL의 TEXT 타입은 NUL 바이트(0x00)를 저장할 수 없는데, 실제 유튜브 댓글·제목에
    이 문자가 섞여 들어오는 경우가 있음(실측: SBS 뉴스 수집 중 발생해 해당 채널 전체가 실패).
    저장 전에 제거한다."""
    if value is None:
        return None
    return value.replace("\x00", "")


def build_comment_row(comment_thread, video_id):
    """API 응답(commentThread)에서 DB에 저장할 필드만 추출한다.
    응답 원본(raw JSONB)은 저장하지 않음 — 대부분이 이미 별도 컬럼에 있는 값이거나
    분석에 안 쓰는 값(etag/thumbnails/authorProfileImageUrl 등)이라 용량만 잡아먹었음.
    text는 textDisplay가 아니라 textOriginal을 쓴다 (textDisplay엔 <br>·&quot; 같은
    HTML이 섞여 있어 그대로 학습에 넣으면 노이즈가 됨 — 실측 23.5%가 해당)."""
    top = comment_thread["snippet"]["topLevelComment"]
    snippet = top["snippet"]
    return (
        top["id"],
        video_id,
        sanitize_text(snippet.get("textOriginal") or snippet["textDisplay"]),
        snippet["publishedAt"],
        snippet["likeCount"],
        snippet.get("authorChannelId", {}).get("value"),
        comment_thread["snippet"].get("totalReplyCount"),
    )


# 영상당 저장할 댓글 수. API는 여전히 1페이지(최대 100건)를 받아오지만
# (페이지 단위 과금이라 적게 요청해도 quota가 안 줄어든다), **저장은 20건만** 한다.
#
# 왜 자르는가:
#   Neon 무료 티어 512MB에서 영상 1편이 13.4KB(실측)를 먹어, 하한선까지 소급하면 615MB로
#   한도를 넘는다. 게다가 한 영상 안의 댓글은 같은 주제·같은 시청자라 서로 강하게 상관돼서
#   41건이 41개의 독립 표본이 아니다. 채널 성향 추정에는 **영상 수**가 훨씬 값지므로
#   댓글 밀도를 줄이고 기간을 지키는 쪽을 택했다.
#
# 왜 '상위 10 + 무작위 10'인가 (층화 표집):
#   - 좋아요 상위만 남기면: 좋아요 총량의 78%를 잡아 '댓글은 안 달고 좋아요만 누르는 다수'의
#     여론을 잘 담지만, 남는 것이 짧고 자극적인 고참여 댓글뿐이라(평균 좋아요 154.7 vs 11.2,
#     대댓글 2.92 vs 0.21) 전체 분포가 왜곡되고 소수 의견이 사라진다. 이미 매긴 라벨 850건은
#     전체 풀에서 뽑은 것이라 학습/추론 분포도 어긋난다. **무가중 분석이 영원히 불가능해진다.**
#   - 무작위만 남기면: 분포는 지켜지지만 좋아요 3,000개짜리 대표 댓글이 탈락해
#     좋아요 가중 분석의 정밀도가 떨어진다.
#   두 층을 반씩 남기면 둘 다 살아남고, 남은 20건을 좋아요로 정렬하면 상위 10건이 곧
#   그 영상의 원래 상위 10건이라 **별도 컬럼 없이 층을 되살릴 수 있다.**
KEEP_TOP = 10
KEEP_RANDOM = 10


def select_comments(comment_threads):
    """저장할 댓글만 고른다 (좋아요 상위 KEEP_TOP + 나머지에서 무작위 KEEP_RANDOM).

    무작위는 comment_id 해시로 정한다 — 실행할 때마다 달라지는 난수를 쓰면 같은 영상을
    다시 수집했을 때 다른 댓글이 뽑혀 표집 규칙이 시점마다 달라진다.
    """
    if len(comment_threads) <= KEEP_TOP + KEEP_RANDOM:
        return comment_threads

    def like_of(t):
        return t["snippet"]["topLevelComment"]["snippet"]["likeCount"]

    def id_of(t):
        return t["snippet"]["topLevelComment"]["id"]

    # 좋아요 동점일 때 순서가 응답 순서에 좌우되지 않도록 id를 2차 기준으로 둔다.
    ordered = sorted(comment_threads, key=lambda t: (-like_of(t), id_of(t)))
    top = ordered[:KEEP_TOP]
    rest = sorted(ordered[KEEP_TOP:], key=lambda t: hashlib.md5(id_of(t).encode()).hexdigest())
    return top + rest[:KEEP_RANDOM]


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


def resume_cutoff(watermarks, channel_id, now):
    """이 채널을 어디까지 다시 훑을지(cutoff)를 계산한다.

    watermarks[channel_id] = 이 채널의 DB 내 가장 최신 정치 영상 published_at.
      - 없으면(새 채널·정치 영상 0건): now - DEFAULT_WINDOW(24시간) fallback.
      - 있으면: 그 시점부터 재개하되 OVERLAP만큼 겹쳐 훑는다. 단 MAX_LOOKBACK을 넘으면 상한으로 자른다.

    반환: (cutoff, note)  — note는 로그용 상태 문자열.
    """
    wm = watermarks.get(channel_id)
    if wm is None:
        return now - DEFAULT_WINDOW, "신규(24h fallback)"
    cutoff = wm - OVERLAP
    floor = now - MAX_LOOKBACK
    if cutoff < floor:
        return floor, f"⚠️상한적용({(now - wm).days}일 밀림 — backfill 권장)"
    return cutoff, f"재개(최신 {wm.strftime('%m/%d %H:%M')} 이후)"


def process_channel(api_key, outlet_name, key_type, key_value, channel_type, watermarks, now):
    """1단계 작업 (스레드에서 실행): 채널 메타데이터 + 최근 영상 목록 + 카테고리 조회.

    cutoff는 고정값이 아니라 이 채널의 DB 최신 영상(watermarks)을 기준으로 여기서 계산한다.
    handle/username 채널은 channel_id가 fetch_channel 이후에야 확정되므로 그 뒤에 계산해야 한다.
    """
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

        cutoff, cutoff_note = resume_cutoff(watermarks, channel_id, now)
        recent_items = fetch_recent_videos(youtube, uploads_playlist_id, cutoff)
        video_ids = [it["snippet"]["resourceId"]["videoId"] for it in recent_items]
        categories = fetch_video_categories(youtube, video_ids) if video_ids else {}
        recent_items = apply_filters(recent_items, categories)

        return {
            "outlet_name": outlet_name,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "channel_type": channel_type,
            "uploads_playlist_id": uploads_playlist_id,
            "recent_items": recent_items,
            "categories": categories,
            "cutoff_note": cutoff_note,
            "error": None,
        }
    except Exception as e:
        return {
            "outlet_name": outlet_name,
            "key_type": key_type,
            "key_value": key_value,
            "error": f"예외 발생: {e}",
        }


class Timings:
    """어디에 시간이 쓰이는지 분해해서 보기 위한 누적 계측기 (스레드 안전)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.data = {}

    def add(self, key, seconds):
        with self._lock:
            n, total = self.data.get(key, (0, 0.0))
            self.data[key] = (n + 1, total + seconds)

    def report(self, wall, workers):
        print(f"\n[구간별 누적 시간] (총 실행 {wall:.1f}초, 동시 {workers}개)")
        for key, (n, total) in sorted(self.data.items(), key=lambda x: -x[1][1]):
            print(f"  {key:22} {n:>5}회  누적 {total:>7.1f}초  평균 {total/n*1000:>6.0f}ms  "
                  f"(병렬 반영 시 약 {total/workers:>5.1f}초)")


TIMINGS = Timings()


def process_video(api_key, db_pool, channel_id, item, category_id, category_name):
    """2단계 작업 (스레드에서 실행): 영상 1건의 댓글 수집 + DB 저장(영상 1행 + 댓글 일괄 삽입)."""
    video_id = item["snippet"]["resourceId"]["videoId"]
    title = item["snippet"]["title"]
    published_at = item["snippet"]["publishedAt"]
    label = f"{category_id} {category_name}"

    t0 = time.perf_counter()
    conn = db_pool.getconn()
    TIMINGS.add("db_pool_getconn", time.perf_counter() - t0)
    try:
        conn.autocommit = True
        youtube = make_youtube_client(api_key)

        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (
                    video_id, channel_id, title, published_at, category_id, is_political
                )
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (video_id) DO NOTHING
                """,
                (video_id, channel_id, sanitize_text(title), published_at, category_id),
            )
        TIMINGS.add("db_insert_video", time.perf_counter() - t0)

        t0 = time.perf_counter()
        comments, error_reason = fetch_top_comments(youtube, video_id)
        TIMINGS.add("api_commentThreads", time.perf_counter() - t0)

        if error_reason is not None:
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE videos SET comments_disabled = TRUE WHERE video_id = %s",
                    (video_id,),
                )
            TIMINGS.add("db_update_disabled", time.perf_counter() - t0)
            return {"title": title, "label": label, "comment_count": 0, "error": error_reason}

        if comments:
            rows = [build_comment_row(c, video_id) for c in select_comments(comments)]
            # 댓글 개수만큼 INSERT를 따로 보내지 않고 한 번에 일괄 삽입 (영상당 DB 왕복 1회)
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, COMMENT_INSERT_SQL, rows)
            TIMINGS.add("db_insert_comments", time.perf_counter() - t0)

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
    print(f"\n수집 기준 시각(UTC): {now.isoformat()}")

    # minconn을 작업자 수만큼 잡아야 한다. psycopg2의 putconn은 풀 보유 연결이 minconn 이상이면
    # 반납된 연결을 보관하지 않고 그냥 close()해버린다. minconn=1이면 연결 1개만 남고 나머지는
    # 매번 새로 만들게 되는데, Neon(싱가포르)까지 새 연결을 여는 데 평균 1.7초가 들어
    # 이것이 전체 실행시간의 최대 병목이었다(계측: getconn 1,682ms vs API 139ms).
    db_pool = ThreadedConnectionPool(DB_POOL_SIZE, DB_POOL_SIZE, test_database_url)

    conn = db_pool.getconn()
    conn.autocommit = True
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO outlets (outlet_name) VALUES %s ON CONFLICT DO NOTHING",
            [(name,) for name in CHANNELS],
        )
        # 채널별 재개 지점(watermark): 이미 저장된 가장 최신 영상의 published_at.
        # 이 값 이후부터만 다시 훑으므로 실행이 밀려도 그 사이를 따라잡는다.
        cur.execute("SELECT channel_id, max(published_at) FROM videos GROUP BY channel_id")
        watermarks = dict(cur.fetchall())
    db_pool.putconn(conn)
    print(f"재개 기준: 채널별 최신 영상 published_at (watermark {len(watermarks)}개, "
          f"신규 채널은 {int(DEFAULT_WINDOW.total_seconds() // 3600)}h fallback)")

    # ---- 1단계: 채널별 메타데이터 + 최근 영상 목록 (채널 단위 병렬) ----
    channel_jobs = [
        (outlet_name, key_type, key_value, channel_type)
        for outlet_name, keys in CHANNELS.items()
        for key_type, key_value, channel_type in keys
    ]

    print(f"\n[1단계] 채널 {len(channel_jobs)}개 메타데이터/영상목록 조회 (동시 {CHANNEL_WORKERS}개)")
    stage1_start = time.perf_counter()
    channel_results = []
    with ThreadPoolExecutor(max_workers=CHANNEL_WORKERS) as executor:
        futures = [
            executor.submit(process_channel, api_key, outlet_name, key_type, key_value,
                            channel_type, watermarks, now)
            for outlet_name, key_type, key_value, channel_type in channel_jobs
        ]
        for future in as_completed(futures):
            channel_results.append(future.result())
    stage1_elapsed = time.perf_counter() - stage1_start

    conn = db_pool.getconn()
    conn.autocommit = True
    video_jobs = []
    for result in channel_results:
        if result["error"] is not None:
            print(f"  [실패] {result['outlet_name']} ({result['key_type']}={result['key_value']}): {result['error']}")
            continue

        print(
            f"  채널: {result['channel_title']} ({result['channel_id']}) "
            f"- outlet: {result['outlet_name']} - {result['cutoff_note']} "
            f"- 정치영상 {len(result['recent_items'])}개"
        )
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
                (
                    result["channel_id"],
                    result["outlet_name"],
                    sanitize_text(result["channel_title"]),
                    result["uploads_playlist_id"],
                    result["channel_type"],
                ),
            )

        for item in result["recent_items"]:
            video_id = item["snippet"]["resourceId"]["videoId"]
            category_id = result["categories"].get(video_id)
            category_name = category_names.get(category_id, "?")
            video_jobs.append((result["channel_id"], item, category_id, category_name))
    db_pool.putconn(conn)

    # ---- 2단계: 전체 채널의 영상을 한 리스트로 모아 영상 단위 병렬 처리 ----
    print(f"\n[2단계] 영상 {len(video_jobs)}개 댓글 수집 (동시 {VIDEO_WORKERS}개)\n")
    stage2_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as executor:
        futures = [
            executor.submit(process_video, api_key, db_pool, channel_id, item, category_id, category_name)
            for channel_id, item, category_id, category_name in video_jobs
        ]
        for future in as_completed(futures):
            r = future.result()
            status = f"댓글 {r['comment_count']}개" if r["error"] is None else f"실패({r['error']})"
            print(f"  - [{r['label']}] {r['title']} → {status}")
    stage2_elapsed = time.perf_counter() - stage2_start

    db_pool.closeall()
    elapsed = time.perf_counter() - start_time
    print(f"\n[단계별] 1단계(채널/영상목록) {stage1_elapsed:.1f}초 / 2단계(댓글수집) {stage2_elapsed:.1f}초")
    TIMINGS.report(stage2_elapsed, VIDEO_WORKERS)
    print(f"\n=== 완료: collect_test DB에 저장됨 (운영 DB 아님) — 총 소요시간 {elapsed:.1f}초 ===")


if __name__ == "__main__":
    main()
