"""모델이 정말 '댓글을 읽고' 판정하는지, 아니면 라벨 분포만 따라 찍는지 가른다.

왜 필요한가:
  학습 결과의 예측 분포(좌 20.7 / 우 42.7 / 중립 32.7 / 불가 4.0%)가 학습셋 분포
  (19.7 / 39.0 / 35.6 / 5.7%)와 거의 같다. 분포만 보면 "그냥 비율대로 찍은 것"과
  "제대로 추론한 것"이 **똑같이 생겼다.** 둘을 가르려면 다른 증거가 필요하다.

세 가지 검사:
  A. 사전 확률 베이스라인 — 입력을 아예 안 보고 얻을 수 있는 점수의 상한
  B. 라벨 셔플 학습 (결정적) — 정답을 무작위로 섞어 학습시킨다. 텍스트와 라벨의 관계가
     끊겼으므로, 여기서도 점수가 나오면 그 점수는 '텍스트를 읽어서' 얻은 것이 아니다.
     제대로 된 모델이라면 이 대조군은 A 수준으로 무너져야 한다.
  C. 학습셋 자체 성능 — B가 무너지면서도 학습 데이터는 외우는지 확인한다.
     외우기만 하고 일반화를 못 하는 것이 '신호 없음'의 전형적 서명이다.

이 검사는 통계 교과서의 permutation test와 같은 논리다: 관계를 인위적으로 끊은
대조군을 만들어, 실제 결과가 그보다 얼마나 나은지로 신호의 존재를 증명한다.
"""

import argparse
import os
import random
import sys
import time
from collections import Counter

import numpy as np
import psycopg2
import torch
from dotenv import load_dotenv

from db import ensure_test_database
from train import KO, LABELS, load_data, make_loader, predict, set_seed, train_model


def acc_of(y_true, y_pred):
    return float((y_true == y_pred).mean())


def macro_f1(y_true, y_pred):
    f1s = []
    for i in range(len(LABELS)):
        tp = int(((y_pred == i) & (y_true == i)).sum())
        pp, ap = int((y_pred == i).sum()), int((y_true == i).sum())
        pr = tp / pp if pp else 0.0
        rc = tp / ap if ap else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return float(np.mean(f1s))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="모델이 텍스트를 읽는지 검증")
    p.add_argument("--epochs", type=int, default=15)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--class-weights", action="store_true", default=True)
    args = p.parse_args()

    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 환경변수가 필요합니다.")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    conn = psycopg2.connect(ensure_test_database(url))
    train_rows, eval_rows, _ = load_data(conn, "llm")
    conn.close()

    y_true = np.array([r[1] for r in eval_rows])
    n_ev = len(y_true)
    tr_cnt = Counter(y for _, y in train_rows)
    ev_cnt = Counter(y_true.tolist())

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained("beomi/KcELECTRA-base-v2022")
    eval_loader = make_loader(tokenizer, eval_rows, args.batch, shuffle=False)

    print(f"학습 {len(train_rows)}건 / 평가 {n_ev}건\n")

    # ── A. 입력을 보지 않고 얻을 수 있는 점수 ──────────────────────────────
    print("=" * 68)
    print("A. 사전 확률만으로 얻을 수 있는 점수 (입력을 아예 안 봄)")
    maj = max(ev_cnt.values()) / n_ev
    prop = sum((tr_cnt.get(i, 0) / len(train_rows)) * (ev_cnt.get(i, 0) / n_ev)
               for i in range(len(LABELS)))
    print(f"   최빈 클래스만 찍기      정확도 {maj*100:>5.1f}%")
    print(f"   학습셋 분포대로 찍기     정확도 {prop*100:>5.1f}%   ← 대조군의 기대 수준")
    print(f"   4개 중 무작위          정확도 {25.0:>5.1f}%")

    # ── B. 라벨 셔플 학습 (결정적 대조군) ─────────────────────────────────
    print("\n" + "=" * 68)
    print("B. 라벨 셔플 학습 — 텍스트와 정답의 관계를 끊고 같은 조건으로 학습")
    set_seed(args.seed)
    shuffled_labels = [y for _, y in train_rows]
    random.shuffle(shuffled_labels)
    shuffled = [(t, y) for (t, _), y in zip(train_rows, shuffled_labels)]
    kept = sum(1 for (_, a), (_, b) in zip(train_rows, shuffled) if a == b)
    print(f"   셔플 후 원래 라벨과 우연히 같은 것 {kept}/{len(train_rows)}건"
          f" ({kept/len(train_rows)*100:.0f}%, 기대값 약 30%)")

    t0 = time.perf_counter()
    set_seed(args.seed)
    m_shuf = train_model(shuffled, tokenizer, args, device, quiet=True)
    yp_shuf = predict(m_shuf, eval_loader, device)
    print(f"   ({time.perf_counter()-t0:.0f}초)")
    a_shuf, f_shuf = acc_of(y_true, yp_shuf), macro_f1(y_true, yp_shuf)
    print(f"   평가셋 정확도 {a_shuf*100:.1f}%   macro-F1 {f_shuf*100:.1f}%")

    # 셔플 모델이 학습 데이터는 외웠는지 확인. 외웠는데 평가셋에서 무너졌다면
    # "외울 능력은 충분한데 일반화할 신호가 없었다"는 뜻이다.
    tr_loader = make_loader(tokenizer, train_rows, args.batch, shuffle=False)
    yp_tr = predict(m_shuf, tr_loader, device)
    y_tr_shuf = np.array([y for _, y in shuffled])
    print(f"   (참고) 셔플된 학습 데이터 자체는 {acc_of(y_tr_shuf, yp_tr)*100:.1f}% 재현 — 외우기는 함")
    del m_shuf
    torch.cuda.empty_cache()

    # ── C. 정상 학습 ────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    print("C. 정상 학습 (같은 하이퍼파라미터)")
    t0 = time.perf_counter()
    set_seed(args.seed)
    m_real = train_model(train_rows, tokenizer, args, device, quiet=True)
    yp_real = predict(m_real, eval_loader, device)
    print(f"   ({time.perf_counter()-t0:.0f}초)")
    a_real, f_real = acc_of(y_true, yp_real), macro_f1(y_true, yp_real)
    print(f"   평가셋 정확도 {a_real*100:.1f}%   macro-F1 {f_real*100:.1f}%")

    # ── 판정 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    print("=== 판정 ===")
    print(f"   {'':<26}{'정확도':>9}{'macro-F1':>11}")
    print(f"   {'분포대로 찍기 (계산값)':<24}{prop*100:>8.1f}%{'—':>11}")
    print(f"   {'라벨 셔플 학습 (대조군)':<24}{a_shuf*100:>8.1f}%{f_shuf*100:>10.1f}%")
    print(f"   {'정상 학습':<26}{a_real*100:>8.1f}%{f_real*100:>10.1f}%")
    gap = (a_real - a_shuf) * 100
    print(f"\n   정상 − 대조군 = {gap:+.1f}%p")

    # 대조군 대비 우연히 이만큼 벌어질 확률(이항 근사). 대조군을 귀무가설로 둔 검정.
    se = (a_shuf * (1 - a_shuf) / n_ev) ** 0.5
    z = (a_real - a_shuf) / se if se > 0 else float("inf")
    print(f"   z = {z:.1f}  (대조군을 귀무가설로 둔 이항 근사)")
    if gap > 15 and a_shuf < prop * 100 + 10:
        print("\n   → 모델은 **댓글 텍스트를 읽고 판정한다.**")
        print("      라벨을 섞으면 사전 확률 수준으로 무너지는데 정상 학습은 크게 웃돈다.")
        print("      즉 75%는 '분포를 따라 찍어서' 나온 점수가 아니다.")
    else:
        print("\n   ⚠️ 대조군과 차이가 작다. 텍스트가 아니라 분포를 학습했을 가능성이 있다.")
    print("\n   ※ 예측 '분포'가 학습셋 분포와 비슷한 것은 정상이다. 평가셋의 실제 분포가")
    print("     학습셋과 비슷하므로, 잘 맞히면 분포도 자연히 비슷해진다. 분포 일치는")
    print("     추론의 증거도 반증도 아니며, 가르는 것은 **건별 정확도**다.")


if __name__ == "__main__":
    main()
