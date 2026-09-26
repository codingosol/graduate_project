# Part 1 코드 구조 안내

Part 1(뉴스 채널 정치성향 분석)의 `part1/` 디렉토리에 있는 각 파일의 역할 정리.
설계 배경과 진행 상황은 [Part1.md](Part1.md), 의사결정 히스토리는 `../DECISIONS.md` 참고.

## 디렉토리 배치

```
part1/
├── migrate.py, DB_migrations/, requirements.txt   ← 공용 (스키마·의존성)
├── Data/   collect · backfill · sync · reclassify · keywords(.py/.json)   ← 수집·ETL
└── AI/     train · train_intensity · sanity_check · freeze_sample · sample_trainset · ingest_trainset · blind_check · infer   ← 학습·라벨링·추론
```

- **실행은 `part1/`에서** `python Data/collect.py`, `python AI/train.py` 형태.
- **크로스 import**: 같은 디렉토리끼리(예: `backfill`→`collect`, `sanity_check`·`infer`→`train`)는
  파이썬이 실행 스크립트 디렉토리를 `sys.path`에 자동으로 넣어주므로 그대로 import된다.
  (예전엔 공용 `db.py`를 쓰려고 part1 루트를 `sys.path`에 넣는 부트스트랩이 있었으나, 운영 DB 단일화로 `db.py`가 사라져 제거했다.)

## 전체 흐름

```
YouTube Data API
      │  channels → playlistItems → videos(카테고리) → 필터 → commentThreads
      ▼
  Data/collect.py · backfill.py  (GitHub Actions, 매일)
      │ 적재
      ▼
  스테이징 DB — Neon 무료 0.5GB (STAGING_DATABASE_URL)   ← 수집분 임시 보관 + 수집 상태(채널 커서·watermark)만
      │  python Data/sync.py  (로컬에서 실행: 복사 → 옮긴 댓글은 스테이징에서 삭제)
      ▼
  로컬 분석 DB — Docker PostgreSQL 18 (DATABASE_URL)       ← 전체 데이터·라벨의 정본
      └── AI/(학습·라벨링·추론) · reclassify · part2/tools/export_data.py(→ 크롬 익스텐션 데이터)

  공용: Data/keywords.py / keywords.json (2차 필터), migrate.py / DB_migrations/ (두 DB 같은 스키마)
```

무거운 작업(추론 적재·집계·대시보드)은 전부 로컬 DB에서만 한다. 스테이징은 매일 수천 행이 들어왔다
나가는 버퍼라 무료 한도 안에 머문다(경위: `../DECISIONS.md` 2026-09-26).

## 파일별 역할

### `collect.py` — 메인 수집 스크립트
매일 GitHub Actions(`.github/workflows/collect.yml`)가 실행하는 핵심 스크립트.

- **`CHANNELS`**: outlet(언론사) → 채널 식별자 매핑. **실행으로 검증된 값만** 들어있음(핸들 추정은 자주 틀림). 현재 10개 outlet = 10개 채널 1:1.
- **수집 흐름**: `channels.list` → `playlistItems.list`(최근 24시간) → `videos.list`(categoryId) → 필터 → `commentThreads.list`(order=relevance, 1페이지 최대 100개)
- **`apply_filters` / `is_political_title`**: 1차(categoryId≠25 제외) + 2차(정치 키워드) 필터
- **병렬 처리 2단계**: 1단계 채널별 메타데이터/영상목록 동시 조회(`CHANNEL_WORKERS=5`) → 2단계 전체 영상을 한 리스트로 모아 댓글 수집 동시 처리(`VIDEO_WORKERS=10`). 중첩 스레드풀로 스레드가 곱연산으로 불어나는 걸 피하려고 이 구조로 나눔
- 댓글은 `execute_values`로 **영상당 1회 일괄 삽입**(건당 INSERT 대비 DB 왕복 대폭 절감)
- `commentsDisabled`(HTTP 403)는 정상 케이스로 스킵하고 `videos.comments_disabled` 플래그 기록

**적재 대상**: `STAGING_DATABASE_URL`이 가리키는 **스테이징 DB(Neon)**에만 적재한다(`backfill.py`도 동일). 로컬 분석 DB로는 `sync.py`가 옮긴다. 재개 지점(채널별 최신 영상 `published_at`)도 스테이징에서 읽으므로, `sync.py`는 채널별 최신 영상을 스테이징에 남긴다. (이력: Neon `newstance` → 2026-08-05 CockroachDB → 2026-09-04 체험판 만료로 차단 → 09-26 현재 구조. [Part1.md](Part1.md) §3)

### `keywords.json` — 정치 키워드 데이터
2차 필터가 쓰는 키워드를 코드와 분리해 데이터로 관리(정치인·정당명은 시간이 지나면 낡아지므로 갱신이 잦음).

- 카테고리: `parties`(정당명) / `institutions_terms`(기구·용어) / `hanja_abbreviations`(한자 축약 與野檢李尹) / `nicknames`(별칭 잼통·잼프) / `politician_names`(정치인) + `exclude_foreign_leaders`(해외 정상 제외 목록)
- **판정에서 카테고리가 두 갈래로 쓰인다** — `parties`·`hanja_abbreviations`·`nicknames`·`politician_names`(143개)는 **국내 고유**, `institutions_terms`(79개)는 **일반 용어**. 아래 `keywords.py` 참고
- 현재 포함 241개 / 제외 12개 (2026-08-07 감사로 최근 정치인 19명 추가 — 경위 `_comment`·`../DECISIONS.md` 08-07)
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
- `DB_migrations/0008_backfill_exhausted.sql`: `channels.backfill_exhausted` 추가 — `playlistItems`가 최근 ~2만 편까지만 접근 가능해 하한(04-21)에 못 닿는 고업로드 채널(YTN·연합뉴스TV)을 완주로 인정해 `resolve_targets`에서 제외하기 위함
- `DB_migrations/0009_channel_thumbnail.sql`: `channels.thumbnail_url` 추가 — 시각화 대시보드의 채널 카드 로고용. `channels.list`(snippet.thumbnails)로 13개 채널 1회 수집
- `migrate.py`: 아직 적용 안 된 `.sql` 파일만 순서대로 실행하고 `schema_migrations` 테이블에 이력 기록
- **스키마를 바꿀 땐 기존 파일을 수정하지 말고 `0002_xxx.sql` 같은 새 파일을 추가할 것** (기존 파일 수정 시 이미 적용된 DB와 어긋남)
- 실행: `python migrate.py` — `DATABASE_URL`(로컬 분석 DB) 대상. `python migrate.py --staging` — `STAGING_DATABASE_URL`(Neon) 대상. **새 마이그레이션은 두 DB 모두에 적용할 것** — 스키마가 다르면 `sync.py`가 컬럼 불일치로 멈춘다.

### `backfill.py` — 과거 영상 소급 수집
매일 24시간치만 모으는 `collect.py`와 달리 **더 과거로** 소급 수집하는 도구. 상시 실행하지 않고, 하루 남은 쿼터에 맞춰 수동으로 돌린다.

- **채널별 독립 quota + `MAX_PAGES_PER_CHANNEL`(채널당 페이지 상한)** 이중 안전장치 — 전체 공용 예산 하나만 뒀다가 한 채널(오마이TV)이 예산을 독점해버린 사고가 있어서 이렇게 바꿈
- **예산은 `resolve_targets()`가 실행할 때마다 자동 배분한다**(2026-07-24, 배분식 08-07 개정). `channels.backfill_cursor`로 채널별 남은 일수를 읽어 ①하한 도달·exhausted 채널 제외 ②need(=통과비용+소급비용) 적은 순으로 **완주 임박 채널에 예산을 몰아준다**. '가까운 채널부터'인 이유는 `playlistItems`가 날짜 점프를 못 해 **매 실행마다 커서까지 다시 페이지를 넘겨야**(통과 비용) 하고, 채널을 빨리 끝낼수록 그 반복 비용이 사라지기 때문.
  - ⚠️ **08-07 개정 — MIN 살포 폐기, 몰아주기.** 이전엔 MIN_CHANNEL_BUDGET(300)을 전 채널에 깔았는데, need(≈2.4만)가 하루 예산(≈8천)보다 훨씬 큰 상황(커서 깊은 채널)에선 대부분이 **통과 비용(고빈도 채널 ~540)에도 못 미쳐 커서에 닿지도 못하고 300씩 헛씀**(실측 로그: 신규0/훑음0). 그래서 need 적은 순으로 몰아주되 **통과 비용조차 못 받을 채널은 0으로 건너뛴다**. 한 채널을 완주/exhausted시켜 다음부터 그 통과 비용을 없애는 게 최선(경위 `../DECISIONS.md` 08-07).
- **필요량(`need`)은 통과 비용 + 소급 비용**이다(2026-07-25). 커서까지 다시 넘기는 `days_since_cursor × PASS_UNITS_PER_DAY`와 커서 아래로 내려가는 `days_left × UNITS_PER_DAY`의 합. 통과 비용을 빼먹으면 커서 깊은 채널(YTN, 커서 92일 전)이 남은 일수가 적다는 이유로 최소 예산만 받아 **커서에 닿지도 못하고 끝난다**(실측: 300 unit으로 훑음 0건).
- `TOTAL_BUDGET`(7,920)만 조정하면 되고, `EXCLUDED_OUTLETS`는 이미 충분히 모인 채널을 제외
- 재개 지점은 `channels.backfill_cursor`(실제로 훑은 가장 오래된 지점). `MIN(published_at)`을 쓰면 정치 영상이 없는 구간을 훑고도 커서가 안 움직여 매일 같은 구간을 다시 훑게 됨
- 하한선 `RETENTION_FLOOR`(2025-08-01, 고정 날짜)는 이 파일에 정의한다. 롤링이 아니라 고정인 이유는 창이 앞으로만 자라 고정 평가셋이 깨지지 않게 하려는 것. 이보다 내리면 윤석열 정권기가 섞여 라벨 규칙이 깨지므로 내리지 말 것
- 채널 조회 시 `outlet_name`이 아니라 **`CHANNELS`에서 얻은 `channel_id`로 직접 조회** — outlet_name이 DB에서 유일하지 않아 엉뚱한 채널을 긁은 사고가 있었음
- 다시 쓸 땐 `TOTAL_BUDGET`을 그날 남은 실제 쿼터에 맞춰 조정할 것 (채널별 배분은 자동)

### `sync.py` — 스테이징(Neon) → 로컬 분석 DB 동기화
로컬에서 수동 실행. 전 테이블을 FK 순서로 키셋 청크 복사(로컬은 `ON CONFLICT`라 멱등)한 뒤, **이번 실행에서 복사한 기본키만** 스테이징에서 지운다 — 동기화 중 수집이 새로 넣은 행은 지워질 수 없다.

- 스테이징에 남기는 것: `outlets`·`channels`(backfill 커서의 정본), 채널별 최신 영상 + 최근 `--keep-days`(기본 14)일 영상. 지우는 것: 옮긴 `comments`, 라벨 테이블 행, 오래된 영상(댓글이 스테이징에 남지 않은 것만).
- 충돌 처리: `channels`는 스테이징 값으로 갱신, `videos`는 `comments_disabled`·`last_checked_at`만 반영(`is_political`은 로컬 `reclassify.py`가 정본), 나머지는 DO NOTHING.
- `--keep-staging`: 복사만. `--from-env NAME`: 다른 DB에서 복사만(원본은 절대 안 지움 — 옛 Neon 백업 이관에 사용). `--seed-staging`: 새 스테이징에 로컬의 수집 상태(채널·커서·채널별 최신 영상)를 심는다 — 빈 스테이징이면 backfill이 "지금"부터 다시 소급해 쿼터를 낭비하므로 필수.

### `reclassify.py` — 기존 영상의 정치 여부 재판정
`keywords.json`을 고친 뒤 이미 저장된 영상의 `is_political`을 다시 계산한다. 키워드는 정치인·정당명이 섞여 있어 시간이 지나면 낡으므로 주기적으로 갱신 → 재판정이 필요하다.

## AI/ — 학습·라벨링

### `train.py` — 성향 분류기 학습
`beomi/KcELECTRA-base-v2022` 파인튜닝. 학습셋은 `--train-run`으로 고른다(기본 `llm_train_v1`) / 평가 300건(`manual_gold_v1`). 최종 배포 모델은 v2 라벨 1000건(`llm_train_v2_big`)으로 학습해 `models/kcelectra_v2`에 저장했다(근거: `../DECISIONS.md` 08-03).

- **평가셋 오염 차단이 SQL에 박혀 있다** — `run_id='manual_gold_v1'`인 라벨 전부(잉여분 포함)를 학습에서 제외한다.
- `--train-run RUN`(학습셋 run_id 지정), `--curve`(학습곡선, **스텝 수 고정**), `--tune`(dev로 하이퍼파라미터 선택 후 평가셋 1회 측정), `--class-weights`(드문 클래스 보정), `--save DIR`(모델 저장).
- **epoch을 너무 작게 두면 학습이 안 된다** — 700÷32=22스텝/epoch이라 4 epoch(88스텝)은 부족해 `좌`·`불가`를 아예 예측 못 한다. 기본 15 epoch(330스텝).

### `train_intensity.py` — 과격도(강도 축) 분류기 학습
성향 축(train.py)과 **독립**. `humane-lab/K-HATERS`(네이버 뉴스 댓글 19만, EMNLP 2023)의 4단계 공격성(`normal`<`offensive`<`L1_hate`<`L2_hate`)으로 KcELECTRA를 파인튜닝 → `models/khaters_v1`. **우리 손라벨 불필요**(데이터셋에 라벨이 있음). 학습 루프는 train.py와 같은 검증된 패턴(fp16 GradScaler·AdamW·OneCycleLR·class-weights). HuggingFace `datasets`로 로드. ⚠️ `LABELS` 순서가 `infer.py`의 `_postprocess_intensity`와 일치해야 함(모델 출력 인덱스). 사용: `python AI/train_intensity.py --epochs 2 --class-weights --save ../models/khaters_v1` (스모크: `--limit 2000 --epochs 1`).

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

- **증분이 기본** — 이 run_id로 아직 라벨 없는 정치 댓글만 추론(`NOT EXISTS`, PK(run_id,comment_id) 인덱스 조회). 수집이 계속돼도 매번 전량(61만)을 다시 안 돌린다. 모델 교체로 전량 덮어쓸 땐 `--reinfer`, 적재 없이 점검은 `--no-db`(이번 배치 기준 순위), 빠른 점검은 `--limit N`(요약 파일 안 덮음).
- **원격 DB 접근은 앞뒤 한 번씩만** — 시작에 대상 fetch(연결 닫음) → GPU 추론(DB 무접근) → 완료 후 연결 하나로 `comment_labels` 청크 UPSERT + 순위 집계. label=argmax, score=P(우)−P(좌), confidence=max prob.
- **길이순 버킷팅**(`run_inference`) — 비슷한 길이끼리 배치해 패딩 낭비 제거 → **추론 ~4배**(실측 15k: 150→600건/s). 결과는 원래 순서로 복원해 ids/like와 정렬 유지(수치 동일).
- **순위는 SQL GROUP BY로 전체 run 집계**(`fetch_run_aggregates`) — 60만 행을 파이썬으로 안 끌어오고 채널 ~13행만. 증분 추론 뒤에도 순위가 전체를 반영. (`--no-db`일 때만 이번 배치로 in-메모리 집계.) ⚠️ CockroachDB는 `ln()`에 float 캐스팅 필요(`::float8`).
- **구간별 타이머**(fetch/infer/push)를 출력해 병목을 바로 볼 수 있다. 실측 병목은 **GPU 추론**(push·fetch보다 큼).
- **2축 지원** — 축별 후처리를 `AXIS_CONFIG`로 분리. `leaning`(P우−P좌) / `intensity`(과격도=기대서열/3, label NULL) 둘 다 구현. `--axis intensity`는 `models/khaters_v1`·run_id `model_khaters_v1`. 집계·순위도 axis-aware(intensity는 unusable 필터 없이 전량, 과격한 순 정렬).
- 기본 모델 `models/kcelectra_v2`, run_id `model_kcelectra_v2`. `results/<run_id>.txt`(순위·분포)·`.progress`(진행률)를 로컬에도 남긴다(`results/`는 gitignore).

### `requirements.txt` (수집용) / `AI/requirements.txt` (학습용)
- **`requirements.txt`** — 수집·ETL과 CI가 쓴다. `google-api-python-client` / `psycopg2-binary` / `python-dotenv`. 병렬 처리는 표준 라이브러리(`concurrent.futures`)라 별도 의존성 없음.
- **`AI/requirements.txt`** — 학습·추론 전용(로컬 GPU). `-r ../requirements.txt`로 수집 의존성을 상속하고 `torch`(cu128 전용 인덱스)·`transformers`·`numpy`를 더한다. **GitHub Actions는 이 파일을 설치하지 않는다** — 러너엔 GPU가 없고 torch 2.8GB를 매 실행 받을 이유가 없다.

### `.env` (gitignore됨)
- `YOUTUBE_API_KEY`
- `DATABASE_URL` — **로컬 분석 DB**(Docker 컨테이너 `newstance-pg`, 127.0.0.1:5432). 수집 외의 모든 스크립트가 읽는다.
- `STAGING_DATABASE_URL` — **스테이징 DB(Neon)**. `collect.py`·`backfill.py`·`sync.py`·`migrate.py --staging`만 읽는다.
- `NEON_BACKUP_DATABASE_URL` — 옛 Neon(2026-08-02까지 데이터). 로컬 첫 이관 원본. `COCKROACH_DATABASE_URL` — 체험판 만료로 차단된 옛 운영 DB(보존용).

GitHub Actions에는 `YOUTUBE_API_KEY`·`STAGING_DATABASE_URL` Secrets만 필요하다. 코드는 양쪽에서 동일하게 `os.environ`으로 읽는다.

## 실행 방법

```bash
cd part1
pip install -r requirements.txt

# 로컬 분석 DB (최초 1회): Docker Desktop 실행 상태에서 컨테이너 newstance-pg가 떠 있어야 한다
docker start newstance-pg     # (--restart unless-stopped라 보통 Docker Desktop과 함께 자동 기동)
python migrate.py               # 스키마 생성/갱신 — 로컬(DATABASE_URL)
python migrate.py --staging     # 스키마 생성/갱신 — 스테이징(STAGING_DATABASE_URL)

python Data/sync.py        # ★ 수집분을 로컬로 가져오기 (분석 전에 실행)
python Data/collect.py     # 24시간치 수집 (스테이징에 적재 — 평소엔 GitHub Actions가 매일 실행)
python Data/backfill.py     # 과거 소급 수집 (스테이징에 적재)

python AI/infer.py     # 전체 댓글 추론·적재 (기본 models/kcelectra_v2, run_id=model_kcelectra_v2)

# 시각화는 크롬 익스텐션(part2/)이 맡는다 — chrome://extensions → 개발자 모드 → '압축해제된 확장 프로그램 로드' → part2/
python ../part2/tools/export_data.py   # Part 1 결과를 익스텐션용 정적 JSON으로 추출(갱신 시)
```

## 삭제된 파일

- `viz/`(Streamlit 대시보드) — 2026-09-26 삭제. 결과 시각화는 크롬 익스텐션(part2 `analysis.html`)이 맡는다. 대시보드가 원격 DB에 대량 집계를 반복한 것이 CockroachDB 사용량의 주요 원인 후보이기도 했다(`../DECISIONS.md` 09-26).
- `Data/prune.py` — 하한선 밑 데이터를 지우는 도구. 원래 Neon 512MB 용량 때문에 만들었는데, backfill이 하한에서 멈춰 하한 밑 데이터가 생기지 않고 용량 제약도 없어져 2026-09-26 삭제. `RETENTION_FLOOR`는 `backfill.py`로 옮겼다.
- `train.py --labels human-first` 분기 — 맹검 100건을 사람 라벨로 바꿔 학습하던 실험(07-26 (5), 결과 '사실상 동률'). 최종 모델에 쓰이지 않아 삭제.
- `keywords.py`의 `load_political_keywords()`·`INCLUDE_CATEGORIES` — 키워드를 국내 고유/일반 두 갈래로 나누기 전의 옛 API. 호출처가 없어 삭제.
- `db.py` — test/운영 DB 분리(`ensure_test_database`)를 담당했으나, 필터 검증 후 `collect_test`를 운영 DB `newstance`로 rename·승격하면서 분리가 불필요해져 삭제(2026-08-03). 각 스크립트는 이제 `DATABASE_URL`에 직접 연결하며, 이때 각 파일의 `sys.path` 부트스트랩도 함께 제거했다.
- `quick_test.py` — API 연결 확인용 임시 스크립트였고, 기능(채널 조회 → 영상 목록 → 댓글 수집 + `commentsDisabled` 예외처리)이 전부 `collect.py`에 흡수돼서 삭제함
- 일회성 진단 스크립트 다수(드리프트 검정, 용량 측정, 채널 검증, 라벨 검토 등) — 결론만 `../DECISIONS.md`에 남기고 스크립트는 삭제. 전부 특정 시점의 DB 상태를 전제로 한 것이라 나중에 다시 돌려도 같은 답이 안 나온다
- `cap_comments.py` — 영상당 댓글 20건 상한을 과거 데이터에 소급 적용한 도구. 1회 실행 후 삭제했다. `comments`에 INSERT하는 경로가 `collect.py`·`backfill.py` 둘뿐이고 둘 다 `select_comments()`를 지나므로 상한이 깨질 수 있는 경로가 없다. 무엇보다 **같은 선별 규칙을 Python과 SQL 두 벌로 들고 있는 상태**라서, 남겨두면 한쪽만 고쳐 어긋나는 사고를 부른다. 상한 값을 바꿔 소급 재적용할 일이 생기면 그때 다시 쓴다
