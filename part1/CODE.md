# Part 1 코드 구조 안내

Part 1(뉴스 채널 정치성향 분석)의 `part1/` 디렉토리에 있는 각 파일의 역할 정리.
설계 배경과 진행 상황은 [Part1.md](Part1.md), 의사결정 히스토리는 `../DECISIONS.md` 참고.

## 디렉토리 배치

```
part1/
├── migrate.py, DB_migrations/, requirements.txt   ← 공용 (스키마·의존성)
├── Data/   collect · backfill · prune · reclassify · keywords(.py/.json)   ← 수집·ETL
└── AI/     train · sanity_check · freeze_sample · sample_trainset · ingest_trainset · blind_check · infer   ← 학습·라벨링·추론
```

- **실행은 `part1/`에서** `python Data/collect.py`, `python AI/train.py` 형태.
- **크로스 import**: 같은 디렉토리끼리(예: `backfill`→`collect`, `sanity_check`·`infer`→`train`)는
  파이썬이 실행 스크립트 디렉토리를 `sys.path`에 자동으로 넣어주므로 그대로 import된다.
  (예전엔 공용 `db.py`를 쓰려고 part1 루트를 `sys.path`에 넣는 부트스트랩이 있었으나, 운영 DB 단일화로 `db.py`가 사라져 제거했다.)

## 전체 흐름

```
YouTube Data API                    Neon PostgreSQL
      │                                    ▲
      │  channels → playlistItems          │
      │  → videos(카테고리) → 필터         │
      │  → commentThreads                  │
      ▼                                    │
  Data/collect.py  ──── 적재 ──────────────┘
      │                        (운영 DB `newstance`에 적재 — DATABASE_URL이 직접 가리킴)
      ├── Data/keywords.py / keywords.json  (2차 필터: 정치 키워드 판별)
      └── migrate.py / DB_migrations/       (스키마 관리, 공용)
```

## 파일별 역할

### `collect.py` — 메인 수집 스크립트
매일 GitHub Actions(`.github/workflows/collect.yml`)가 실행하는 핵심 스크립트.

- **`CHANNELS`**: outlet(언론사) → 채널 식별자 매핑. **실행으로 검증된 값만** 들어있음(핸들 추정은 자주 틀림). 현재 10개 outlet = 10개 채널 1:1.
- **수집 흐름**: `channels.list` → `playlistItems.list`(최근 24시간) → `videos.list`(categoryId) → 필터 → `commentThreads.list`(order=relevance, 1페이지 최대 100개)
- **`apply_filters` / `is_political_title`**: 1차(categoryId≠25 제외) + 2차(정치 키워드) 필터
- **병렬 처리 2단계**: 1단계 채널별 메타데이터/영상목록 동시 조회(`CHANNEL_WORKERS=5`) → 2단계 전체 영상을 한 리스트로 모아 댓글 수집 동시 처리(`VIDEO_WORKERS=10`). 중첩 스레드풀로 스레드가 곱연산으로 불어나는 걸 피하려고 이 구조로 나눔
- 댓글은 `execute_values`로 **영상당 1회 일괄 삽입**(건당 INSERT 대비 DB 왕복 대폭 절감)
- `commentsDisabled`(HTTP 403)는 정상 케이스로 스킵하고 `videos.comments_disabled` 플래그 기록

**적재 대상**: `DATABASE_URL`(운영 DB `newstance`)에 직접 적재한다. 1·2차 필터 검증을 마친 뒤, 그동안 쓰던 테스트 DB `collect_test`를 `newstance`로 rename해 운영으로 승격했다(2026-08-03). 예전의 test/운영 분리(`ensure_test_database`)와 공용 `db.py`는 제거됐다.

### `keywords.json` — 정치 키워드 데이터
2차 필터가 쓰는 키워드를 코드와 분리해 데이터로 관리(정치인·정당명은 시간이 지나면 낡아지므로 갱신이 잦음).

- 카테고리: `parties`(정당명) / `institutions_terms`(기구·용어) / `hanja_abbreviations`(한자 축약 與野檢李尹) / `nicknames`(별칭 잼통·잼프) / `politician_names`(정치인) + `exclude_foreign_leaders`(해외 정상 제외 목록)
- **판정에서 카테고리가 두 갈래로 쓰인다** — `parties`·`hanja_abbreviations`·`nicknames`·`politician_names`(143개)는 **국내 고유**, `institutions_terms`(79개)는 **일반 용어**. 아래 `keywords.py` 참고
- 현재 포함 222개 / 제외 10개
- `_comment` 필드에 **오탐으로 확인돼 의도적으로 뺀 키워드**를 기록해둠(검찰·제헌절·정당·당원·군수·시의원·정부·민심·여야) — 나중에 "이거 왜 없지?" 하고 다시 넣는 실수를 막기 위함

### `keywords.py` — 키워드 로더
- `load_keyword_groups()` → `(국내 고유, 일반 용어, 제외)` **세 갈래**. 판정 로직이 쓰는 것.
- `load_political_keywords()` → `(포함, 제외)` 2-튜플. 기존 호출부 호환용.
- `normalize()` → **NFC 정규화**. 제목과 키워드 **양쪽에** 적용해야 한다(한쪽만 하면 여전히 안 맞음).

> **왜 세 갈래인가** (2026-07-24). 이전에는 카테고리 구분 없이 "하나라도 포함되면 정치"로 합쳐 쓰고, 해외 정상 이름이 있으면 **무조건** 제외했다. 그래서 `트럼프 대통령에 국방비 먼저 꺼낸 이재명` 같은 정상외교·국내 시사물이 통째로 탈락했다(실측 57건 중 23건이 국내 정치). 국내 고유 키워드(정당·별칭·한자 축약·인물, 143개)가 걸리면 제외를 무시하고, 일반 용어(기구·용어, 79개)만 걸렸을 때만 제외한다.
>
> **왜 정규화가 필요한가.** 헤드라인의 `李`가 표준 한자(U+674E)가 아니라 **CJK 호환용 한자(U+F9E1)**로 들어오는 경우가 있다. 눈에는 같지만 `in`이 False를 낸다. 실측 413개 제목에 호환용 한자가 있었고 7건이 이 때문에 누락돼 있었다.

### `migrate.py` + `DB_migrations/` — 스키마 마이그레이션
- `DB_migrations/0001_init.sql`: outlets / channels / videos / comments 테이블 + 인덱스
- `DB_migrations/0002_slim_raw.sql`: `raw JSONB` 제거 + 필요 필드(`author_channel_id`, `total_reply_count`) 추출, `text`를 textOriginal로 교체. **UPDATE 대신 새 테이블로 교체하는 방식** — 제자리 UPDATE는 22만 행 재작성에 용량이 2배 필요해 Neon 512MB 한도에 걸렸었음
- `DB_migrations/0003_channel_type.sql`: `channels.channel_type`(news/opinion) 추가 — 스트레이트 뉴스와 시사/논평 채널을 구분해 장르 효과를 분리하기 위함
- `DB_migrations/0004_comment_labels.sql`: 성향 라벨을 `comments` 컬럼에서 떼어내 `label_runs` + `comment_labels`로 분리. UPDATE로 인한 MVCC 용량 폭증 회피가 주목적이고, `run_id`로 라벨링 세대를 나란히 보관해 손라벨 정답셋이 덮이지 않게 함
- `DB_migrations/0005_drop_label_comment_index.sql`: 0004에서 넣은 `idx_comment_labels_comment` 제거 — 79.9만 행 실측에서 플래너가 한 번도 쓰지 않으면서 33MB만 차지했음
- `DB_migrations/0006_label_unusable.sql`: 라벨 값에 `unusable` 추가 — `neutral`(정치적이나 편향 없음)과 판단 대상이 아닌 잡음("ㅋㅋㅋ", "1등", 광고)을 구분. 뭉치면 모델이 "노이즈=중립"을 학습해 채널 점수가 0쪽으로 끌린다
- `DB_migrations/0007_label_sample.sql`: 평가셋 표본을 고정하는 `label_sample` 테이블. `comments`를 **`ON DELETE RESTRICT`로** 참조한다(`comment_labels`의 CASCADE와 반대) — 표본이 삭제되면 실험이 무효가 되므로 전파시키지 않고 아예 막는다
- `migrate.py`: 아직 적용 안 된 `.sql` 파일만 순서대로 실행하고 `schema_migrations` 테이블에 이력 기록
- **스키마를 바꿀 땐 기존 파일을 수정하지 말고 `0002_xxx.sql` 같은 새 파일을 추가할 것** (기존 파일 수정 시 이미 적용된 DB와 어긋남)
- 실행: `python migrate.py` — `DATABASE_URL`(운영 DB `newstance`) 대상. 각 수집·학습 스크립트도 실행 시 같은 DB에 직접 연결한다.

### `backfill.py` — 과거 영상 소급 수집
매일 24시간치만 모으는 `collect.py`와 달리 **더 과거로** 소급 수집하는 도구. 상시 실행하지 않고, 하루 남은 쿼터에 맞춰 수동으로 돌린다.

- **채널별 독립 quota + `MAX_PAGES_PER_CHANNEL`(채널당 페이지 상한)** 이중 안전장치 — 전체 공용 예산 하나만 뒀다가 한 채널(오마이TV)이 예산을 독점해버린 사고가 있어서 이렇게 바꿈
- **예산은 `resolve_targets()`가 실행할 때마다 자동 배분한다**(2026-07-24). `channels.backfill_cursor`로 채널별 남은 일수를 읽어 ①하한 도달 채널은 제외 ②`MIN_CHANNEL_BUDGET`을 깔고 ③남은 예산을 **필요량이 적은 채널부터** 채운다. 손으로 적던 `CHANNEL_BUDGETS` 상수는 완주 채널의 몫이 놀아서(실측 1,758 unit) 폐기했다. '가까운 채널부터'인 이유는 `playlistItems`가 날짜 점프를 못 해 **매 실행마다 커서까지 다시 페이지를 넘겨야** 하고, 채널을 빨리 끝낼수록 그 반복 비용이 사라지기 때문 (시뮬레이션: 완주까지 2일 vs 균등 배분 3일)
- **필요량(`need`)은 통과 비용 + 소급 비용**이다(2026-07-25). 커서까지 다시 넘기는 `days_since_cursor × PASS_UNITS_PER_DAY`와 커서 아래로 내려가는 `days_left × UNITS_PER_DAY`의 합. 통과 비용을 빼먹으면 커서 깊은 채널(YTN, 커서 92일 전)이 남은 일수가 적다는 이유로 최소 예산만 받아 **커서에 닿지도 못하고 끝난다**(실측: 300 unit으로 훑음 0건).
- `TOTAL_BUDGET`(7,920)만 조정하면 되고, `EXCLUDED_OUTLETS`는 이미 충분히 모인 채널을 제외
- 재개 지점은 `channels.backfill_cursor`(실제로 훑은 가장 오래된 지점). `MIN(published_at)`을 쓰면 정치 영상이 없는 구간을 훑고도 커서가 안 움직여 매일 같은 구간을 다시 훑게 됨
- 하한선은 `prune.RETENTION_FLOOR`를 **import해서 공유**한다. 두 곳에 따로 적으면 backfill이 긁어온 구간을 prune이 곧바로 지우는 일이 생김
- 채널 조회 시 `outlet_name`이 아니라 **`CHANNELS`에서 얻은 `channel_id`로 직접 조회** — outlet_name이 DB에서 유일하지 않아 엉뚱한 채널을 긁은 사고가 있었음
- 다시 쓸 땐 `TOTAL_BUDGET`을 그날 남은 실제 쿼터에 맞춰 조정할 것 (채널별 배분은 자동)

### `prune.py` — 하한선보다 오래된 데이터 삭제
`RETENTION_FLOOR`(고정 날짜)를 정의하는 **유일한 곳**이고 `backfill.py`가 이 값을 가져다 쓴다.

- 롤링 윈도우가 아니라 **고정 날짜**인 이유: 롤링이면 시간이 지날수록 과거 댓글이 계속 삭제되는데, 고정 평가셋은 바뀌면 안 되므로 손으로 매긴 정답셋을 갉아먹는다. 고정 날짜면 창이 앞으로만 자란다
- 시작 전에 삭제 범위 안의 손라벨/평가셋을 세어 보고, 평가셋이 걸리면 **옵션 없이 중단**한다(중간에 FK 위반으로 터지면 일부만 지워진 상태가 됨)
- `VACUUM FULL`은 쓰지 않는다 — 일반 VACUUM으로 회수한 공간을 직후 backfill의 INSERT가 그대로 재사용하고, FULL은 ACCESS EXCLUSIVE 락과 순간 용량 급증을 부른다

### `reclassify.py` — 기존 영상의 정치 여부 재판정
`keywords.json`을 고친 뒤 이미 저장된 영상의 `is_political`을 다시 계산한다. 키워드는 정치인·정당명이 섞여 있어 시간이 지나면 낡으므로 주기적으로 갱신 → 재판정이 필요하다.

## AI/ — 학습·라벨링

### `train.py` — 성향 분류기 학습
`beomi/KcELECTRA-base-v2022` 파인튜닝. 학습셋은 `--train-run`으로 고른다(기본 `llm_train_v1`) / 평가 300건(`manual_gold_v1`). 최종 배포 모델은 v2 라벨 1000건(`llm_train_v2_big`)으로 학습해 `models/kcelectra_v2`에 저장했다(근거: `../DECISIONS.md` 08-03).

- **평가셋 오염 차단이 SQL에 박혀 있다** — `run_id='manual_gold_v1'`인 라벨 전부(잉여분 포함)를 학습에서 제외한다.
- `--train-run RUN`(학습셋 run_id 지정), `--curve`(학습곡선, **스텝 수 고정**), `--tune`(dev로 하이퍼파라미터 선택 후 평가셋 1회 측정), `--labels human-first`(맹검 100건을 사람 라벨로 교체), `--class-weights`(드문 클래스 보정), `--save DIR`(모델 저장).
- **epoch을 너무 작게 두면 학습이 안 된다** — 700÷32=22스텝/epoch이라 4 epoch(88스텝)은 부족해 `좌`·`불가`를 아예 예측 못 한다. 기본 15 epoch(330스텝).

### `sanity_check.py` — 순열 검정
"예측 분포가 학습셋 분포를 따라 찍는 것 아닌가"를 가른다. **라벨을 무작위로 섞어** 텍스트-정답 관계를 끊은 대조군과 비교(정상 74.7% vs 셔플 38.7%, z=9.1). 라벨·입력을 바꿀 때마다 재실행해 "진짜 학습인지"를 확인한다. `train.py`의 함수를 import한다.

### 라벨링 파이프라인

| 파일 | 역할 |
|---|---|
| `freeze_sample.py` | 평가셋 표본을 `label_sample`에 **못박는다**(`ord` 순서까지). 라벨링 툴은 읽기만 한다 — 매번 새로 뽑던 방식은 수집이 진행될 때마다 표본이 바뀌어 라벨이 특정 채널에 쏠리는 사고를 냈다 |
| `sample_trainset.py` | 학습셋 표본을 뽑아 청크 JSON으로 내보낸다(`--out`). 평가셋·기존 라벨과 겹치지 않게 제외하고, **영상 제목은 내보내지 않는다**(라벨을 매기는 쪽이 모델보다 많은 정보를 보면 안 되므로). 채널이 청크마다 고르게 섞이도록 라운드로빈 배치하고, 내보내기 전에 편차를 검사해 어긋나면 중단 |
| `ingest_trainset.py` | 채워진 라벨 JSON(`--dir`)을 `comment_labels`에 UPSERT. 평가셋과 겹침 0건을 확인 |
| `blind_check.py` | 학습셋 라벨의 **맹검 일치율** 측정. `prepare`로 새 `run_id` 표본을 만들면 기존 라벨이 조인되지 않아 툴 코드를 고치지 않고 맹검이 성립한다. `compare`로 일치율·Cohen's kappa·혼동행렬 출력 |

> `sample_trainset`·`ingest_trainset`이 쓰는 청크 파일은 저장소 루트의 `labeling_tool/trainset/`에
> 있다(gitignore). AI/에서 두 단계 위(`../../`)라, 기본 경로가 그렇게 잡혀 있고 `--out`/`--dir`로 바꿀 수 있다.

### `infer.py` — 전체 댓글 추론·적재 (Part 1 결론 단계)
학습된 모델을 전체 댓글에 적용해 채널별 성향 점수를 낸다. 개별 정확도가 아니라 **채널 순위가 통념과 맞는가**가 목적이다(수만 건 평균이라 개별 오차는 상쇄된다).

- **Neon 접근은 앞뒤 한 번씩만** — 시작에 댓글을 fetch → GPU로 전부 추론 → 완료 후 `comment_labels`에 청크 배치 UPSERT(추론 중에는 DB 미접근). label=argmax, score=P(우)−P(좌), confidence=max prob.
- **2축 확장 대비** — 축별 후처리를 `AXIS_CONFIG`로 분리했다. `leaning`(기본)만 구현돼 있고, 강도 축(K-HATERS)은 후처리 함수 하나만 추가하면 `--axis intensity`로 붙는다.
- 기본 모델 `models/kcelectra_v2`, run_id `model_kcelectra_v2`. `results/<run_id>.txt`(채널 순위·분포)와 `.progress`(진행률)를 로컬에도 남긴다(`results/`는 gitignore).

### `requirements.txt` (수집용) / `AI/requirements.txt` (학습용)
- **`requirements.txt`** — 수집·ETL과 CI가 쓴다. `google-api-python-client` / `psycopg2-binary` / `python-dotenv`. 병렬 처리는 표준 라이브러리(`concurrent.futures`)라 별도 의존성 없음.
- **`AI/requirements.txt`** — 학습·추론 전용(로컬 GPU). `-r ../requirements.txt`로 수집 의존성을 상속하고 `torch`(cu128 전용 인덱스)·`transformers`·`numpy`를 더한다. **GitHub Actions는 이 파일을 설치하지 않는다** — 러너엔 GPU가 없고 torch 2.8GB를 매 실행 받을 이유가 없다.

### `.env` (gitignore됨)
로컬 실행용 `YOUTUBE_API_KEY`, `DATABASE_URL`. GitHub Actions에서는 같은 이름의 Secrets가 환경변수로 주입되므로 **코드는 양쪽에서 동일하게 `os.environ`으로 읽음**.

## 실행 방법

```bash
cd part1
pip install -r requirements.txt

python migrate.py     # 스키마 생성/갱신 (운영 DB newstance 대상)
python Data/collect.py     # 24시간치 수집 (운영 DB newstance에 적재)
python Data/backfill.py     # 과거 소급 수집 (필요할 때만, TOTAL_BUDGET 확인 후)
python Data/prune.py --dry-run    # 하한선보다 오래된 데이터 확인 (--yes로 실제 삭제)
```

## 삭제된 파일

- `db.py` — test/운영 DB 분리(`ensure_test_database`)를 담당했으나, 필터 검증 후 `collect_test`를 운영 DB `newstance`로 rename·승격하면서 분리가 불필요해져 삭제(2026-08-03). 각 스크립트는 이제 `DATABASE_URL`에 직접 연결하며, 이때 각 파일의 `sys.path` 부트스트랩도 함께 제거했다.
- `quick_test.py` — API 연결 확인용 임시 스크립트였고, 기능(채널 조회 → 영상 목록 → 댓글 수집 + `commentsDisabled` 예외처리)이 전부 `collect.py`에 흡수돼서 삭제함
- 일회성 진단 스크립트 다수(드리프트 검정, 용량 측정, 채널 검증, 라벨 검토 등) — 결론만 `../DECISIONS.md`에 남기고 스크립트는 삭제. 전부 특정 시점의 DB 상태를 전제로 한 것이라 나중에 다시 돌려도 같은 답이 안 나온다
- `cap_comments.py` — 영상당 댓글 20건 상한을 과거 데이터에 소급 적용한 도구. 1회 실행 후 삭제했다. `comments`에 INSERT하는 경로가 `collect.py`·`backfill.py` 둘뿐이고 둘 다 `select_comments()`를 지나므로 상한이 깨질 수 있는 경로가 없다. 무엇보다 **같은 선별 규칙을 Python과 SQL 두 벌로 들고 있는 상태**라서, 남겨두면 한쪽만 고쳐 어긋나는 사고를 부른다. 상한 값을 바꿔 소급 재적용할 일이 생기면 그때 다시 쓴다
