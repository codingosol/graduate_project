"""평가셋(정답셋) 표본을 label_sample 테이블에 고정한다.

라벨링 툴은 원래 표본을 질의할 때마다 md5(comment_id) 순서로 새로 뽑았다. 표본이 DB 내용에
의존하는 파생값이라 수집이 진행될 때마다 표본이 바뀌었고, 이미 매긴 라벨이 표본 밖으로
밀려나거나 채널별 구성이 뒤틀렸다(자세한 경위는 migrations/0007_label_sample.sql 주석).
이 스크립트로 못박은 뒤에는 표본이 절대 바뀌지 않는다.

기간을 나눠 두 번에 걸쳐 뽑는 이유:
  backfill이 하루 quota로는 하한선까지 못 내려가서, 지금 시점의 풀은 창의 뒤쪽 절반
  (news 채널 기준 06-03~07-21)만 담고 있다. 여기서 300건을 다 뽑으면 평가셋이 최근 절반만
  대표하게 된다. 그렇다고 backfill이 끝날 때까지 며칠을 기다리면 라벨링이 통째로 멈춘다.
  그래서 기간을 층(stratum)으로 나눠 절반씩 뽑는다.
      1차: --since 2026-06-03 --until 2026-07-22  (지금)
      2차: --since 2026-04-21 --until 2026-06-03  (backfill 완료 후)
  두 층의 실제 기간이 1.6개월 대 1.4개월로 비슷해 150/150 균등 배분이 거의 비례가 된다.
  같은 run_id에 ord를 이어 붙이므로 결과물은 나눠 뽑은 흔적 없이 하나의 평가셋이다.

  단, 층과 라벨링 시점이 같이 움직이므로 판정 기준이 흔들리면(drift) 그것이 '시기 차이'와
  구분되지 않는다. guidelines.md를 두 차수 사이에 바꾸지 않는 것으로 이 위험을 줄인다.

표본 설계:
  - 채널(outlet x channel_type) 13개 그룹에 균등 배분한다. 채널 간 비교가 목적이라
    특정 채널이 표본을 독식하면 안 된다(실제로 오마이TV가 라벨의 20%를 차지한 적이 있음).
  - 해당 기간에 이미 손으로 매긴 댓글은 그룹 목표치까지 먼저 채워 넣는다. 사람이 매긴 라벨은
    재생성 비용이 크고, 이들 역시 애초에 무작위로 뽑힌 것이라 넣어도 무작위성이 깨지지 않는다.
    목표치를 넘는 초과분은 표본에 넣지 않고 그대로 둔다(학습용 여분으로 남음).
  - 나머지는 md5(comment_id) 순서(사실상 무작위)로 채운다. 제시 순서(ord)도 같은 기준이라
    기존 라벨과 신규가 고르게 뒤섞인다.
"""

import argparse
import hashlib
import os
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# part1/ 루트를 경로에 추가 — 공용 모듈(db, migrate)을 Data/·AI/ 어디서 실행해도 찾도록.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from db import ensure_test_database

MIN_TEXT_LEN = 10   # labeling_tool/label.py와 같은 값이어야 한다
MAX_TEXT_LEN = 600


def md5_key(comment_id):
    """DB의 md5(comment_id)와 같은 값이라 정렬 결과가 일치한다."""
    return hashlib.md5(comment_id.encode()).hexdigest()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="평가셋 표본을 기간별로 고정")
    parser.add_argument("--run", default="manual_gold_v1")
    parser.add_argument("--size", type=int, required=True, help="이번 차수에 뽑을 개수")
    parser.add_argument("--since", required=True, help="영상 업로드일 하한 (포함)")
    parser.add_argument("--until", required=True, help="영상 업로드일 상한 (미포함)")
    args = parser.parse_args()

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")

    conn = psycopg2.connect(ensure_test_database(database_url))
    cur = conn.cursor()

    # 이미 고정된 표본은 절대 건드리지 않는다. 이번 차수는 그 뒤에 이어 붙일 뿐이다.
    cur.execute(
        "SELECT count(*), COALESCE(max(ord) + 1, 0) FROM label_sample WHERE run_id = %s",
        (args.run,),
    )
    existing, next_ord = cur.fetchone()
    if existing:
        print(f"기존 고정분 {existing}건 — 이번 차수는 ord {next_ord}번부터 이어 붙입니다.")

    cur.execute(
        """
        SELECT c.comment_id, ch.outlet_name, ch.channel_type,
               (l.comment_id IS NOT NULL) AS already_labeled
        FROM comments c
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        LEFT JOIN comment_labels l
               ON l.comment_id = c.comment_id AND l.run_id = %s
        WHERE v.is_political
          AND length(c.text) BETWEEN %s AND %s
          AND v.published_at >= %s
          AND v.published_at <  %s
          -- 앞 차수에서 이미 뽑힌 댓글은 후보에서 제외
          AND NOT EXISTS (
              SELECT 1 FROM label_sample s
              WHERE s.run_id = %s AND s.comment_id = c.comment_id
          )
        """,
        (args.run, MIN_TEXT_LEN, MAX_TEXT_LEN, args.since, args.until, args.run),
    )
    pool = defaultdict(list)
    for comment_id, outlet, ctype, labeled in cur.fetchall():
        pool[(outlet, ctype)].append((comment_id, labeled))

    groups = sorted(pool)
    if not groups:
        raise SystemExit(f"{args.since} ~ {args.until} 구간에 표본 후보가 없습니다.")

    # 그룹별 목표: size를 고르게 나누고 나머지는 앞 그룹부터 하나씩.
    base, extra = divmod(args.size, len(groups))
    targets = {g: base + (1 if i < extra else 0) for i, g in enumerate(groups)}

    chosen, reused, surplus = [], 0, 0
    print(f"\n기간 {args.since} ~ {args.until}, 목표 {args.size}건")
    print(f"{'채널':<24}{'후보':>9}{'기존라벨':>9}{'목표':>6}{'재사용':>7}{'신규':>6}")
    for g in groups:
        items = sorted(pool[g], key=lambda x: md5_key(x[0]))
        labeled = [cid for cid, lab in items if lab]
        unlabeled = [cid for cid, lab in items if not lab]

        keep = labeled[: targets[g]]                       # 기존 라벨을 목표치까지 우선 사용
        fresh = unlabeled[: max(targets[g] - len(keep), 0)]
        chosen.extend(keep + fresh)
        reused += len(keep)
        surplus += len(labeled) - len(keep)
        print(
            f"{g[0] + '/' + g[1]:<24}{len(items):>9,}{len(labeled):>9}"
            f"{targets[g]:>6}{len(keep):>7}{len(fresh):>6}"
        )

    chosen.sort(key=md5_key)
    rows = [(args.run, cid, next_ord + i) for i, cid in enumerate(chosen)]
    psycopg2.extras.execute_values(
        cur, "INSERT INTO label_sample (run_id, comment_id, ord) VALUES %s", rows
    )
    conn.commit()

    cur.execute("SELECT count(*) FROM label_sample WHERE run_id = %s", (args.run,))
    total = cur.fetchone()[0]
    print(f"\n고정 완료: 이번 차수 {len(rows)}건 (기존 라벨 재사용 {reused}건, 새로 매길 것 {len(rows) - reused}건)")
    print(f"  누적 표본 {total}건")
    if surplus:
        print(f"  목표를 넘어 표본에 넣지 않은 기존 라벨 {surplus}건은 그대로 남아 학습용 여분이 됩니다.")
    conn.close()


if __name__ == "__main__":
    main()
