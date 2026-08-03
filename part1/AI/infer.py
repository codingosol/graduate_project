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

AXIS_CONFIG = {
    "leaning": {
        "post": _postprocess_leaning,
        "default_model": os.path.join(os.path.dirname(__file__), "..", "..", "models", "kcelectra_v2"),
        "default_run": "model_kcelectra_v2",
    },
    # "intensity": {  # 강도 축 도입(K-HATERS) 시 여기에 후처리·기본 모델·run_id를 추가.
    #     "post": _postprocess_intensity,   # label=None, score=과격도(0~1)
    #     "default_model": ".../models/khaters_v1",
    #     "default_run": "model_khaters_v1",
    # },
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
    """배치 추론 → 축별 후처리 결과(label, score, confidence)를 numpy로 모은다."""
    labels, scores, confs = [], [], []
    n = len(texts)
    for i in range(0, n, batch):
        enc = tokenizer(texts[i : i + batch], truncation=True, max_length=MAX_LEN,
                        padding=True, return_tensors="pt")
        enc = {k: v.to(device) for k, v in enc.items()}
        with torch.autocast("cuda", dtype=torch.float16, enabled=device == "cuda"):
            logits = model(**enc).logits
        prob = torch.softmax(logits.float(), dim=-1).cpu().numpy()
        l, s, c = post(prob)
        labels.append(l); scores.append(s); confs.append(c)
        if (i // batch) % 100 == 0:
            progress_cb(min(i + batch, n))
    progress_cb(n)
    return np.concatenate(labels), np.concatenate(scores), np.concatenate(confs)


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


def ranking_text(df, title, only_type=None):
    """채널별 성향 순위를 문자열로 만든다(화면·파일 공용). intensity 축엔 호출하지 않는다."""
    d = df if only_type is None else df[df.ctype == only_type]
    pol = d[d.label != "unusable"].copy()
    if pol.empty:
        return ""
    pol["w"] = np.log1p(pol["like"].clip(lower=0))
    pol["sw"] = pol["score"] * pol["w"]
    key = ["outlet", "ctype"] if only_type is None else ["outlet"]
    g = pol.groupby(key).agg(n=("score", "size"), mean=("score", "mean"),
                             sw=("sw", "sum"), w=("w", "sum")).reset_index()
    g["wmean"] = np.where(g["w"] > 0, g["sw"] / g["w"], g["mean"])
    g = g.sort_values("mean")

    out = [f"\n{'='*72}\n{title}  (음수=진보 / 양수=보수)",
           f"  {'채널':<20}{'단순평균':>9}{'좋아요가중':>11}{'통념':>10}{'표본':>9}"]
    for _, r in g.iterrows():
        name = f"{r['outlet']}/{r['ctype']}" if only_type is None else r["outlet"]
        conv = CONVENTION.get(r["outlet"], "—")
        out.append(f"  {name:<20}{r['mean']:>+9.3f}{r['wmean']:>+11.3f}{conv:>10}{int(r['n']):>9,}")
    return "\n".join(out)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="채널별 성향 점수 산출 + DB·로컬 적재")
    p.add_argument("--axis", choices=list(AXIS_CONFIG), default="leaning")
    p.add_argument("--model", help="모델 경로 (기본: 축별 default)")
    p.add_argument("--run", help="run_id (기본: 축별 default)")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--limit", type=int, help="빠른 점검용 댓글 수 제한")
    p.add_argument("--no-db", action="store_true", help="DB 적재 없이 순위만 출력")
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
    print(f"축 {args.axis} / run_id {run_id} / 모델 {model_path}")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSequenceClassification.from_pretrained(model_path).to(device).eval()

    # ── ① 시작: 댓글을 한 번 fetch (이후 추론 끝까지 DB 접근 없음) ──
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
    if args.limit:
        q += " LIMIT %s"; params.append(args.limit)
    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()   # 추론 동안 연결을 닫아 둔다(무료 티어 유휴 연결 정리)
    print(f"추론 대상 {len(rows):,}건 / {device} / batch {args.batch}\n")

    # ── ② 추론: GPU만 사용, DB 무접근, 진행률 flush + 파일 ──
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    cb, prog_path = progress_writer(run_id, len(rows))
    t0 = time.perf_counter()
    labels, scores, confs = run_inference(model, tokenizer, texts, args.batch, device,
                                          cfg["post"], cb)
    infer_sec = time.perf_counter() - t0
    print(f"추론 완료 ({infer_sec:.0f}초)")

    # ── ③ 완료 후: DB 배치 적재 ──
    if not args.no_db:
        conn = psycopg2.connect(url)
        save_to_db(conn, run_id, args.axis, os.path.basename(model_path.rstrip("/\\")),
                   ids, labels, scores, confs)
        conn.close()
        print(f"DB 적재 완료: comment_labels (run_id='{run_id}')")

    # ── ④ 집계 + 로컬 요약 ──
    df = pd.DataFrame({
        "outlet": [r[2] for r in rows], "ctype": [r[3] for r in rows],
        "like": np.array([r[4] for r in rows], dtype=np.int32),
        "label": labels, "score": scores,
    })
    dist = pd.Series(labels).value_counts()
    dist_line = "  ".join(f"{KO.get(l, l)} {int(dist.get(l, 0)):,}" for l in LABELS)

    blocks = [f"run_id: {run_id}   축: {args.axis}   모델: {model_path}",
              f"생성: {time.strftime('%Y-%m-%d %H:%M')}   추론 {len(rows):,}건 / {infer_sec:.0f}초",
              f"분류 분포: {dist_line}"]
    if args.axis == "leaning":
        blocks.append(ranking_text(df, "전체 (news + opinion)", None))
        blocks.append(ranking_text(df, "news 채널만", "news"))
        blocks.append(ranking_text(df, "opinion(시사) 채널만", "opinion"))
    report = "\n".join(b for b in blocks if b)
    print("\n" + report)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    summary_path = os.path.join(RESULTS_DIR, f"{run_id}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    if os.path.exists(prog_path):
        os.remove(prog_path)   # 완료됐으므로 진행률 파일 정리
    print(f"\n로컬 요약 저장: {summary_path}")

if __name__ == "__main__":
    main()
