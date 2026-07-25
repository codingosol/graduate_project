"""저장된 영상 제목에 현재 키워드 필터를 다시 적용해 `videos.is_political`을 갱신한다.

왜 필요한가:
  - 초기 수집(필터가 categoryId만 있던 시절)에 들어온 비정치 영상이 DB에 남아 있다.
    실측 4,659건(전체의 23.8%)이 현재 필터를 통과하지 못하며, 날씨·교통사고·스포츠 등이 섞여 있다.
  - `keywords.json`은 앞으로도 계속 손본다(정치인 이름 갱신, 오탐 키워드 제거 등).
    키워드를 바꿀 때마다 과거 데이터의 판정도 함께 갱신되어야 일관성이 유지된다.

수집 시점의 판정을 신뢰하지 않고 **항상 현재 기준으로 다시 매기는** 방식이라 몇 번을 실행해도 안전하다.
데이터를 지우지 않고 플래그만 갱신하므로 되돌릴 수 있다.

    python reclassify.py            # 테스트 DB 대상, 변경 내역 요약 출력
    python reclassify.py --dry-run  # 실제로 쓰지 않고 무엇이 바뀔지만 확인
"""

import argparse
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# part1/ 루트를 경로에 추가 — 공용 모듈(db, migrate)을 Data/·AI/ 어디서 실행해도 찾도록.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from collect import is_political_title


def test_database_url():
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL이 .env에 없습니다.")
    if "?" in url:
        head, query = url.split("?", 1)
        query = "?" + query
    else:
        head, query = url, ""
    return head.rsplit("/", 1)[0] + "/collect_test" + query


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="저장된 영상에 현재 정치 필터를 재적용")
    p.add_argument("--dry-run", action="store_true", help="쓰지 않고 결과만 확인")
    args = p.parse_args()

    conn = psycopg2.connect(test_database_url())
    with conn.cursor() as cur:
        cur.execute("SELECT video_id, title, is_political FROM videos")
        rows = cur.fetchall()

    updates = []
    changed = {"NULL→True": 0, "NULL→False": 0, "True→False": 0, "False→True": 0}
    for video_id, title, current in rows:
        verdict = is_political_title(title)
        if current is verdict:
            continue
        key = f"{'NULL' if current is None else current}→{verdict}"
        changed[key] = changed.get(key, 0) + 1
        updates.append((video_id, verdict))

    total = len(rows)
    politics = sum(1 for _, t, _ in rows if is_political_title(t))
    print(f"전체 영상        : {total:,}")
    print(f"현재 기준 정치   : {politics:,} ({politics/total*100:.1f}%)")
    print(f"현재 기준 비정치 : {total-politics:,} ({(total-politics)/total*100:.1f}%)")
    print(f"\n플래그 변경 대상 : {len(updates):,}건")
    for k, v in changed.items():
        if v:
            print(f"  {k:<14} {v:>6,}")

    if args.dry_run:
        print("\n--dry-run 이므로 쓰지 않고 종료합니다.")
        conn.close()
        return

    if updates:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """UPDATE videos AS v SET is_political = d.flag
                   FROM (VALUES %s) AS d(video_id, flag)
                   WHERE v.video_id = d.video_id""",
                updates,
                template="(%s, %s::boolean)",
            )
        conn.commit()
        print(f"\n갱신 완료: {len(updates):,}건")

    with conn.cursor() as cur:
        cur.execute(
            """SELECT v.is_political, count(*) AS videos,
                      (SELECT count(*) FROM comments c
                        JOIN videos v2 ON v2.video_id = c.video_id
                       WHERE v2.is_political IS NOT DISTINCT FROM v.is_political)
               FROM videos v GROUP BY 1 ORDER BY 1"""
        )
        print(f"\n{'is_political':<14}{'영상':>10}{'댓글':>12}")
        for flag, vids, cmts in cur.fetchall():
            print(f"{str(flag):<14}{vids:>10,}{cmts:>12,}")
    conn.close()


if __name__ == "__main__":
    main()
