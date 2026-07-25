"""LLM이 매긴 학습셋 라벨(labels_NN.json)을 DB에 적재한다.

청크 단위로 파일에 기록해 두고 마지막에 한 번에 넣는 구조라, 라벨링이 중간에
끊겨도 그때까지 만들어진 파일은 그대로 남는다. 이 스크립트는 존재하는 파일만
찾아 넣으므로 몇 개가 있든 그대로 동작하고, 여러 번 실행해도 안전하다(UPSERT).
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# part1/ 루트를 경로에 추가 — 공용 모듈(db, migrate)을 Data/·AI/ 어디서 실행해도 찾도록.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from db import ensure_test_database

# labeling_tool/은 저장소 루트(part1의 부모) 아래에 있다. AI/에서 두 단계 위로 올라간다.
DEFAULT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "labeling_tool", "trainset"
)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="LLM 학습셋 라벨 적재")
    p.add_argument("--run", default="llm_train_v1")
    p.add_argument("--dir", default=DEFAULT_DIR, help="chunk/labels/index.json이 있는 디렉토리")
    args = p.parse_args()

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")

    with open(os.path.join(args.dir, "index.json"), encoding="utf-8") as f:
        index = json.load(f)["index"]

    labels = {}
    files = sorted(glob.glob(os.path.join(args.dir, "labels_*.json")))
    for path in files:
        with open(path, encoding="utf-8") as f:
            labels.update(json.load(f))
    print(f"라벨 파일 {len(files)}개에서 {len(labels)}건 수집")

    rows, missing = [], 0
    for n, label in labels.items():
        comment_id = index.get(str(n))
        if comment_id is None:
            missing += 1
            continue
        rows.append((args.run, comment_id, label))
    if missing:
        print(f"  ⚠️ index에 없는 번호 {missing}건은 건너뜀")

    conn = psycopg2.connect(ensure_test_database(database_url))
    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO comment_labels (run_id, comment_id, label) VALUES %s
           ON CONFLICT (run_id, comment_id) DO UPDATE SET label = EXCLUDED.label""",
        rows,
    )
    conn.commit()

    cur.execute(
        "SELECT label, count(*) FROM comment_labels WHERE run_id=%s GROUP BY 1 ORDER BY 1",
        (args.run,),
    )
    d = dict(cur.fetchall())
    total = sum(d.values())
    print(f"\n적재 완료 — run '{args.run}' 누적 {total}건")
    for k in ("left", "right", "neutral", "unusable"):
        print(f"  {k:<9}{d.get(k, 0):>5}  ({d.get(k, 0) / total * 100:.0f}%)")
    print(f"  ── 학습 가능(사용불가 제외): {total - d.get('unusable', 0)}건")

    # 평가셋과 겹치면 정확도 측정이 무의미해진다. 매번 확인한다.
    cur.execute(
        """SELECT count(*) FROM comment_labels l
           JOIN label_sample s ON s.comment_id = l.comment_id
           WHERE l.run_id = %s""",
        (args.run,),
    )
    overlap = cur.fetchone()[0]
    print(f"  평가셋과 중복: {overlap}건 {'✅' if overlap == 0 else '⚠️ 문제!'}")
    conn.close()


if __name__ == "__main__":
    main()
