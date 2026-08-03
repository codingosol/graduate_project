"""LLM 라벨링용 학습셋 표본을 뽑아 청크 파일로 내보낸다.

평가셋(label_sample)과 겹치면 안 된다. 겹치면 학습 데이터로 채점하게 되어
정확도 측정이 무의미해진다. 이미 라벨이 있는 댓글도 전부 제외한다.

**영상 제목은 내보내지 않는다.** guidelines.md 원칙 1이 "모델은 추론할 때 댓글
텍스트만 받는다"이므로, 라벨을 매기는 쪽도 텍스트만 봐야 한다. 제목을 보고 매기면
모델이 원리적으로 알아낼 수 없는 정답을 학습 데이터에 심게 된다.
텍스트만 주면 "제목이 있어야 대상을 알 수 있는 댓글은 neutral"이 자동으로 강제된다.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

import psycopg2
from dotenv import load_dotenv


MIN_TEXT_LEN = 10    # labeling_tool/label.py와 같은 값
MAX_TEXT_LEN = 600
# labeling_tool/은 저장소 루트(part1의 부모) 아래에 있다. AI/에서 두 단계 위로 올라간다.
DEFAULT_OUT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "labeling_tool", "trainset"
)


def _h(salt, s):
    """salt별로 다른 순서를 주는 결정적 해시. 같은 입력이면 언제나 같은 결과."""
    return hashlib.md5((salt + "|" + s).encode()).hexdigest()


def pick_key(comment_id):
    """채널 안에서 '어떤 댓글을 뽑을지' 정하는 키."""
    return _h("pick", comment_id)


def interleave(picked):
    """채널별로 뽑아 둔 묶음을 라운드로빈으로 번갈아 배치한다.

    왜 정렬이 아니라 라운드로빈인가:
      첫 판은 여기서 md5(comment_id)로 다시 정렬했는데, **뽑을 때 쓴 키와 같은 키**여서
      채널별 해시 구간이 그대로 보존됐다. 즉 '섞는다'고 써 놓고 아무 일도 하지 않았다.
      그 결과 700건이 청크 1~4 = 진보 성향 채널, 5~7 = 시사 채널로 갈렸고, 나중에
      청크별 우비율 상승(60.6% -> 75.3%)을 '판정 기준이 흔들렸다'로 오진할 뻔했다.
      실제로는 청크마다 채널 구성이 달랐던 것뿐이었다(DECISIONS.md 참고).
      해시로 섞으면 우연히 한쪽으로 쏠릴 수 있으므로, 아예 채널을 번갈아 꺼내
      **모든 청크의 채널 구성이 같아지도록** 보장한다.
    """
    buckets = {g: sorted(v, key=lambda x: _h("mix", x[0])) for g, v in picked.items()}
    chosen = []
    rnd = 0
    while any(buckets.values()):
        # 매 바퀴 채널 순서를 바꿔, 청크 경계가 특정 채널에 항상 걸리지 않게 한다.
        for g in sorted(buckets, key=lambda g: _h(f"r{rnd}", g[0] + g[1])):
            if buckets[g]:
                chosen.append(buckets[g].pop())
        rnd += 1
    return chosen


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="LLM 라벨링용 학습셋 표본 추출")
    p.add_argument("--run", default="llm_train_v1")
    p.add_argument("--size", type=int, default=700)
    p.add_argument("--chunk", type=int, default=100, help="청크 파일당 댓글 수")
    p.add_argument("--out", default=DEFAULT_OUT_DIR, help="청크 JSON을 내보낼 디렉토리")
    args = p.parse_args()
    out_dir = args.out

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()

    cur.execute(
        """INSERT INTO label_runs (run_id, axis, label_source, model_name, note)
           VALUES (%s, 'leaning', 'llm', %s, %s)
           ON CONFLICT (run_id) DO NOTHING""",
        (
            args.run,
            "claude-opus-4-8",
            "guidelines.md 기준으로 LLM이 매긴 학습셋. 댓글 텍스트만 보고 판정(제목 미제공).",
        ),
    )
    conn.commit()

    cur.execute(
        """
        SELECT c.comment_id, c.text, ch.outlet_name, ch.channel_type
        FROM comments c
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE v.is_political
          AND length(c.text) BETWEEN %s AND %s
          -- 평가셋과 절대 겹치면 안 된다
          AND NOT EXISTS (SELECT 1 FROM label_sample s WHERE s.comment_id = c.comment_id)
          -- 이미 어떤 run으로든 라벨이 있는 것도 제외
          AND NOT EXISTS (SELECT 1 FROM comment_labels l WHERE l.comment_id = c.comment_id)
        """,
        (MIN_TEXT_LEN, MAX_TEXT_LEN),
    )
    pool = defaultdict(list)
    for comment_id, text, outlet, ctype in cur.fetchall():
        pool[(outlet, ctype)].append((comment_id, text))

    groups = sorted(pool)
    base, extra = divmod(args.size, len(groups))
    targets = {g: base + (1 if i < extra else 0) for i, g in enumerate(groups)}

    picked = {}
    print(f"{'채널':<24}{'후보':>10}{'선정':>6}")
    for g in groups:
        items = sorted(pool[g], key=lambda x: pick_key(x[0]))
        picked[g] = items[: targets[g]]
        print(f"{g[0] + '/' + g[1]:<24}{len(items):>10,}{len(picked[g]):>6}")

    chosen = interleave(picked)

    # 정말 섞였는지 내보내기 전에 확인한다. '섞는다고 써 놓고 안 섞인' 사고가 있었으므로
    # 주석을 믿지 말고 숫자로 검증한다.
    cid2group = {cid: g for g, items in picked.items() for cid, _ in items}
    per_chunk = [
        Counter(cid2group[cid] for cid, _ in chosen[i : i + args.chunk])
        for i in range(0, len(chosen), args.chunk)
    ]
    worst = max(
        max(c.get(g, 0) for c in per_chunk) - min(c.get(g, 0) for c in per_chunk) for g in groups
    )
    print(f"\n청크 간 채널 편차: 최대 {worst}건 (채널당 청크 평균 {len(chosen)/len(groups)/len(per_chunk):.1f}건)")
    if worst > 2:
        raise SystemExit("중단: 청크별 채널 구성이 고르지 않습니다. interleave()를 확인하세요.")

    os.makedirs(out_dir, exist_ok=True)
    index = {}
    n_chunks = 0
    for i in range(0, len(chosen), args.chunk):
        part = chosen[i : i + args.chunk]
        n_chunks += 1
        rows = []
        for j, (comment_id, text) in enumerate(part, start=i):
            index[j] = comment_id
            rows.append({"n": j, "text": text})
        path = os.path.join(out_dir, f"chunk_{n_chunks:02d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)

    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"run": args.run, "index": index}, f, ensure_ascii=False, indent=1)

    print(f"\n표본 {len(chosen)}건 → 청크 {n_chunks}개 ({out_dir})")
    print(f"run_id: {args.run}")
    conn.close()

if __name__ == "__main__":
    main()
