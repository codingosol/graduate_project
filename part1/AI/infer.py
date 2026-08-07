"""학습된 분류기를 전체 댓글에 적용해 결과를 DB·로컬에 적재하고 채널 순위를 낸다.

이것이 Part 1의 결론 단계다. 개별 댓글 정확도(64%)는 중간 지표일 뿐이고, 진짜 질문은
**"채널 순위가 언론 성향 통념과 맞는가"**이다. 개별은 틀려도 채널 점수는 수만 건의 평균이라
무작위 오차가 상쇄돼 순위는 안정적으로 나온다.

설계 원칙:
  1. **Neon 접근은 앞뒤 한 번씩만.** 추론 중(수십 분)에는 DB를 건드리지 않는다.
     시작에 댓글을 한 번 fetch → GPU로 전부 추론 → 완료 후 결과를 청크로 배치 UPSERT.
     추론 도중 매 배치마다 DB에 쓰면 무료 티어에 연결·부하 문제가 생긴다.
  2. **2축 확장 대비.** 스키마(label_runs.axis)가 이미 leaning/intensity를 구분한다.
     축별 후처리를 AXIS_CONFIG로 분리해, 나중에 강도 축(K-HATERS 모델)은 함수 하나만 추가하면 된다.
        - leaning  : label=argmax(좌/우/중립/불가), score=P(우)−P(좌)
        - intensity: (미구현) label=NULL, score=0~1 과격도 — K-HATERS 모델 붙일 때 작성
  3. **진행률이 파이프에 갇히지 않게** flush로 출력하고 `results/<run_id>.progress`에도 기록한다.
     (백그라운드 실행 시 그 파일을 tail 하면 어디까지 됐는지 보인다.)
  4. **DB뿐 아니라 로컬에도** 요약을 남긴다: `results/<run_id>.txt`(채널 순위·분포·메타).

학습과 같은 길이 필터(10~600자)를 추론에도 적용해야 학습/추론 분포가 어긋나지 않는다.

사용법:
  python AI/infer.py                            # leaning, models/kcelectra_v1, DB+로컬 적재
  python AI/infer.py --no-db                    # 적재 없이 순위만
  python AI/infer.py --limit 5000               # 빠른 점검
  (강도 축은 K-HATERS 모델 도입 후) python AI/infer.py --axis intensity --model ... --run model_khaters_v1
"""

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import torch


from train import KO, L2I, LABELS, MAX_LEN

MIN_TEXT_LEN = 10   # sample_trainset.py와 같은 값 — 학습/추론 분포 일치
MAX_TEXT_LEN = 600
DB_CHUNK = 5000     # 배치 UPSERT 청크. 너무 크면 한 쿼리가 무거워지고, 작으면 왕복이 잦다.
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "results")

CONVENTION = {
    "오마이TV": "진보", "MBC 뉴스": "진보", "JTBC News": "진보",
    "TV조선": "보수", "채널A News": "보수", "MBN News": "보수",
    "KBS News": "중도/공영", "YTN": "중도/공영", "SBS 뉴스": "중도/공영",
    "연합뉴스TV": "중도/공영",
}

# ── 축별 후처리 ────────────────────────────────────────────────────────────
# prob: (N, C) softmax 확률 → (label_str 배열, score 배열, confidence 배열)
# label은 comment_labels.label CHECK('left'/'right'/'neutral'/'unusable', NULL 허용)에 맞춘다.


def _postprocess_leaning(prob):
    li, ri = L2I["left"], L2I["right"]
    idx = prob.argmax(axis=1)
    labels = np.array(LABELS, dtype=object)[idx]     # 'left'/'right'/'neutral'/'unusable'
    scores = (prob[:, ri] - prob[:, li]).astype(np.float32)   # P(우)−P(좌)
    conf = prob.max(axis=1).astype(np.float32)
    return labels, scores, conf


def _postprocess_intensity(prob):
    """강도 축(K-HATERS). 클래스 순서 normal<offensive<L1_hate<L2_hate (train_intensity.py와 동일).
    label은 없고(NULL), score = **기대 서열 / 3 = 0~1 과격도**. confidence = max 확률."""
    n, k = prob.shape                                  # k=4
    ranks = np.arange(k, dtype=np.float32)             # [0,1,2,3]
    scores = ((prob * ranks).sum(axis=1) / (k - 1)).astype(np.float32)  # 0~1
    conf = prob.max(axis=1).astype(np.float32)
    labels = np.array([None] * n, dtype=object)        # 강도 축은 이산 라벨을 쓰지 않는다
    return labels, scores, conf


AXIS_CONFIG = {
    "leaning": {
        "post": _postprocess_leaning,
        "default_model": os.path.join(os.path.dirname(__file__), "..", "..", "models", "kcelectra_v2"),
        "default_run": "model_kcelectra_v2",
    },
    "intensity": {
        "post": _postprocess_intensity,
        "default_model": os.path.join(os.path.dirname(__file__), "..", "..", "models", "khaters_v1"),
        "default_run": "model_khaters_v1",
    },
}


def progress_writer(run_id, total):
    """진행률을 stdout(flush)과 results/<run_id>.progress 양쪽에 쓴다."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{run_id}.progress")
    t0 = time.perf_counter()

    def cb(done):
        pct = done / total * 100
        el = time.perf_counter() - t0
        eta = el / done * (total - done) if done else 0
        msg = f"추론 {done:,}/{total:,} ({pct:.1f}%)  경과 {el:.0f}s  남음 ~{eta:.0f}s"
        print("  " + msg, flush=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    return cb, path


@torch.no_grad()
def run_inference(model, tokenizer, texts, batch, device, post, progress_cb):
    """배치 추론 → 축별 후처리 결과(label, score, confidence)를 numpy로 모은다.

    **길이순 버킷팅**: 텍스트를 길이순으로 정렬해 비슷한 길이끼리 한 배치에 넣는다. padding은
    배치 내 최댓값까지 채워지는데, 무작위 순서면 짧은 댓글이 긴 댓글 하나 때문에 통째로 패딩돼
    GPU가 헛일을 한다(댓글 길이 편차가 큼). 정렬하면 패딩이 최소화돼 추론이 빨라진다.
    결과는 원래 순서로 되돌려 반환하므로 ids/like 등과의 정렬이 유지된다(수치도 동일)."""
    n = len(texts)
    order = sorted(range(n), key=lambda i: len(texts[i]))   # 길이순 인덱스
    labels = np.empty(n, dtype=object)
    scores = np.empty(n, dtype=np.float32)
    confs = np.empty(n, dtype=np.float32)
    done = 0
    for b in range(0, n, batch):
        idx = order[b : b + batch]
        enc = tokenizer([texts[i] for i in idx], truncation=True, max_length=MAX_LEN,
                        padding=True, return_tensors="pt")
        enc = {k: v.to(device) for k, v in enc.items()}
        with torch.autocast("cuda", dtype=torch.float16, enabled=device == "cuda"):
            logits = model(**enc).logits
        prob = torch.softmax(logits.float(), dim=-1).cpu().numpy()
        l, s, c = post(prob)
        for j, i in enumerate(idx):     # 원위치로 되돌려 저장
            labels[i], scores[i], confs[i] = l[j], s[j], c[j]
        done += len(idx)
        if (b // batch) % 100 == 0:
            progress_cb(done)
    progress_cb(n)
    return labels, scores, confs


def save_to_db(conn, run_id, axis, model_name, ids, labels, scores, confs):
    """추론이 **전부 끝난 뒤** 결과를 배치로 적재한다(추론 중 DB 접근 없음).

    label_runs에 run을 등록하고 comment_labels에 청크 단위로 UPSERT.
    intensity 축은 label이 없으므로 NULL로 넣는다(스키마가 NULL 허용).
    """
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO label_runs (run_id, axis, label_source, model_name, note)
           VALUES (%s, %s, 'model', %s, %s)
           ON CONFLICT (run_id) DO UPDATE SET model_name = EXCLUDED.model_name,
                                              note = EXCLUDED.note""",
        (run_id, axis, model_name, f"{axis} 축 모델 추론 결과 ({len(ids):,}건)"),
    )
    conn.commit()

    label_ok = axis == "leaning"   # leaning만 label 문자열을 넣고, intensity는 NULL
    rows = [
        (run_id, cid, (str(lab) if label_ok else None), float(sc), float(cf))
        for cid, lab, sc, cf in zip(ids, labels, scores, confs)
    ]
    for i in range(0, len(rows), DB_CHUNK):
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO comment_labels (run_id, comment_id, label, score, confidence)
               VALUES %s
               ON CONFLICT (run_id, comment_id)
               DO UPDATE SET label = EXCLUDED.label, score = EXCLUDED.score,
                             confidence = EXCLUDED.confidence""",
            rows[i : i + DB_CHUNK],
        )
        conn.commit()
        print(f"  DB 적재 {min(i + DB_CHUNK, len(rows)):,}/{len(rows):,}", flush=True)


def _agg_from_rows(rows, labels, scores):
    """--no-db 경로: 방금 추론한 것만으로 채널별 합계 집계(전체 run이 아니라 이번 배치)."""
    df = pd.DataFrame({
        "outlet": [r[2] for r in rows], "ctype": [r[3] for r in rows],
        "like": np.array([r[4] for r in rows], dtype=np.int64),
        "label": labels, "score": scores,
    })
    pol = df[df["label"] != "unusable"].copy()
    pol["w"] = np.log1p(pol["like"].clip(lower=0))
    pol["sw"] = pol["score"] * pol["w"]
    agg = pol.groupby(["outlet", "ctype"], as_index=False).agg(
        n=("score", "size"), s_score=("score", "sum"), s_sw=("sw", "sum"), s_w=("w", "sum"))
    dist = pd.Series(labels).value_counts().to_dict()
    return agg, dist


def fetch_run_aggregates(conn, run_id, axis):
    """전체 run 기준 채널별 합계를 **SQL에서** 집계한다(증분 추론 후에도 순위가 전체를 반영).
    60만 행을 파이썬으로 끌어오지 않고 채널 ~13행만 받아 온다.
    leaning은 unusable(잡음)을 빼고 집계하지만, intensity는 label이 NULL이라 전량 집계한다."""
    cur = conn.cursor()
    # ⚠️ NULL <> 'unusable' 는 NULL(거짓)이라 intensity에 이 필터를 걸면 전부 빠진다. axis로 분기.
    unusable_filter = "AND cl.label <> 'unusable'" if axis == "leaning" else ""
    cur.execute(f"""
        SELECT ch.outlet_name, ch.channel_type,
               count(*)                                                   AS n,
               sum(cl.score)                                              AS s_score,
               sum(cl.score * ln((1 + greatest(c.like_count, 0))::float8)) AS s_sw,
               sum(ln((1 + greatest(c.like_count, 0))::float8))           AS s_w
        FROM comment_labels cl
        JOIN comments c  ON c.comment_id = cl.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE cl.run_id = %s AND v.is_political {unusable_filter}
        GROUP BY ch.outlet_name, ch.channel_type
    """, (run_id,))
    agg = pd.DataFrame(cur.fetchall(),
                       columns=["outlet", "ctype", "n", "s_score", "s_sw", "s_w"])
    for col in ("s_score", "s_sw", "s_w"):
        agg[col] = agg[col].astype(float)
    dist = {}
    if axis == "leaning":   # intensity는 label이 NULL이라 라벨 분포가 의미 없다
        cur.execute("SELECT label, count(*) FROM comment_labels WHERE run_id = %s GROUP BY label",
                    (run_id,))
        dist = {lbl: n for lbl, n in cur.fetchall()}
    return agg, dist


def ranking_text(agg, title, axis="leaning", only_type=None):
    """채널별 순위를 문자열로 만든다. agg는 (outlet, ctype, n, s_score, s_sw, s_w) 합계.
    leaning: 점수=P(우)−P(좌), 진보(음수)→보수(양수) 오름차순, 통념 열 표시.
    intensity: 점수=과격도 0~1, 과격한 순(내림차순), 통념 열 없음."""
    d = agg if only_type is None else agg[agg["ctype"] == only_type]
    if d.empty:
        return ""
    if only_type is None:
        g = d.copy()                                    # outlet/ctype 각각 한 줄
    else:
        g = d.groupby("outlet", as_index=False)[["n", "s_score", "s_sw", "s_w"]].sum()
    g["mean"] = g["s_score"] / g["n"]
    g["wmean"] = np.where(g["s_w"] > 0, g["s_sw"] / g["s_w"], g["mean"])
    leaning = axis == "leaning"
    g = g.sort_values("mean", ascending=leaning)        # 과격도는 높은 순으로

    if leaning:
        out = [f"\n{'='*72}\n{title}  (음수=진보 / 양수=보수)",
               f"  {'채널':<20}{'단순평균':>9}{'좋아요가중':>11}{'통념':>10}{'표본':>9}"]
    else:
        out = [f"\n{'='*72}\n{title}  (과격도 0=온건 ~ 1=과격, 높을수록 과격)",
               f"  {'채널':<20}{'단순평균':>9}{'좋아요가중':>11}{'표본':>11}"]
    for _, r in g.iterrows():
        name = f"{r['outlet']}/{r['ctype']}" if only_type is None else r["outlet"]
        if leaning:
            conv = CONVENTION.get(r["outlet"], "—")
            out.append(f"  {name:<20}{r['mean']:>+9.3f}{r['wmean']:>+11.3f}{conv:>10}{int(r['n']):>9,}")
        else:
            out.append(f"  {name:<20}{r['mean']:>9.3f}{r['wmean']:>11.3f}{int(r['n']):>11,}")
    return "\n".join(out)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="채널별 성향 점수 산출 + DB·로컬 적재")
    p.add_argument("--axis", choices=list(AXIS_CONFIG), default="leaning")
    p.add_argument("--model", help="모델 경로 (기본: 축별 default)")
    p.add_argument("--run", help="run_id (기본: 축별 default)")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--limit", type=int, help="빠른 점검용 댓글 수 제한")
    p.add_argument("--no-db", action="store_true", help="DB 적재 없이 순위만 출력(이번 배치 기준)")
    p.add_argument("--reinfer", action="store_true",
                   help="이미 라벨된 댓글도 전부 다시 추론(모델 교체 시). 기본은 미라벨분만(증분).")
    args = p.parse_args()

    cfg = AXIS_CONFIG[args.axis]
    model_path = args.model or cfg["default_model"]
    run_id = args.run or cfg["default_run"]

    __import__("dotenv").load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    if not os.path.isdir(model_path):
        raise SystemExit(f"모델을 찾을 수 없습니다: {model_path}\n먼저 train.py --save 로 저장하세요.")
    # 증분(기본): 이 run_id로 아직 라벨이 없는 정치 댓글만 추론한다. 수집이 계속돼도
    # 매번 전량(59.7만)을 다시 돌리지 않는다. --reinfer면 전량 재추론(모델 교체 시 덮어쓰기).
    incremental = not args.reinfer and not args.no_db
    print(f"축 {args.axis} / run_id {run_id} / 모델 {model_path}")
    print(f"모드: {'증분(미라벨분만)' if incremental else '전량 재추론'}"
          f"{' / DB 미적재' if args.no_db else ''}")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSequenceClassification.from_pretrained(model_path).to(device).eval()

    # ── ① 시작: 추론 대상 fetch (이후 추론 끝까지 DB 연결을 닫아 둔다) ──
    t_fetch = time.perf_counter()
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    q = """
        SELECT c.comment_id, c.text, ch.outlet_name, ch.channel_type, c.like_count
        FROM comments c
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE v.is_political AND length(c.text) BETWEEN %s AND %s
        """
    params = [MIN_TEXT_LEN, MAX_TEXT_LEN]
    if incremental:   # 이 run으로 아직 라벨 없는 것만 (PK(run_id,comment_id) 인덱스 조회)
        q += """ AND NOT EXISTS (SELECT 1 FROM comment_labels cl
                                 WHERE cl.run_id = %s AND cl.comment_id = c.comment_id)"""
        params.append(run_id)
    if args.limit:
        q += " LIMIT %s"; params.append(args.limit)
    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()
    fetch_sec = time.perf_counter() - t_fetch
    print(f"fetch {len(rows):,}건 / {fetch_sec:.1f}s / {device} / batch {args.batch}")

    # ── ② 추론: GPU만, DB 무접근 ──
    infer_sec = push_sec = 0.0
    ids = labels = scores = confs = None
    if rows:
        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        cb, prog_path = progress_writer(run_id, len(rows))
        t_inf = time.perf_counter()
        labels, scores, confs = run_inference(model, tokenizer, texts, args.batch, device,
                                              cfg["post"], cb)
        infer_sec = time.perf_counter() - t_inf
        if os.path.exists(prog_path):
            os.remove(prog_path)
        print(f"추론 완료 ({infer_sec:.0f}s)")
    else:
        print("추론할 미라벨 댓글이 없습니다(이미 최신) — 순위만 갱신합니다.")

    # ── ③ 적재 + ④ 집계: 추론 뒤 연결 하나로 처리 ──
    if args.no_db:
        if not rows:
            print("표시할 데이터가 없습니다."); return
        agg, dist = _agg_from_rows(rows, labels, scores)
    else:
        conn = psycopg2.connect(url)
        if rows:
            t_push = time.perf_counter()
            save_to_db(conn, run_id, args.axis, os.path.basename(model_path.rstrip("/\\")),
                       ids, labels, scores, confs)
            push_sec = time.perf_counter() - t_push
            print(f"DB 적재 완료 ({push_sec:.0f}s): comment_labels (run_id='{run_id}')")
        agg, dist = fetch_run_aggregates(conn, run_id, args.axis)  # 전체 run 기준(증분이어도 전체 반영)
        conn.close()

    total_run = int(sum(dist.values())) if dist else int(agg["n"].sum())
    blocks = [f"run_id: {run_id}   축: {args.axis}   모델: {model_path}",
              f"생성: {time.strftime('%Y-%m-%d %H:%M')}   "
              f"이번 실행 {len(rows):,}건 추론 / run 전체 {total_run:,}건",
              f"소요: fetch {fetch_sec:.1f}s · infer {infer_sec:.0f}s · push {push_sec:.0f}s"]
    if args.axis == "leaning":
        dist_line = "  ".join(f"{KO.get(l, l)} {int(dist.get(l, 0)):,}" for l in LABELS)
        blocks.append(f"분류 분포(run 전체): {dist_line}")
        blocks.append(ranking_text(agg, "전체 (news + opinion)", args.axis, None))
        blocks.append(ranking_text(agg, "news 채널만", args.axis, "news"))
        blocks.append(ranking_text(agg, "opinion(시사) 채널만", args.axis, "opinion"))
    else:   # intensity — 채널 과격도 순위
        tot_n = agg["n"].sum()
        overall = agg["s_score"].sum() / tot_n if tot_n else 0.0
        blocks.append(f"전체 평균 과격도: {overall:.3f} (0=온건 ~ 1=과격)")
        blocks.append(ranking_text(agg, "채널 과격도 — 전체 (news + opinion)", args.axis, None))
        blocks.append(ranking_text(agg, "채널 과격도 — news만", args.axis, "news"))
        blocks.append(ranking_text(agg, "채널 과격도 — opinion만", args.axis, "opinion"))
    report = "\n".join(b for b in blocks if b)
    print("\n" + report)

    if args.limit:
        print("\n(--limit 부분 실행이라 로컬 요약 파일은 덮어쓰지 않음)")
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    summary_path = os.path.join(RESULTS_DIR, f"{run_id}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\n로컬 요약 저장: {summary_path}")

if __name__ == "__main__":
    main()
