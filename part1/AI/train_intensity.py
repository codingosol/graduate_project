"""댓글 '과격도(공격성 강도)' 분류기를 학습한다 — **강도 축** (성향 축 train.py와 독립).

무엇을 하는가:
  K-HATERS(`humane-lab/K-HATERS`, 네이버 뉴스 댓글 19만, EMNLP 2023)의 4단계 공격성 라벨로
  KcELECTRA를 파인튜닝한다. **손라벨 불필요** — 데이터셋에 이미 라벨이 있다(강도 축의 핵심 이점).

왜 별도 스크립트인가:
  성향 축(train.py)은 우리가 매긴 DB 라벨(좌/우/중립/불가)로 학습하지만, 강도 축은 외부
  데이터셋(K-HATERS)으로 학습한다. 라벨 체계·출처가 완전히 달라 파일을 분리했다.
  학습 루프(fp16 GradScaler·AdamW·OneCycleLR·class-weights)는 train.py와 같은 검증된 패턴이다.

서열(낮을수록 온건 → 높을수록 과격): normal < offensive < L1_hate(암시적) < L2_hate(명시적).
  추론(infer.py --axis intensity)에서 점수 = 기대 서열 / 3 → 0~1 과격도.
  ⚠️ 이 LABELS 순서가 infer.py의 _postprocess_intensity와 반드시 일치해야 한다(모델 출력 인덱스).

도메인 일치: K-HATERS도 네이버 뉴스 댓글이라 우리 유튜브 뉴스 댓글과 거의 같다(길이 median 42,
  우리 10~600 필터 안). KcELECTRA(네이버 댓글 사전학습)와도 궁합이 맞는다.

사용법:
  python AI/train_intensity.py --limit 2000 --epochs 1          # 스모크 테스트(빠름)
  python AI/train_intensity.py --epochs 2 --class-weights --save ../models/khaters_v1
"""
import argparse
import os
import random
import sys
import time
from collections import Counter

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

MODEL_NAME = "beomi/KcELECTRA-base-v2022"
LABELS = ("normal", "offensive", "L1_hate", "L2_hate")   # 서열 0<1<2<3
L2I = {l: i for i, l in enumerate(LABELS)}
KO = {"normal": "정상", "offensive": "공격", "L1_hate": "혐오L1", "L2_hate": "혐오L2"}
MAX_LEN = 128


def set_seed(seed):
    random.seed(seed); np.random.seed(seed)
    torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)


def load_khaters(limit=None):
    """HuggingFace에서 K-HATERS를 읽어 (train, val, test)를 (text, label_idx) 리스트로 반환."""
    from datasets import load_dataset
    ds = load_dataset("humane-lab/K-HATERS")

    def conv(split):
        return [(t, L2I[l]) for t, l in zip(ds[split]["text"], ds[split]["label"])
                if isinstance(t, str) and t.strip()]

    tr = conv("train")
    if limit:
        random.seed(42); random.shuffle(tr); tr = tr[:limit]
    return tr, conv("validation"), conv("test")


def encode(tokenizer, texts):
    enc = tokenizer(texts, truncation=True, max_length=MAX_LEN,
                    padding="max_length", return_tensors="pt")
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


def train_model(train_rows, tokenizer, args, device):
    from transformers import AutoModelForSequenceClassification
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, num_labels=len(LABELS)).to(device)

    loader = make_loader(tokenizer, train_rows, args.batch, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    steps = len(loader) * args.epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=steps, pct_start=0.1)
    scaler = torch.amp.GradScaler("cuda", enabled=device == "cuda")

    weight = None
    if args.class_weights:
        cnt = Counter(y for _, y in train_rows)
        w = [len(train_rows) / (len(LABELS) * max(cnt.get(i, 0), 1)) for i in range(len(LABELS))]
        weight = torch.tensor(w, dtype=torch.float, device=device)

    for ep in range(1, args.epochs + 1):
        model.train()
        total, t0 = 0.0, time.perf_counter()
        for step, (ids, mask, y) in enumerate(loader, 1):
            opt.zero_grad(set_to_none=True)
            with torch.autocast("cuda", dtype=torch.float16, enabled=device == "cuda"):
                logits = model(input_ids=ids.to(device), attention_mask=mask.to(device)).logits
                loss = F.cross_entropy(logits.float(), y.to(device), weight=weight)
            scaler.scale(loss).backward()
            scaler.step(opt); scaler.update(); sched.step()
            total += loss.item()
            if step % 500 == 0:
                el = time.perf_counter() - t0
                eta = el / step * (len(loader) - step)
                print(f"    epoch {ep} step {step}/{len(loader)}  loss {total/step:.4f}"
                      f"  경과 {el:.0f}s 남음 ~{eta:.0f}s", flush=True)
        print(f"    epoch {ep}/{args.epochs}  평균 loss {total/len(loader):.4f}"
              f"  ({time.perf_counter()-t0:.0f}s)", flush=True)
    return model


def report(y_true, y_pred):
    n = len(y_true)
    acc = (y_true == y_pred).mean()
    print(f"\n  전체 정확도  {acc*100:.1f}%  ({int((y_true==y_pred).sum())}/{n})")
    # 서열 오차(MAE): 과격도는 서열형이라 '한 칸 차이'와 '세 칸 차이'를 구분해 본다.
    mae = np.abs(y_true - y_pred).mean()
    print(f"  서열 MAE     {mae:.3f}  (0=완벽, 1=평균 한 단계 차이)")

    print(f"\n  {'라벨':<8}{'정밀도':>8}{'재현율':>8}{'F1':>8}{'실제':>8}{'예측':>8}")
    f1s = []
    for i, lab in enumerate(LABELS):
        tp = int(((y_pred == i) & (y_true == i)).sum())
        pp, ap = int((y_pred == i).sum()), int((y_true == i).sum())
        pr = tp / pp if pp else 0.0
        rc = tp / ap if ap else 0.0
        f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
        f1s.append(f1)
        print(f"  {KO[lab]:<8}{pr*100:>7.0f}%{rc*100:>7.0f}%{f1*100:>7.0f}%{ap:>8}{pp:>8}")
    print(f"  macro-F1 {np.mean(f1s)*100:.1f}%")

    print(f"\n  혼동행렬 (행=정답, 열=예측)")
    print(f"  {'':<8}" + "".join(f"{KO[l]:>7}" for l in LABELS))
    for i, lab in enumerate(LABELS):
        row = [int(((y_true == i) & (y_pred == j)).sum()) for j in range(len(LABELS))]
        print(f"  {KO[lab]:<8}" + "".join(f"{v:>7}" for v in row))
    return acc, float(np.mean(f1s))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="댓글 과격도(강도 축) 분류기 학습 — K-HATERS")
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--batch", type=int, default=32, help="VRAM 6GB 실측 기준값")
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--class-weights", action="store_true",
                   help="불균형(offensive 70k vs L1 20k) 보정")
    p.add_argument("--limit", type=int, help="학습셋 크기 제한(스모크 테스트용)")
    p.add_argument("--save", metavar="DIR", help="학습된 모델 저장 경로")
    args = p.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("⚠️ GPU 없음 — CPU로도 되지만 수십 배 느립니다.\n")
    set_seed(args.seed)

    print("K-HATERS 로드 중…", flush=True)
    train_rows, val_rows, test_rows = load_khaters(args.limit)
    tc = Counter(y for _, y in train_rows)
    print(f"학습 {len(train_rows):,} / val {len(val_rows):,} / test {len(test_rows):,}")
    print("  학습 분포:", "  ".join(f"{KO[l]} {tc.get(i,0):,}" for i, l in enumerate(LABELS)))
    print(f"  모델 {MODEL_NAME} / {device} / batch {args.batch} / epochs {args.epochs}"
          f" / class_weights {args.class_weights}", flush=True)

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    t0 = time.perf_counter()
    model = train_model(train_rows, tokenizer, args, device)
    print(f"\n학습 완료 ({time.perf_counter()-t0:.0f}s) — test로 채점", flush=True)

    test_loader = make_loader(tokenizer, test_rows, args.batch, shuffle=False)
    y_true = np.array([r[1] for r in test_rows])
    report(y_true, predict(model, test_loader, device))

    if args.save:
        os.makedirs(args.save, exist_ok=True)
        model.save_pretrained(args.save)
        tokenizer.save_pretrained(args.save)
        print(f"\n모델 저장: {args.save}")


if __name__ == "__main__":
    main()
