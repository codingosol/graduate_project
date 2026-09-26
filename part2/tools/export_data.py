"""Part 1(로컬 분석 DB) 집계 → 크롬 익스텐션용 정적 JSON 2개 export.

산출물(part2/data/):
  leaning_table.json      채널(13개) → {진보|중도|보수}  (분류용, 수 KB)
  channel_dashboard.json  채널별 성향·강도 점수 + 일별 시계열 (참고 대시보드용, 수백 KB)

서버 없이 익스텐션에 번들하기 위한 것. Part 1 갱신(추론) 후 다시 실행하면 된다.
쿼터 불필요(DB 읽기만). 접속은 part1/.env 의 DATABASE_URL(로컬 분석 DB).

실행: python part2/tools/export_data.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # graduateProject/
load_dotenv(os.path.join(ROOT, "part1", ".env"))
OUT_DIR = os.path.join(ROOT, "part2", "data")

LEAN_RUN = "model_kcelectra_v2"
INT_RUN = "model_khaters_v1"

# Part 1이 통념·모델 순위로 검증해 확정한 3버킷. ⚠️ 점수 부호로 나누지 않는다(우편향 때문).
BUCKET = {
    "오마이TV": "진보", "MBC 뉴스": "진보", "JTBC News": "진보",
    "TV조선": "보수", "채널A News": "보수", "MBN News": "보수",
    "KBS News": "중도", "YTN": "중도", "SBS 뉴스": "중도", "연합뉴스TV": "중도",
}


def q(cur, sql, params=None):
    cur.execute(sql, params or [])
    return cur.fetchall()


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    # ── leaning_table: 채널(13개) → 버킷. DOM 매칭용으로 channel_id·title 함께. ──
    rows = q(cur, """
        SELECT ch.channel_id, ch.title, ch.outlet_name, ch.channel_type
        FROM channels ch ORDER BY ch.outlet_name, ch.channel_type
    """)
    leaning_table = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "채널명(title) 또는 channel_id로 매칭. bucket이 성향(진보/중도/보수). "
                "여기 없는 채널은 익스텐션에서 '미상' 처리.",
        "channels": [
            {"channel_id": cid, "title": title, "outlet": outlet,
             "channel_type": ctype, "bucket": BUCKET.get(outlet, "미상")}
            for cid, title, outlet, ctype in rows
        ],
    }

    # ── 채널(outlet) 요약: 성향 점수·분포(leaning) + 과격도(intensity) + 로고 ──
    lean = q(cur, f"""
        SELECT ch.outlet_name,
               count(*) FILTER (WHERE cl.label='left')    AS l,
               count(*) FILTER (WHERE cl.label='neutral') AS n,
               count(*) FILTER (WHERE cl.label='right')   AS r,
               avg(cl.score)::float AS score, count(*) AS total,
               max(ch.thumbnail_url) AS thumb
        FROM comment_labels cl
        JOIN comments c  ON c.comment_id = cl.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE cl.run_id=%s AND cl.label <> 'unusable' AND v.is_political
        GROUP BY ch.outlet_name
    """, [LEAN_RUN])
    inten = dict((r[0], (r[1], r[2])) for r in q(cur, f"""
        SELECT ch.outlet_name, avg(cl.score)::float AS intensity, count(*) AS total
        FROM comment_labels cl
        JOIN comments c  ON c.comment_id = cl.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE cl.run_id=%s AND v.is_political
        GROUP BY ch.outlet_name
    """, [INT_RUN]))

    channels = []
    for outlet, l, n, r, score, total, thumb in lean:
        it = inten.get(outlet, (None, 0))
        channels.append({
            "outlet": outlet, "bucket": BUCKET.get(outlet, "미상"), "thumb": thumb,
            "leaning_score": round(score, 4) if score is not None else None,
            "intensity": round(it[0], 4) if it[0] is not None else None,
            "counts": {"left": l, "neutral": n, "right": r, "total": total},
        })
    channels.sort(key=lambda c: (c["leaning_score"] if c["leaning_score"] is not None else 0))

    # ── 일별 시계열(outlet 단위): leaning 좌/우/중립·영상·댓글 + intensity 평균 ──
    lean_ts = q(cur, f"""
        SELECT ch.outlet_name, date_trunc('day', c.published_at)::date AS d,
               count(*) FILTER (WHERE cl.label='left')  AS l,
               count(*) FILTER (WHERE cl.label='right') AS r,
               count(*) FILTER (WHERE cl.label='neutral') AS n,
               count(DISTINCT v.video_id) AS videos, count(*) AS comments
        FROM comment_labels cl
        JOIN comments c  ON c.comment_id = cl.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE cl.run_id=%s AND cl.label <> 'unusable' AND v.is_political
              AND c.published_at IS NOT NULL
        GROUP BY 1, 2
    """, [LEAN_RUN])
    int_ts = dict(((r[0], r[1]), r[2]) for r in q(cur, f"""
        SELECT ch.outlet_name, date_trunc('day', c.published_at)::date AS d,
               avg(cl.score)::float AS intensity
        FROM comment_labels cl
        JOIN comments c  ON c.comment_id = cl.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE cl.run_id=%s AND v.is_political AND c.published_at IS NOT NULL
        GROUP BY 1, 2
    """, [INT_RUN]))

    ts = defaultdict(list)
    for outlet, d, l, r, n, videos, comments in sorted(lean_ts, key=lambda x: (x[0], x[1])):
        inten_v = int_ts.get((outlet, d))
        ts[outlet].append({
            "d": d.isoformat(), "l": l, "r": r, "n": n,
            "videos": videos, "comments": comments,
            "intensity": round(inten_v, 4) if inten_v is not None else None,
        })

    dashboard = {
        "generated_at": leaning_table["generated_at"],
        "channels": channels,
        "timeseries": ts,
    }

    conn.close()
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, obj in [("leaning_table.json", leaning_table),
                      ("channel_dashboard.json", dashboard)]:
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  {name}: {os.path.getsize(path)/1024:.1f} KB")
    print(f"채널 {len(channels)}개 / 시계열 outlet {len(ts)}개 / 저장: {OUT_DIR}")


if __name__ == "__main__":
    main()
