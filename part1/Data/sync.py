"""스테이징 DB(Neon) → 로컬 분석 DB 동기화. 옮긴 수집분은 스테이징에서 지워 Neon을 가볍게 유지한다.

구조 (2026-09-26부터 — 경위는 DECISIONS.md 09-26):
    GitHub Actions: collect.py·backfill.py ──적재──▶ 스테이징 DB (Neon 무료 0.5GB, STAGING_DATABASE_URL)
                                                          │  python Data/sync.py  (로컬에서 실행)
                                                          ▼
                                   로컬 분석 DB (Docker PostgreSQL, DATABASE_URL)
                                   = 전체 데이터·라벨의 정본. 학습·추론·집계·대시보드는 여기서만 한다.

스테이징에 남기는 것 — 수집 스크립트가 다음 실행에서 읽는 상태:
  - outlets, channels : 작다. backfill 커서(backfill_cursor/exhausted)의 정본은 스테이징이다.
  - videos            : 채널별 최신 영상은 collect.py의 재개 지점(watermark)이라 남긴다.
                        그 외에 --keep-days보다 오래됐고 스테이징에 댓글이 남지 않은 영상만 지운다.
스테이징에서 지우는 것 — 로컬에 커밋까지 끝난 comments(용량의 대부분)와 라벨 테이블 행.

안전장치:
  - 모든 테이블을 먼저 복사(청크마다 로컬 커밋)한 뒤, **이번 실행에서 복사한 기본키만** 스테이징에서
    지운다. 복사하지 않은 행(예: 동기화 도중 수집이 새로 넣은 행)은 지워질 수 없다.
  - 로컬에는 ON CONFLICT로 넣으므로 몇 번을 실행해도 안전하다. 중간에 끊기면 다시 실행하면 된다.
  - --keep-staging : 복사만 하고 스테이징은 건드리지 않는다(점검용).
  - --from-env     : 다른 DB를 원본으로 쓴다(예: 옛 Neon 백업). 이때는 항상 복사만 하고 원본을 지우지 않는다.

새 스테이징 DB를 처음 쓸 때 (--seed-staging):
  비어 있는 스테이징에는 수집 스크립트가 읽는 상태가 없다. 그대로 두면 collect.py는 재개 지점을 몰라
  24시간만 훑고, backfill.py는 커서가 없어 "지금"부터 하한까지 전부 다시 소급하며 쿼터를 몇 주 낭비한다.
  그래서 로컬 → 스테이징으로 outlets·channels(커서 포함)와 채널별 최신 영상(+최근 --keep-days일)을 심는다.

실행 (part1/에서):
    python Data/sync.py                   # 복사 + 스테이징 정리 (평소)
    python Data/sync.py --keep-staging    # 복사만
    python Data/sync.py --from-env NEON_BACKUP_DATABASE_URL   # 다른 DB에서 복사만
    python migrate.py --staging && python Data/sync.py --seed-staging   # 새 스테이징 초기화
"""

import argparse
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

CHUNK = 5000

# 외래키 순서(부모 → 자식). 세 번째 값은 로컬에 같은 기본키가 이미 있을 때의 처리.
#   channels : 스테이징이 정본(재생목록 ID·backfill 커서) → 비어 있지 않은 값으로 갱신
#   videos   : 수집이 바꾸는 상태(comments_disabled·last_checked_at)만 반영한다.
#              is_political은 로컬의 reclassify.py가 정본이라 덮어쓰지 않는다.
#   나머지   : 한 번 저장되면 바뀌지 않으므로 DO NOTHING
TABLES = [
    ("outlets", ("outlet_name",), "DO NOTHING"),
    ("channels", ("channel_id",), "COALESCE"),
    ("videos", ("video_id",),
     "DO UPDATE SET comments_disabled = videos.comments_disabled OR EXCLUDED.comments_disabled, "
     "last_checked_at = GREATEST(videos.last_checked_at, EXCLUDED.last_checked_at)"),
    ("comments", ("comment_id",), "DO NOTHING"),
    ("label_runs", ("run_id",), "DO NOTHING"),
    ("comment_labels", ("run_id", "comment_id"), "DO NOTHING"),
    ("label_sample", ("run_id", "comment_id"), "DO NOTHING"),
]
PK = {t: pk for t, pk, _ in TABLES}

# 채널별 최신 영상 = collect.py의 재개 지점(watermark). 한 번만 계산되도록 비상관 서브쿼리로 둔다
# (영상마다 max()를 다시 구하는 상관 서브쿼리는 5만 건에서 85초가 걸렸다).
LATEST_PER_CHANNEL = ("SELECT DISTINCT ON (channel_id) video_id FROM videos "
                      "ORDER BY channel_id, published_at DESC")


def clean(e):
    """에러 메시지에서 접속 문자열(비밀번호 포함)을 가린다."""
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return re.sub(r"postgres(ql)?://\S+", "<url>", s)


class Source:
    """읽기용 연결. Neon은 긴 작업 중 연결을 끊는 일이 있어, 끊기면 다시 연결해 같은 쿼리를 재시도한다."""

    def __init__(self, url):
        self.url = url
        self.conn = None
        self.connect()

    def connect(self):
        self.conn = psycopg2.connect(self.url, connect_timeout=30)
        self.conn.autocommit = True

    def fetch(self, sql, params=()):
        for attempt in range(3):
            try:
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if attempt == 2:
                    raise
                print(f"    스테이징 연결 끊김({clean(e)}) → 재연결 후 재시도")
                self.connect()


def columns(fetch, table):
    rows = fetch("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position""", (table,))
    return [r[0] for r in rows]


def copy_table(src, dst, table, pk, on_conflict, where="TRUE"):
    """키셋 페이지네이션으로 src(Source) → dst(연결) 복사. 복사한 기본키 목록을 돌려준다."""
    with dst.cursor() as dc:
        dc.execute("""SELECT column_name FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = %s
                      ORDER BY ordinal_position""", (table,))
        cols = [r[0] for r in dc.fetchall()]
    missing = set(cols) - set(columns(src.fetch, table))
    if missing:
        raise SystemExit(f"{table}: 원본에 없는 컬럼 {sorted(missing)} — 두 DB의 마이그레이션을 맞출 것 "
                         f"(python migrate.py / python migrate.py --staging)")

    col_sql, pk_sql = ", ".join(cols), ", ".join(pk)
    if on_conflict == "COALESCE":
        sets = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in cols if c not in pk)
        on_conflict = f"DO UPDATE SET {sets}"
    insert = f"INSERT INTO {table} ({col_sql}) VALUES %s ON CONFLICT ({pk_sql}) {on_conflict}"
    pk_idx = [cols.index(k) for k in pk]
    after = f"AND ({pk_sql}) > ({', '.join(['%s'] * len(pk))})"

    copied, last, t0 = [], None, time.perf_counter()
    while True:
        rows = src.fetch(f"SELECT {col_sql} FROM {table} WHERE ({where}) {after if last else ''} "
                         f"ORDER BY {pk_sql} LIMIT %s", (*(last or ()), CHUNK))
        if not rows:
            break
        with dst.cursor() as dc:
            psycopg2.extras.execute_values(dc, insert, rows, page_size=1000)
        dst.commit()
        keys = [tuple(r[i] for i in pk_idx) for r in rows]
        copied.extend(keys)
        last = keys[-1]
        if len(copied) % (CHUNK * 20) == 0:
            print(f"    {table}: {len(copied):,}건 복사 중… ({time.perf_counter() - t0:.0f}s)", flush=True)
    return copied


def delete_keys(stg, table, pk, keys, extra_where=""):
    """스테이징에서 주어진 기본키 행만 지운다(청크 단위). 지운 행 수를 돌려준다."""
    cond = " AND ".join(f"t.{k} = d.{k}" for k in pk)
    sql = f"DELETE FROM {table} t USING (VALUES %s) AS d({', '.join(pk)}) WHERE {cond} {extra_where}"
    deleted = 0
    for i in range(0, len(keys), CHUNK):
        part = keys[i:i + CHUNK]
        with stg.conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, part, page_size=len(part))
            deleted += cur.rowcount
    return deleted


def purge_staging(stg, copied, keep_days):
    """복사가 끝난 행을 스테이징에서 지운다. label_sample → comments가 ON DELETE RESTRICT라 자식부터."""
    print("\n[스테이징 정리]")
    for table in ("label_sample", "comment_labels", "comments"):
        n = delete_keys(stg, table, PK[table], copied.get(table, []))
        print(f"  {table:<15} {n:>9,}건 삭제")
    # 라벨 실행 기록은 딸린 라벨이 스테이징에 하나도 안 남은 것만 지운다.
    n = delete_keys(stg, "label_runs", PK["label_runs"], copied.get("label_runs", []),
                    "AND NOT EXISTS (SELECT 1 FROM comment_labels c WHERE c.run_id = t.run_id) "
                    "AND NOT EXISTS (SELECT 1 FROM label_sample s WHERE s.run_id = t.run_id)")
    print(f"  {'label_runs':<15} {n:>9,}건 삭제")
    # 영상: 오래됐고, 스테이징에 댓글이 없고, 채널의 최신 영상(watermark)이 아닌 것만.
    n = delete_keys(stg, "videos", PK["videos"], copied.get("videos", []),
                    f"AND t.published_at < now() - make_interval(days => {int(keep_days)}) "
                    "AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.video_id = t.video_id) "
                    f"AND t.video_id NOT IN ({LATEST_PER_CHANNEL})")
    print(f"  {'videos':<15} {n:>9,}건 삭제 ({keep_days}일 이내·채널별 최신 영상은 유지)")


def host(url):
    m = re.search(r"@([^:/?]+)", url)
    return m.group(1) if m else "?"


def copy_all(src, dst, tables, label):
    """tables = [(테이블, 기본키, 충돌 처리, 원본 필터)]를 순서대로 복사하고 테이블별 복사 키를 돌려준다."""
    print(f"\n[복사] {label}")
    copied = {}
    for table, pk, on_conflict, where in tables:
        t = time.perf_counter()
        copied[table] = copy_table(src, dst, table, pk, on_conflict, where)
        with dst.cursor() as dc:
            dc.execute(f"SELECT count(*) FROM {table}")
            total = dc.fetchone()[0]
        dst.commit()
        print(f"  {table:<15} {len(copied[table]):>9,}건 복사 → 대상 총 {total:>9,}건 "
              f"({time.perf_counter() - t:.0f}s)", flush=True)
    return copied


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="스테이징 DB(Neon) → 로컬 분석 DB 동기화")
    p.add_argument("--keep-staging", action="store_true", help="복사만 하고 스테이징은 지우지 않는다")
    p.add_argument("--from-env", default="STAGING_DATABASE_URL",
                   help="원본 DB 환경변수 이름. 기본값이 아니면 복사만 하고 원본은 지우지 않는다")
    p.add_argument("--seed-staging", action="store_true",
                   help="반대 방향: 로컬 → 스테이징으로 수집 상태(채널·커서·채널별 최신 영상)를 심는다")
    p.add_argument("--keep-days", type=int, default=14,
                   help="스테이징에 남길 최근 영상 기간(일). collect.py 재개 창(최대 7일)보다 넉넉하게 (기본 14)")
    args = p.parse_args()

    load_dotenv()
    src_env = "DATABASE_URL" if args.seed_staging else args.from_env
    dst_env = "STAGING_DATABASE_URL" if args.seed_staging else "DATABASE_URL"
    src_url, dst_url = os.environ.get(src_env), os.environ.get(dst_env)
    if not src_url or not dst_url:
        raise SystemExit(f"{src_env} / {dst_env} 환경변수가 필요합니다.")
    if host(src_url) == host(dst_url):
        raise SystemExit("원본과 대상이 같은 호스트입니다 — .env를 확인할 것.")

    src = Source(src_url)
    dst = psycopg2.connect(dst_url, connect_timeout=30)
    # 파괴적 작업 전에 대상을 눈으로 확인할 수 있게 한다(migrate.py와 같은 원칙).
    with dst.cursor() as dc:
        dc.execute("SELECT current_database()")
        dst_db = dc.fetchone()[0]
    print(f"원본: {src_env} = {host(src_url)} / {src.fetch('SELECT current_database()')[0][0]}")
    print(f"대상: {dst_env} = {host(dst_url)} / {dst_db}")
    t0 = time.perf_counter()

    if args.seed_staging:
        latest = ("published_at >= now() - make_interval(days => %d) OR video_id IN (%s)"
                  % (args.keep_days, LATEST_PER_CHANNEL))
        copy_all(src, dst, [("outlets", PK["outlets"], "DO NOTHING", "TRUE"),
                            ("channels", PK["channels"], "COALESCE", "TRUE"),
                            ("videos", PK["videos"], "DO NOTHING", latest)],
                 "로컬 → 스테이징 (수집 상태 심기)")
        print(f"=== 스테이징 초기화 완료 — {time.perf_counter() - t0:.0f}초 ===")
        return

    # 적재 뒤 통계 갱신(ANALYZE)은 autovacuum에 맡긴다 — 수동 전체 ANALYZE는 로컬에서 97초가 걸려
    # 매일 수천 행 동기화에 비해 과했다.
    copied = copy_all(src, dst, [(t, pk, oc, "TRUE") for t, pk, oc in TABLES], f"{src_env} → 로컬")

    if args.keep_staging or src_env != "STAGING_DATABASE_URL":
        print(f"\n복사만 했다 — 원본({src_env})은 그대로 둔다.")
    else:
        purge_staging(src, copied, args.keep_days)
    size = src.fetch("SELECT pg_size_pretty(pg_database_size(current_database()))")[0][0]
    print(f"\n원본 DB 크기: {size}" + (" (Neon 무료 한도 0.5GB)" if src_env == "STAGING_DATABASE_URL" else ""))
    print(f"=== 동기화 완료 — {time.perf_counter() - t0:.0f}초 ===")


if __name__ == "__main__":
    main()
