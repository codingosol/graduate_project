"""LLM 학습셋 라벨의 신뢰도를 '맹검 일치율'로 잰다.

왜 필요한가:
  학습셋 700건은 내가(LLM) `guidelines.md`를 보고 매긴 것이다. 분포가 손라벨 평가셋과
  비슷하다는 것만으로는 "기준을 같게 적용했다"의 증거가 못 된다 — 분포가 같아도 개별
  판정이 서로 어긋날 수 있기 때문이다. 같은 댓글을 사람이 **LLM 라벨을 보지 않고**
  다시 매겨서 건별로 맞춰봐야 한다.

왜 700건 전부를 사람이 다시 매기지 않는가:
  그러면 LLM 라벨링을 쓴 의미가 없어진다(손라벨 700건이 되는 것). 게다가 LLM 라벨을
  보면서 고치면 **앵커링**이 걸려 일치율이 부풀려진다. 100건이면 일치율 80% 가정 시
  95% 신뢰구간이 ±8%p로, "쓸 만한가"를 가르기에 충분하다.

맹검을 어떻게 보장하는가:
  라벨링 툴은 `comment_labels`를 **run_id로 조인**해서 읽는다. 새 run_id로 표본을 만들면
  같은 댓글이라도 LLM 라벨(run_id='llm_train_v1')이 조회되지 않아 빈 화면으로 뜬다.
  코드를 고치지 않고 스키마만으로 맹검이 성립한다.

사용법:
  python blind_check.py prepare            # 100건을 새 run으로 고정
  python label.py --run blind_check_v1 --hide-title   # (labeling_tool/에서) 사람이 라벨링
  python blind_check.py compare            # 일치율·kappa·혼동행렬
"""

import argparse
import hashlib
import os
import sys
from collections import Counter, defaultdict

import psycopg2
from dotenv import load_dotenv


SOURCE_RUN = "llm_train_v1"
BLIND_RUN = "blind_check_v1"
LABELS = ("left", "right", "neutral", "unusable")
KO = {"left": "좌", "right": "우", "neutral": "중립", "unusable": "불가"}


def connect():
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")
    return psycopg2.connect(url)


def prepare(conn, size):
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM label_sample WHERE run_id = %s", (BLIND_RUN,))
    if cur.fetchone()[0]:
        raise SystemExit(
            f"{BLIND_RUN} 표본이 이미 있습니다. 다시 뽑으면 진행 중인 라벨과 어긋나므로 중단합니다.\n"
            f"정말 새로 뽑으려면 label_sample/comment_labels에서 run_id='{BLIND_RUN}'을 먼저 지우세요."
        )

    cur.execute(
        """
        SELECT l.comment_id, ch.outlet_name || '/' || ch.channel_type
        FROM comment_labels l
        JOIN comments c  ON c.comment_id = l.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE l.run_id = %s
        """,
        (SOURCE_RUN,),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"{SOURCE_RUN} 라벨이 없습니다. ingest_trainset.py를 먼저 실행하세요.")

    # 채널별로 비례 배분한다. 무작위로만 뽑으면 우연히 한 채널에 쏠릴 수 있고,
    # 그러면 일치율이 그 채널 어휘에 대한 일치율이 되어 버린다.
    by_ch = defaultdict(list)
    for cid, ch in rows:
        by_ch[ch].append(cid)
    groups = sorted(by_ch)
    base, extra = divmod(size, len(groups))

    chosen = []
    for i, g in enumerate(groups):
        target = base + (1 if i < extra else 0)
        # 학습셋을 뽑을 때와 다른 salt를 쓴다. 같은 키를 재사용하면 정렬이 아무 일도
        # 하지 않는 사고가 난다(DECISIONS.md 07-22 (3)).
        items = sorted(by_ch[g], key=lambda c: hashlib.md5(("blind|" + c).encode()).hexdigest())
        chosen.extend(items[:target])

    # 제시 순서도 채널이 번갈아 나오게 섞는다(한 채널을 연달아 보면 판단이 그 채널에 고정된다).
    chosen.sort(key=lambda c: hashlib.md5(("ord|" + c).encode()).hexdigest())

    cur.execute(
        # label_source는 0004의 CHECK로 ('manual','model','llm')만 허용된다. 사람이 매기므로 manual.
        """INSERT INTO label_runs (run_id, axis, label_source, note)
           VALUES (%s, 'leaning', 'manual', %s)
           ON CONFLICT (run_id) DO NOTHING""",
        (BLIND_RUN, f"{SOURCE_RUN} 학습셋 {size}건 맹검 재라벨 — LLM 라벨과의 일치율 측정용"),
    )
    cur.executemany(
        "INSERT INTO label_sample (run_id, comment_id, ord) VALUES (%s, %s, %s)",
        [(BLIND_RUN, cid, i) for i, cid in enumerate(chosen, start=1)],
    )
    conn.commit()

    print(f"맹검 표본 {len(chosen)}건 고정 (run_id={BLIND_RUN})")
    print(f"  채널 {len(groups)}개에 비례 배분")
    print("\n다음 단계:")
    print("  cd labeling_tool")
    print("  python label.py --run blind_check_v1 --hide-title")
    print("  → LLM 라벨은 보이지 않습니다(run_id가 달라 조인되지 않음).")
    print("  → --hide-title 필수: LLM은 제목 없이 매겼으므로 사람도 댓글 텍스트만 봐야")
    print("     불일치가 '정보량 차이'가 아니라 '판단 기준 차이'를 뜻한다.")


def compare(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT h.label, m.label, ch.outlet_name || '/' || ch.channel_type
        FROM label_sample s
        JOIN comment_labels h ON h.comment_id = s.comment_id AND h.run_id = s.run_id
        JOIN comment_labels m ON m.comment_id = s.comment_id AND m.run_id = %s
        JOIN comments c  ON c.comment_id = s.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE s.run_id = %s
        """,
        (SOURCE_RUN, BLIND_RUN),
    )
    rows = cur.fetchall()
    cur.execute("SELECT count(*) FROM label_sample WHERE run_id = %s", (BLIND_RUN,))
    total = cur.fetchone()[0]
    if not rows:
        raise SystemExit(f"아직 맹검 라벨이 없습니다 (표본 {total}건). label.py --run {BLIND_RUN} 먼저.")

    n = len(rows)
    agree = sum(1 for h, m, _ in rows if h == m)
    print(f"=== 맹검 일치율 ({n}/{total}건 완료) ===")
    print(f"  단순 일치율  {agree}/{n} = {agree/n*100:.1f}%")

    # Cohen's kappa: 우연히 맞을 확률을 뺀 일치율. 분포가 한쪽으로 쏠린 데이터에서는
    # 단순 일치율이 실제보다 후하게 나오므로 같이 본다.
    hc, mc = Counter(h for h, _, _ in rows), Counter(m for _, m, _ in rows)
    pe = sum(hc[k] * mc[k] for k in LABELS) / (n * n)
    po = agree / n
    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    grade = ("거의 완전" if kappa >= .8 else "상당" if kappa >= .6 else
             "보통" if kappa >= .4 else "약함")
    print(f"  Cohen's kappa {kappa:.3f} ({grade})   ※ 우연 일치 기대치 {pe*100:.1f}%")

    print("\n=== 혼동행렬 (행=사람, 열=LLM) ===")
    m2 = defaultdict(Counter)
    for h, m, _ in rows:
        m2[h][m] += 1
    print(f"{'':<8}" + "".join(f"{KO[k]:>6}" for k in LABELS) + f"{'계':>6}")
    for h in LABELS:
        r = m2[h]
        print(f"{KO[h]:<8}" + "".join(f"{r[k]:>6}" for k in LABELS) + f"{sum(r.values()):>6}")

    # 좌/우를 반대로 부른 것은 중립과 혼동한 것보다 훨씬 나쁘다(채널 점수를 두 배로 민다).
    flip = m2["left"]["right"] + m2["right"]["left"]
    print(f"\n좌↔우 정반대 판정: {flip}건 ({flip/n*100:.1f}%)  ← 채널 점수를 가장 크게 왜곡하는 유형")

    print("\n=== 어긋난 항목이 몰린 채널 ===")
    per = defaultdict(lambda: [0, 0])
    for h, m, ch in rows:
        per[ch][1] += 1
        per[ch][0] += (h == m)
    for ch in sorted(per, key=lambda c: per[c][0] / per[c][1]):
        ok, tot = per[ch]
        print(f"  {ch:<22}{ok}/{tot} = {ok/tot*100:>5.0f}%")
    print("\n※ 특정 채널만 유독 낮으면 그 채널 어휘에 대한 기준이 흔들린 것이라 순위가 왜곡된다.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="LLM 학습셋 라벨의 맹검 일치율 측정")
    p.add_argument("cmd", choices=["prepare", "compare"])
    p.add_argument("--size", type=int, default=100)
    args = p.parse_args()

    conn = connect()
    if args.cmd == "prepare":
        prepare(conn, args.size)
    else:
        compare(conn)
    conn.close()

if __name__ == "__main__":
    main()
