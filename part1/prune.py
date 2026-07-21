"""수집 하한선(RETENTION_FLOOR)보다 오래된 영상·댓글을 DB에서 제거한다.

왜 필요한가:
  Neon 무료 티어는 512MB가 상한이고, 댓글 1건이 약 361 bytes, 영상 1편이 댓글 23.5건이라
  영상 1편당 약 8.5KB를 먹는다. news 채널은 월 700~1,850편씩 쏟아내므로 "1년치 소급"은
  1.3GB급이 되어 무료 티어에서 애초에 불가능하다(실측 근거는 DECISIONS.md 참고).
  그래서 기간을 좁히는 대신 **모든 채널이 같은 기간을 커버**하도록 만드는 쪽을 택했다.

하한선을 왜 '고정 날짜'로 두는가 (롤링 윈도우가 아니라):
  90일 롤링으로 두면 시간이 지날수록 과거 댓글이 계속 삭제된다. 그런데 평가셋(label_sample)은
  한 번 고정하면 바뀌면 안 되는 것이라, 롤링 삭제는 손으로 매긴 정답셋을 갉아먹는다.
  (comment_labels는 comments를 ON DELETE CASCADE로 참조하므로 라벨이 조용히 함께 사라진다.)
  고정 날짜면 창은 앞으로만 자라고 뒤는 절대 잘리지 않아 평가셋이 안전하다.

VACUUM FULL을 쓰지 않는 이유:
  DELETE만으로는 물리 용량이 줄지 않고 죽은 튜플이 남지만, 일반 VACUUM으로 회수한 공간은
  같은 테이블의 이후 INSERT가 재사용한다. 우리는 삭제 직후 backfill로 comments에 대량 INSERT를
  하므로 회수 공간이 그대로 재사용된다. VACUUM FULL은 테이블 전체를 재작성해 ACCESS EXCLUSIVE
  락을 잡고 순간 용량이 튀는데, 무료 티어에서 그 위험을 감수할 이유가 없다.
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

from db import ensure_test_database

# 수집 하한선. 이 시각보다 이전에 업로드된 영상은 보관하지 않는다.
# 2026-07-21 기준 약 3개월. 모든 채널의 공통 커버 기간을 이 날짜로 통일한다.
RETENTION_FLOOR = "2026-04-21"

# 한 트랜잭션에서 지울 영상 수. 크게 잡으면 롱 트랜잭션이 되어 죽은 튜플이 한꺼번에 쌓이고
# 잠금 유지 시간도 길어진다. 작게 끊어 커밋해야 중간에 끊겨도 진행분이 남는다.
CHUNK = 300


def summarize(cur, floor):
    cur.execute("SELECT count(*) FROM videos WHERE published_at < %s", (floor,))
    videos = cur.fetchone()[0]
    cur.execute(
        """SELECT count(*) FROM comments c JOIN videos v ON v.video_id = c.video_id
           WHERE v.published_at < %s""",
        (floor,),
    )
    comments = cur.fetchone()[0]
    cur.execute(
        """SELECT count(*) FROM comment_labels l
           JOIN comments c ON c.comment_id = l.comment_id
           JOIN videos v   ON v.video_id  = c.video_id
           WHERE v.published_at < %s""",
        (floor,),
    )
    labels = cur.fetchone()[0]

    # 고정된 평가셋에 든 댓글은 label_sample이 ON DELETE RESTRICT로 참조하므로 삭제가 막힌다.
    # 삭제 도중에 FK 위반으로 터지면 일부만 지워진 어중간한 상태가 되므로, 시작 전에 세어 본다.
    cur.execute(
        """SELECT count(*) FROM label_sample s
           JOIN comments c ON c.comment_id = s.comment_id
           JOIN videos v   ON v.video_id  = c.video_id
           WHERE v.published_at < %s""",
        (floor,),
    )
    sampled = cur.fetchone()[0]
    return videos, comments, labels, sampled


def db_size(cur):
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    return cur.fetchone()[0]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="하한선보다 오래된 영상·댓글 삭제")
    parser.add_argument("--floor", default=RETENTION_FLOOR, help=f"하한 날짜 (기본 {RETENTION_FLOOR})")
    parser.add_argument("--dry-run", action="store_true", help="삭제하지 않고 대상만 보고")
    parser.add_argument("--yes", action="store_true", help="확인 프롬프트 없이 실행")
    args = parser.parse_args()

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")

    conn = psycopg2.connect(ensure_test_database(database_url))
    cur = conn.cursor()

    videos, comments, labels, sampled = summarize(cur, args.floor)
    print(f"하한선 {args.floor} — 삭제 대상")
    print(f"  영상   {videos:,}편")
    print(f"  댓글   {comments:,}건 (약 {comments * 361 / 1024 / 1024:.0f} MB)")
    print(f"  손라벨 {labels}건  ← comment_labels의 ON DELETE CASCADE로 함께 삭제됨")
    print(f"  평가셋 {sampled}건  ← label_sample의 ON DELETE RESTRICT로 삭제가 차단됨")
    print(f"  현재 DB 크기 {db_size(cur)}")

    if videos == 0:
        print("\n삭제할 것이 없습니다.")
        return
    if args.dry_run:
        print("\n[dry-run] 실제로는 아무것도 삭제하지 않았습니다.")
        return
    if sampled:
        # 고정 평가셋을 깨는 것은 실험 결과를 무효화하는 일이라 옵션으로 넘길 수 있게 두지 않는다.
        raise SystemExit(
            f"\n중단: 고정된 평가셋 {sampled}건이 삭제 범위에 들어 있습니다.\n"
            f"평가셋은 한 번 고정하면 바뀌면 안 되므로, 하한선을 {args.floor}보다 뒤로 옮기려면\n"
            f"먼저 평가셋을 어떻게 할지(새 run_id로 다시 뽑을지) 결정해야 합니다."
        )
    if labels and not args.yes:
        # 손라벨은 사람이 직접 매긴 것이라 재생성 비용이 크다. 실수로 날리지 않도록 한 번 막는다.
        raise SystemExit(
            f"\n손라벨 {labels}건이 함께 삭제됩니다. 백업을 확인한 뒤 --yes 를 붙여 다시 실행하세요."
        )

    cur.execute("SELECT video_id FROM videos WHERE published_at < %s", (args.floor,))
    victim_ids = [r[0] for r in cur.fetchall()]

    deleted_c = deleted_v = 0
    for i in range(0, len(victim_ids), CHUNK):
        chunk = victim_ids[i : i + CHUNK]
        # comments가 videos를 참조하므로 자식(댓글)부터 지운다.
        cur.execute("DELETE FROM comments WHERE video_id = ANY(%s)", (chunk,))
        deleted_c += cur.rowcount
        cur.execute("DELETE FROM videos WHERE video_id = ANY(%s)", (chunk,))
        deleted_v += cur.rowcount
        conn.commit()
        print(f"  ...{deleted_v:,}/{len(victim_ids):,}편 삭제 (댓글 {deleted_c:,}건)", end="\r")

    print(f"\n삭제 완료: 영상 {deleted_v:,}편, 댓글 {deleted_c:,}건")

    # VACUUM은 트랜잭션 블록 안에서 실행할 수 없다.
    conn.autocommit = True
    print("VACUUM ANALYZE 실행 중 (회수 공간을 이후 INSERT가 재사용하도록)...")
    cur.execute("VACUUM ANALYZE comments")
    cur.execute("VACUUM ANALYZE videos")

    print(f"완료. DB 크기 {db_size(cur)}")
    print("  ※ 물리 크기 표시는 바로 줄지 않을 수 있습니다. 회수된 공간은 테이블 내부에")
    print("    재사용 가능 상태로 남아 backfill의 신규 INSERT가 그대로 채워 씁니다.")
    conn.close()


if __name__ == "__main__":
    main()
