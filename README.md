# 뉴스 채널 댓글로 보는 유튜브 정치 성향 진단

성균관대학교 소프트웨어학과 졸업작품 (2026)

국내 방송 언론사 10곳의 유튜브 채널 13개에서 정치 영상의 댓글을 매일 수집하고, 댓글마다 정치 성향(진보/보수)과 과격도를 모델로 매긴 뒤 채널 단위로 집계했다. 집계한 채널 성향은 크롬 익스텐션에 넣어서, 내 유튜브 추천 피드나 시청기록에 나오는 정치 영상이 어느 쪽 채널에 몰려 있는지 확인할 수 있게 했다.

- Part 1 (`part1/`): 수집, 라벨링, 학습, 추론, 집계
- Part 2 (`part2/`): 크롬 익스텐션. 서버 없이 브라우저 안에서만 동작한다.


## 폴더 구성

```
part1/
  Data/            수집과 동기화 (collect, backfill, sync, reclassify, 정치 키워드)
  AI/              라벨링 보조, 학습, 추론 (train, train_intensity, infer 등)
  DB_migrations/   스키마 (번호 순서대로 적용)
  migrate.py
  CODE.md          파일별 역할과 설계 이유
part2/
  manifest.json, popup.*, analysis.*, content.js
  lib/             정치 판별, 성향 조회, 차트(viz.js)
  data/            Part 1에서 뽑은 채널 성향 데이터
  tools/           데이터 추출, 키워드 변환 스크립트
models/            학습된 모델 (저장소에는 없고 제출본에만 있음)
.github/workflows/collect.yml   매일 수집 (GitHub Actions)
```


## 익스텐션 설치

1. 크롬 주소창에 `chrome://extensions` 입력
2. 오른쪽 위 "개발자 모드" 켜기
3. "압축해제된 확장 프로그램을 로드합니다"를 눌러 `part2` 폴더 선택

유튜브 홈 화면에서 익스텐션 아이콘을 누르면 지금 피드에 보이는 영상 중 정치 영상의 비율과 진보/중도/보수 분포가 나온다.

시청기록은 [Google Takeout](https://takeout.google.com)에서 YouTube의 "기록"만 골라 내려받은 뒤, 팝업의 "시청기록 상세 분석"에서 시청 기록 파일(JSON 또는 HTML)을 올리면 된다. 월별 추세와 많이 본 채널, 채널별 성향/과격도 대시보드도 같은 페이지에 있다.

채널 성향 데이터는 `part2/data/`에 들어 있어서 실행 중에 외부로 요청을 보내지 않는다. 올린 시청기록도 브라우저 밖으로 나가지 않는다. 성향 표에 없는 채널의 영상은 "미상"으로 따로 센다.


## Part 1 실행

필요한 것

- Python 3.12 (수집 스크립트만 쓸 거면 3.11도 된다. GitHub Actions는 3.11)
- Docker (로컬 PostgreSQL 18)
- YouTube Data API 키
- GitHub Actions에서 접근할 수 있는 PostgreSQL 하나 (수집분을 잠깐 받아 두는 스테이징 DB. 지금은 Neon을 쓴다)
- 학습/추론은 NVIDIA GPU 기준이다. GPU가 없으면 CPU로 돌아가긴 하지만 많이 느리다.

### 1. 패키지

```bash
cd part1
pip install -r requirements.txt       # 수집, 동기화
pip install -r AI/requirements.txt    # 학습, 추론
```

`AI/requirements.txt`의 torch는 CUDA 12.8 빌드로 고정되어 있다. CUDA 버전이 다르거나 GPU가 없으면 https://pytorch.org 에서 맞는 설치 명령을 찾아 그 줄만 바꾸면 된다.

### 2. 로컬 DB

```bash
docker run -d --name newstance-pg --restart unless-stopped \
  -e POSTGRES_USER=newstance -e POSTGRES_DB=newstance -e POSTGRES_PASSWORD=<비밀번호> \
  -p 127.0.0.1:5432:5432 -v newstance-pgdata:/var/lib/postgresql \
  postgres:18
```

### 3. 환경 변수

`part1/.env`에 넣는다. GitHub Actions에는 `YOUTUBE_API_KEY`, `STAGING_DATABASE_URL` 두 개만 Secrets로 등록하면 된다.

```
YOUTUBE_API_KEY=...
DATABASE_URL=postgresql://newstance:<비밀번호>@127.0.0.1:5432/newstance
STAGING_DATABASE_URL=postgresql://...
```

스키마는 두 DB에 모두 만든다.

```bash
python migrate.py             # 로컬 DB
python migrate.py --staging   # 스테이징 DB
```

### 4. 수집부터 익스텐션 데이터까지

```bash
python Data/collect.py                 # 새 영상과 댓글 → 스테이징 DB (평소엔 Actions가 매일 03시에 실행)
python Data/backfill.py --budget 3000  # 남은 quota로 과거 영상 소급
python Data/sync.py                    # 스테이징 → 로컬 DB로 옮기고 스테이징에서는 지움

python AI/infer.py                     # 성향 추론 (아직 점수가 없는 댓글만)
python AI/infer.py --axis intensity    # 과격도 추론

python ../part2/tools/export_data.py        # 익스텐션용 JSON 다시 만들기
python ../part2/tools/build_keywords_js.py  # Data/keywords.json을 고쳤을 때만
```

`export_data.py`를 다시 돌렸으면 `chrome://extensions`에서 익스텐션을 새로고침해야 반영된다.

### 5. 모델 다시 학습하기

```bash
python AI/train.py --train-run llm_train_v2_big --epochs 15 --class-weights --save ../models/kcelectra_v2
python AI/train_intensity.py --epochs 2 --class-weights --save ../models/khaters_v1
```

성향 모델은 DB에 있는 라벨(직접 라벨링한 평가셋 300건, LLM 라벨 학습셋 1,000건)이 있어야 학습할 수 있다. 과격도 모델은 공개 데이터셋 [K-HATERS](https://huggingface.co/datasets/humane-lab/K-HATERS)를 받아서 학습하므로 DB 없이도 다시 만들 수 있다 (GPU 기준 2시간 정도).


## 모델

| 경로 | 용도 | 학습 데이터 |
|---|---|---|
| `models/kcelectra_v2` | 성향 (진보/보수/중립/사용불가) | LLM 라벨 1,000건 |
| `models/khaters_v1` | 과격도 (정상/공격/암시적 혐오/명시적 혐오) | K-HATERS 172,158건 |

둘 다 [beomi/KcELECTRA-base-v2022](https://huggingface.co/beomi/KcELECTRA-base-v2022)를 파인튜닝했다. 제출 용량 때문에 제출본에는 가중치를 fp16으로 저장한 사본을 넣었다 (각 약 257MB). 불러오면 fp32로 올라가기 때문에 코드는 그대로 쓰면 된다.

원본과 fp16 사본을 같은 평가 데이터로 비교했을 때 과격도 결과는 완전히 같았고, 성향은 정확도와 macro-F1이 같았다. 평가셋 300건 중 판정 경계에 걸린 1건만 달라질 수 있어서 좌/우 방향 정확도가 93.7%가 아니라 93.0%로 나올 수 있다. 실제 댓글 2만 건으로 비교했을 때 채널 점수 차이는 최대 0.0002였다.


## 결과

2026년 4월 21일부터 10월 25일까지 정치 댓글 752,835건을 분석했다 (8월 3일부터 9월 18일까지는 수집 공백).

- 성향 모델의 좌/우 방향 정확도 93.7% (직접 라벨링한 평가셋 기준)
- 진보 성향으로 알려진 3개 채널(오마이TV, MBC, JTBC)이 모두 음수로 1, 2, 3위, 가장 보수 쪽은 TV조선
- 학습에 쓰지 않은 9월과 10월 데이터만으로 매긴 채널 순위도 4월부터 8월까지의 순위와 거의 같았다 (순위 상관 0.87)
- 과격도는 성향과 따로 움직였다. 가장 진보 쪽인 오마이TV가 가장 온건했고, MBC와 JTBC가 가장 과격했다.

점수는 전체적으로 보수 쪽(+)으로 치우쳐 있어서 중도 채널도 +0.24 정도가 나온다. 그래서 0을 기준으로 읽지 말고 채널끼리의 순서로 읽어야 한다. 익스텐션도 점수 부호 대신 순위로 확인한 세 그룹(진보 3, 중도 4, 보수 3)을 쓴다.


## 저장소에 없는 것

- 수집한 댓글과 라벨: 로컬 DB에만 있다. 댓글 작성자 ID가 들어 있어서 공개하지 않았다. 채널 단위 집계는 `part2/data/`에 있다.
- 라벨링 도구와 판정 기준 문서: 실존 인물의 진영 분류표가 들어 있어서 뺐다.
- `.env`, 개인 시청기록.
