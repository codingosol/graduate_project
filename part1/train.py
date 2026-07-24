"""라벨을 학습해 댓글 성향 분류기를 만든다.

무엇을 하는가:
  DB에 쌓인 라벨(댓글 + 정답)로 KcELECTRA를 파인튜닝하고, 사람이 매긴 평가셋으로 채점한다.
  결과물은 모델 파일 + 성능 리포트다.

왜 KcELECTRA인가:
  `beomi/KcELECTRA-base-v2022`는 **네이버 뉴스 댓글로 사전학습**된 모델이다(Kc = Korean comments).
  우리 입력이 정확히 그 도메인이라, 맞춤법이 깨지고 자모가 섞이고 신조어가 난무하는 텍스트에
  이미 익숙하다. 위키·뉴스 기사로 학습한 일반 모델(KoBERT 등)은 이 텍스트를 낯설어한다.

무엇을 절대 하지 않는가 — 평가셋 오염:
  `manual_gold_v1`(사람이 손으로 매긴 평가셋)은 **어떤 경로로도 학습에 들어가지 않는다.**
  시험 문제를 미리 보여주고 시험을 치면 점수가 의미를 잃는다. label_sample에 고정된 150건뿐
  아니라 같은 run_id의 잉여 라벨 17건까지 통째로 제외한다 — 그 17건은 나중에 평가셋 2차분을
  뽑을 때 표본으로 편입될 수 있어서, 지금 학습에 쓰면 그때 오염이 된다.

읽는 법 (출력 리포트):
  - `성향 축` 항목이 이 프로젝트의 핵심이다. 채널 점수는 좌/우 판정에서만 나오므로,
    중립·불가를 얼마나 잘 가르는지보다 **좌우 방향을 맞히는지**가 결론을 좌우한다.
  - 맹검 검증에서 사람↔LLM 일치가 좌우 축 92%(κ=0.814)였다. 학습 라벨이 LLM 라벨이므로
    **이 부근이 사실상 성능의 천장**이다. 크게 넘으면 과적합을 의심해야 한다.
  - 채널별 정확도는 평가셋이 채널당 11~12건뿐이라 참고치일 뿐이다(자세한 주의는 출력에 표시).

사용법:
  python train.py                      # 기본: LLM 라벨 700건으로 학습
  python train.py --labels human-first # 맹검 100건은 사람 라벨로 교체해 학습
  python train.py --curve              # 100/250/500/700 학습곡선
  python train.py --save models/v1     # 모델 저장
"""

import argparse
import os
import random
import sys
import time
from collections import Counter, defaultdict

import numpy as np
import psycopg2
import torch
import torch.nn.functional as F
from dotenv import load_dotenv
from torch.utils.data import DataLoader, TensorDataset

from db import ensure_test_database

MODEL_NAME = "beomi/KcELECTRA-base-v2022"
LABELS = ("left", "right", "neutral", "unusable")
L2I = {l: i for i, l in enumerate(LABELS)}
KO = {"left": "좌", "right": "우", "neutral": "중립", "unusable": "불가"}

EVAL_RUN = "manual_gold_v1"   # 사람 손라벨 — 채점 전용, 학습 금지
TRAIN_RUN = "llm_train_v1"    # LLM 라벨 — 학습용
HUMAN_RUN = "blind_check_v1"  # 맹검 재라벨 — TRAIN_RUN과 100건 겹침(의도된 것)

MAX_LEN = 128   # 댓글 평균 45.7자라 충분하다. 길이를 늘리면 메모리만 먹는다.


def set_seed(seed):
    """같은 명령이 같은 결과를 내도록 난수를 고정한다.

    이걸 안 하면 두 번 돌릴 때마다 점수가 달라져서 '이 변경이 성능을 올렸나'를 판단할 수 없다.
    (GPU 연산 일부는 완전 결정적이지 않아 소수점 아래는 흔들릴 수 있다.)
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def load_data(conn, label_source):
    """학습셋과 평가셋을 DB에서 읽는다.

    label_source:
      'llm'         — 학습 라벨 전부 LLM이 매긴 것 (기본)
      'human-first' — 같은 댓글에 사람 라벨(맹검)이 있으면 그것으로 교체
    """
    cur = conn.cursor()

    # ── 평가셋: label_sample에 고정된 것만. ord 순서는 채점에 영향이 없지만 재현성을 위해 정렬.
    cur.execute(
        """
        SELECT c.text, l.label, ch.outlet_name || '/' || ch.channel_type
        FROM label_sample s
        JOIN comment_labels l ON l.comment_id = s.comment_id AND l.run_id = s.run_id
        JOIN comments c  ON c.comment_id = s.comment_id
        JOIN videos v    ON v.video_id   = c.video_id
        JOIN channels ch ON ch.channel_id = v.channel_id
        WHERE s.run_id = %s
        ORDER BY s.ord
        """,
        (EVAL_RUN,),
    )
    ev = cur.fetchall()

    # ── 학습셋: LLM 라벨. 평가셋 run_id를 가진 댓글은 어떤 것도 넣지 않는다(위 docstring 참고).
    cur.execute(
        """
        SELECT c.comment_id, c.text, l.label
        FROM comment_labels l
        JOIN comments c ON c.comment_id = l.comment_id
        WHERE l.run_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM comment_labels g
              WHERE g.comment_id = l.comment_id AND g.run_id = %s
          )
        ORDER BY c.comment_id
        """,
        (TRAIN_RUN, EVAL_RUN),
    )
    tr = cur.fetchall()

    swapped = 0
    if label_source == "human-first":
        cur.execute(
            "SELECT comment_id, label FROM comment_labels WHERE run_id = %s", (HUMAN_RUN,)
        )
        human = dict(cur.fetchall())
        out = []
        for cid, text, lab in tr:
            h = human.get(cid)
            if h and h != lab:
                swapped += 1
            out.append((cid, text, h or lab))
        tr = out

    train = [(t, L2I[l]) for _, t, l in tr]
    evalset = [(t, L2I[l], ch) for t, l, ch in ev]
    return train, evalset, swapped


def encode(tokenizer, texts):
    enc = tokenizer(
        texts, truncation=True, max_length=MAX_LEN, padding="max_length", return_tensors="pt"
    )
    return enc["input_ids"], enc["attention_mask"]


def make_loader(tokenizer, rows, batch, shuffle):
    ids, mask = encode(tokenizer, [r[0] for r in rows])
    y = torch.tensor([r[1] for r in rows])
    return DataLoader(TensorDataset(ids, mask, y), batch_size=batch, shuffle=shuffle)


@torch.no_grad()
def predict(model, loader, device):
    model.eval()
    preds = []
    for ids, mask, _ in loader:
        with torch.autocast("cuda", dtype=torch.float16, enabled=device == "cuda"):
            out = model(input_ids=ids.to(device), attention_mask=mask.to(device)).logits
        preds.append(out.float().argmax(-1).cpu())
    return torch.cat(preds).numpy()


def train_model(train_rows, tokenizer, args, device, quiet=False):
    from transformers import AutoModelForSequenceClassification

    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, num_labels=len(LABELS)
    ).to(device)

    loader = make_loader(tokenizer, train_rows, args.batch, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    steps = len(loader) * args.epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=steps, pct_start=0.1
    )
    # fp16은 메모리를 절반으로 줄이고 속도를 올리는 대신 수치 범위가 좁다. GradScaler가
    # 기울기를 일시적으로 키워 언더플로(작은 값이 0으로 뭉개지는 것)를 막는다.
    scaler = torch.amp.GradScaler("cuda", enabled=device == "cuda")

    weight = None
    if args.class_weights:
        # 클래스마다 개수가 다르면(불가 40건 vs 우 273건) 모델이 드문 클래스를 아예 안 내놓는
        # 쪽이 손해가 적다고 학습해버린다. 빈도의 역수로 가중치를 줘서 균형을 맞춘다.
        cnt = Counter(y for _, y in train_rows)
        w = [len(train_rows) / (len(LABELS) * max(cnt.get(i, 0), 1)) for i in range(len(LABELS))]
        weight = torch.tensor(w, dtype=torch.float, device=device)

    for ep in range(1, args.epochs + 1):
        model.train()
        total = 0.0
        for ids, mask, y in loader:
            opt.zero_grad(set_to_none=True)
            with torch.autocast("cuda", dtype=torch.float16, enabled=device == "cuda"):
                logits = model(input_ids=ids.to(device), attention_mask=mask.to(device)).logits
                loss = F.cross_entropy(logits.float(), y.to(device), weight=weight)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            total += loss.item()
        if not quiet:
            print(f"    epoch {ep}/{args.epochs}  loss {total/len(loader):.4f}")
    return model


def report(y_true, y_pred, channels):
    """채점 결과를 사람이 읽을 수 있게 출력한다."""
    n = len(y_true)
    acc = (y_true == y_pred).mean()
    print(f"\n  전체 정확도  {acc*100:.1f}%  ({int((y_true==y_pred).sum())}/{n})")

    # ── 클래스별 성능. macro-F1은 클래스를 개수와 무관하게 똑같이 취급하므로,
    #    드문 클래스(불가)를 통째로 놓치면 정확도는 멀쩡해도 이 값이 떨어진다.
    print(f"\n  {'라벨':<6}{'정밀도':>8}{'재현율':>8}{'F1':>8}{'실제수':>8}{'예측수':>8}")
    f1s = []
    for i, lab in enumerate(LABELS):
        tp = int(((y_pred == i) & (y_true == i)).sum())
        pp, ap = int((y_pred == i).sum()), int((y_true == i).sum())
        prec = tp / pp if pp else 0.0
        rec = tp / ap if ap else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        f1s.append(f1)
        print(f"  {KO[lab]:<6}{prec*100:>7.0f}%{rec*100:>7.0f}%{f1*100:>7.0f}%{ap:>8}{pp:>8}")
    print(f"  macro-F1 {np.mean(f1s)*100:.1f}%")

    print(f"\n  혼동행렬 (행=정답, 열=예측)")
    print(f"  {'':<6}" + "".join(f"{KO[l]:>6}" for l in LABELS))
    for i, lab in enumerate(LABELS):
        row = [int(((y_true == i) & (y_pred == j)).sum()) for j in range(len(LABELS))]
        print(f"  {KO[lab]:<6}" + "".join(f"{v:>6}" for v in row))

    # ── 성향 축: 채널 점수는 좌/우 판정에서만 만들어진다. 중립·불가를 어떻게 처리하든
    #    좌우 방향이 맞으면 채널 순위는 보존되므로, 이 값이 프로젝트의 실질 성능이다.
    li, ri = L2I["left"], L2I["right"]
    both = ((y_true == li) | (y_true == ri)) & ((y_pred == li) | (y_pred == ri))
    if both.sum():
        axis_acc = (y_true[both] == y_pred[both]).mean()
        flip = int(both.sum() - (y_true[both] == y_pred[both]).sum())
        print(f"\n  ★ 성향 축(좌/우) 정확도  {axis_acc*100:.1f}%  (n={int(both.sum())}, 뒤집힘 {flip}건)")
        print(f"     ※ 맹검에서 사람↔LLM 좌우 일치가 92%였다. 학습 라벨이 LLM 라벨이므로")
        print(f"        이 부근이 사실상 천장이고, 크게 넘으면 과적합을 의심해야 한다.")
    lr_true = ((y_true == li) | (y_true == ri)).sum()
    lr_pred = ((y_pred == li) | (y_pred == ri)).sum()
    print(f"  좌우로 판정한 비율: 정답 {lr_true}/{n} vs 예측 {lr_pred}/{n}")
    print(f"     ※ 예측이 지나치게 적으면 '모르겠다'로 도망친 것이라 표본이 줄어든다.")

    # ── 채널별. 이 프로젝트를 무너뜨릴 수 있는 유일한 위험이 '채널마다 다른 오차'다.
    #    다만 평가셋이 채널당 11~12건뿐이라 지금은 경보음 수준으로만 본다.
    print(f"\n  채널별 정확도 (평가셋이 채널당 11~12건뿐 — 참고치)")
    per = defaultdict(lambda: [0, 0])
    for t, p, ch in zip(y_true, y_pred, channels):
        per[ch][1] += 1
        per[ch][0] += int(t == p)
    for ch in sorted(per, key=lambda c: per[c][0] / per[c][1]):
        ok, tot = per[ch]
        print(f"    {ch:<22}{ok:>3}/{tot:<3} = {ok/tot*100:>3.0f}%")
    print(f"    ※ n=11이면 정확도 {acc*100:.0f}% 모델도 우연히 5/11이 나올 확률이 수 %다.")
    print(f"       채널별로 판단하려면 채널당 100건(총 1,300건)이 필요하다.")
    return acc, float(np.mean(f1s))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="댓글 성향 분류기 학습")
    p.add_argument("--labels", choices=["llm", "human-first"], default="llm",
                   help="학습 라벨 출처. human-first는 맹검 100건을 사람 라벨로 교체")
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--batch", type=int, default=32, help="VRAM 6GB 실측 기준값")
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--class-weights", action="store_true",
                   help="드문 클래스(불가 40건)에 가중치를 줘 균형을 맞춘다")
    p.add_argument("--curve", action="store_true",
                   help="100/250/500/전체로 각각 학습해 학습곡선을 낸다 (스텝 수 고정)")
    p.add_argument("--save", metavar="DIR", help="학습된 모델을 저장할 경로")
    args = p.parse_args()

    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("⚠️ GPU를 찾지 못했습니다. CPU로도 돌아가지만 수십 배 느립니다.\n")

    conn = psycopg2.connect(ensure_test_database(url))
    train_rows, eval_rows, swapped = load_data(conn, args.labels)
    conn.close()

    print(f"학습 {len(train_rows)}건 / 평가 {len(eval_rows)}건   라벨 출처: {args.labels}")
    if swapped:
        print(f"  사람 라벨로 교체된 것 {swapped}건 (나머지는 두 라벨이 같았음)")
    tc = Counter(y for _, y in train_rows)
    print("  학습 분포:", "  ".join(f"{KO[l]} {tc.get(i,0)}" for i, l in enumerate(LABELS)))
    print(f"  모델 {MODEL_NAME} / {device} / batch {args.batch} / epochs {args.epochs}"
          f" / max_len {MAX_LEN}")

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    eval_loader = make_loader(tokenizer, eval_rows, args.batch, shuffle=False)
    y_true = np.array([r[1] for r in eval_rows])
    channels = [r[2] for r in eval_rows]

    sizes = [100, 250, 500, len(train_rows)] if args.curve else [len(train_rows)]

    # 학습곡선은 **스텝 수를 고정**하고 데이터 크기만 바꿔야 한다.
    # epoch을 고정하면 작은 학습셋일수록 스텝이 줄어(100건×15epoch = 60스텝) 학습 자체가
    # 안 되는데, 그걸 "데이터가 부족해서"로 오독하게 된다. 실제로 700건을 4 epoch(88스텝)
    # 돌렸을 때 모델이 좌/불가를 한 번도 예측하지 못한 사고가 있었다.
    steps_per_epoch = lambda n: max(1, -(-n // args.batch))
    target_steps = steps_per_epoch(len(train_rows)) * args.epochs

    results = []
    for size in sizes:
        set_seed(args.seed)   # 크기마다 같은 시드에서 출발해야 비교가 성립한다
        subset = train_rows[:]
        random.shuffle(subset)
        subset = subset[:size]

        run_args = argparse.Namespace(**vars(args))
        if args.curve:
            run_args.epochs = max(1, round(target_steps / steps_per_epoch(size)))

        print(f"\n{'='*66}\n학습셋 {size}건"
              + (f"  (epochs {run_args.epochs} → 약 {steps_per_epoch(size)*run_args.epochs}스텝,"
                 f" 목표 {target_steps}스텝)" if args.curve else ""))
        t0 = time.perf_counter()
        model = train_model(subset, tokenizer, run_args, device, quiet=args.curve)
        y_pred = predict(model, eval_loader, device)
        print(f"  ({time.perf_counter()-t0:.0f}초)")

        if args.curve and size != sizes[-1]:
            acc = (y_true == y_pred).mean()
            f1s = []
            for i in range(len(LABELS)):
                tp = int(((y_pred == i) & (y_true == i)).sum())
                pp, ap = int((y_pred == i).sum()), int((y_true == i).sum())
                pr = tp / pp if pp else 0.0
                rc = tp / ap if ap else 0.0
                f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)

            print(f"  정확도 {acc*100:.1f}%  macro-F1 {np.mean(f1s)*100:.1f}%")
            results.append((size, acc, float(np.mean(f1s))))
        else:
            acc, f1 = report(y_true, y_pred, channels)
            results.append((size, acc, f1))

    if args.curve:
        print(f"\n{'='*66}\n=== 학습곡선 ===")
        print(f"  {'학습셋':>8}{'정확도':>10}{'macro-F1':>11}")
        for size, acc, f1 in results:
            print(f"  {size:>8}{acc*100:>9.1f}%{f1*100:>10.1f}%")
        gain = (results[-1][1] - results[-2][1]) * 100
        print(f"\n  마지막 구간 기울기: {gain:+.1f}%p")
        print("  → 아직 오르는 중이면 라벨을 더 매길 가치가 있고,")
        print("    평평하면 라벨이 아니라 다른 것(규칙·입력 정보)을 손봐야 한다.")

    if args.save:
        os.makedirs(args.save, exist_ok=True)
        model.save_pretrained(args.save)
        tokenizer.save_pretrained(args.save)
        print(f"\n모델 저장: {args.save}")


if __name__ == "__main__":
    main()
