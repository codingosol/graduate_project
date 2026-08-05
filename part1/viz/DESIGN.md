# 시각화 대시보드 DESIGN.md — Streamlit

Part 1 결과(채널별 정치 성향·시계열·우편향)를 탐색·캡처하는 **로컬 Streamlit 대시보드**의 디자인 정본.

- **레이아웃**: designmd.app "Data-Dense Dashboard" — 촘촘한 그리드, KPI 카드, 숫자 monospace, "density without chaos".
- **색상**: Sentry 디자인 시스템(다크 보라 캔버스 + 퍼플 액센트). 정치 성향 축은 Sentry 팔레트 안의 blue↔red를 diverging으로 차용(정치 관례와도 일치).
- 과설계 금지: BI 서버 없이 스크립트 한 벌, `newstance` DB 직접 연결, 로컬 실행.

---

## 1. 색상 토큰 (Sentry, 다크 기준)

**표면 / 잉크 (UI 크롬)**
| 역할 | hex | 용도 |
|---|---|---|
| canvas | `#1D1127` | 앱 배경(가장 어두움) |
| surface | `#2B1D38` | 카드·패널·사이드바 |
| ink-strong | `#E7E1EC` | 제목·주요 텍스트 |
| ink-muted | `#9386A0` | 보조 텍스트·축 레이블 |
| border | `#776589` | 카드 경계·구분선(저채도) |

**액센트 (강조·상호작용, 데이터 아님)**
| 역할 | hex |
|---|---|
| accent(primary) | `#6C5FC7` (purple300) — 선택·버튼·링크·단일 강조선 |
| accent-soft | `#A396DA` (purple200) — hover·보조 강조 |

**데이터 색 — 성향 diverging (0 중심)**
| 극 | hex | 의미 |
|---|---|---|
| 진보(score<0) | `#3D74DB` (Sentry blue300) | cool |
| 중립(score≈0) | `#9386A0` (Sentry gray300) | 회색 중간점(무지개 금지) |
| 보수(score>0) | `#F55459` (Sentry red300) | warm |

> 성향축에 퍼플을 쓰지 않는다 — 진보/보수 양극이 구분돼야 하므로 diverging 두 색은 blue↔red.
> 퍼플은 "데이터가 아닌 강조"(선택된 채널, 우비율 단일선 등) 전용.

**상태 / 이벤트**
| 역할 | hex |
|---|---|
| good | `#33BF9E` (green300) |
| warning | `#FFC227` (yellow300) |
| event-marker | `#FF7738` (orange400) — 시계열 이벤트 주석선(6·3 지방선거 등) |

⚠️ **구현 시 눈으로 판단하지 말고** dataviz 스킬의 `validate_palette.js`로 검증한다:
diverging 두 극(blue300/red300)과 중립(gray300)을 **다크 표면(#1D1127, #2B1D38)** 기준으로
CVD 분리·대비 통과 확인(파랑↔빨강은 색맹 안전한 편이나 명시 검증). 라이트 모드는 별도 스텝으로.

## 2. 타이포 / 간격 (Data-Dense)

- 폰트: System UI 스택(본문 400 / 제목 600–700). **숫자·점수·표 셀은 monospace**(자릿수 정렬).
- 간격: **8px 베이스**. 카드 내부 패딩 12px, 카드 간 간격 8px. 여백은 장식이 아니라 구조.
- 밀도 우선: 큰 히어로 여백 대신 화면당 정보량을 높이되, 경계·정렬로 질서 유지.

## 3. 레이아웃

```
┌ 사이드바(surface) ─────────┐  ┌ 본문(canvas) ───────────────────────────┐
│ 채널 다중선택(기본 전체)     │  │ [KPI 행]  전체평균 · 진보최댓값 · 보수최댓값 · 표본수  │
│ 기간 슬라이더               │  │  ── st.metric 카드(surface, monospace) ──          │
│ 축: 성향 / 우비율           │  │ [탭1 채널순위] [탭2 시계열] [탭3 우편향] [탭4 원자료]  │
│ news · opinion · 전체       │  │                                                    │
└────────────────────────────┘  └────────────────────────────────────────────────┘
```

- 상단 **KPI 카드 행**(`st.columns` + `st.metric`, 카드는 surface 색·12px 패딩).
- 그 아래 **탭 4개**. 12칸 그리드는 Streamlit `st.columns(n)`으로 근사.

## 4. Streamlit 적용

`.streamlit/config.toml` (테마를 Sentry로 고정):
```toml
[theme]
base = "dark"
primaryColor = "#6C5FC7"            # 액센트(퍼플)
backgroundColor = "#1D1127"         # canvas
secondaryBackgroundColor = "#2B1D38" # surface(카드·사이드바)
textColor = "#E7E1EC"
font = "sans serif"
```
- 카드 테두리·monospace 숫자 등 세부는 `st.markdown(unsafe_allow_html=True)`로 최소 CSS 주입.
- 차트는 Plotly `template="plotly_dark"`를 베이스로 위 토큰 색을 덮어쓴다.

## 5. 차트 스펙 (form + color, dataviz 원칙 준수)

| 탭 | form | 색 배정 | 원칙 |
|---|---|---|---|
| 채널 순위 | 수평 막대(정렬) | 막대=성향 diverging(blue↔gray↔red), 통념 라벨 병기 | 한 축. 색만으로 판단 안 함(라벨 병기) |
| 시계열 | 선그래프 | 채널=entity 고정색(진영 톤: 진보 blue계·보수 red계·중도 gray계) | **한 화면 ≤8선** — 10개 전체는 진영 묶음 평균, 개별은 다중선택. 이중축 금지. crosshair+tooltip |
| 우비율 추세 | 단일 선 | accent purple(`#6C5FC7`) 1색 | 단일 series → 범례 없음. 이벤트는 orange 수직 주석선 |
| 대상별 우비율 | 수평 막대 | 50% 기준 diverging(blue↔red) | 여당/야당/무언급 3막대 |
| 원자료 | 표 + CSV | ink 텍스트(색 재사용 금지), 숫자 monospace | 색 못 봐도 수치 확인 가능(접근성 백업) |

## 6. 접근성 / 마감

- 2개 이상 series엔 범례 항상, 4개 이하면 직접 라벨. 값·축 텍스트는 ink 색(series 색 재사용 금지).
- 색은 **entity(채널)에 고정** — 필터로 채널이 빠져도 남은 채널 색 불변.
- 표 뷰(탭4)가 항상 존재. 렌더 후 레이블 충돌·오버플로 눈으로 확인.

## 7. 데이터 / 성능

- `newstance`(DATABASE_URL) 직접 연결. 추론 run = `model_kcelectra_v2`.
- 집계는 SQL(주별·채널별 GROUP BY)에서 끝내고 파이썬은 그림만. `@st.cache_data`로 결과 캐싱 → 위젯 조작마다 DB 미접근.

## 8. 비용 / 범위 밖

- `streamlit`·`plotly`·`pandas`·`psycopg2` 전부 무료 오픈소스. 로컬 실행이라 호스팅비 0, Neon는 읽기+캐싱이라 부담 미미.
- 의존성은 `part1/viz/requirements.txt`에 별도(수집 CI·학습과 무관).
- **범위 밖(지금 안 함)**: 강도 축(K-HATERS) 2축 산점도, 실시간 갱신·인증·멀티유저.

---

# v2 요구사항 — 2페이지 구조 & 구현 TODO

## 공통 데이터 필터

- **분류된 댓글만 집계.** `collect.py`가 계속 수집 중이라 아직 추론 안 된 신규 댓글이 있다.
  → 항상 `comment_labels`(run='model_kcelectra_v2')와 **inner join**해서, 그 run에 라벨이 있는 댓글만 쓴다(미분류는 자동 배제).
- `unusable` 제외, `neutral`은 포함(성향 집계).

## 페이지 A — 대시보드 (채널 개요)  `app.py`

채널 한 줄 = **[유튜브 로고] [채널명] [좌·중립·우 비율 바]** + 우비율 숫자. 클릭하면 상세로.

- **비율 바(중립 처리)**: 100% 가로 누적 바를 **좌(파랑) | 중립(회색) | 우(빨강)** 순서로 → 중립이 가운데라
  아무리 커져도 **좌·우가 양 끝에 붙어 길이 비교가 유지**된다. (대안: 중립 제외 diverging 바 — 중립 정보 손실이라 비권장.)
- **우비율(좌+우 중 우%)은 바에 병기하지 않는다.** 바는 좌/중립/우 비율만 보여주고, 우비율은
  **별도 추가 정보로 분리**한다(예: 상세 페이지 지표, 또는 보조 컬럼/툴팁).
- **로고**: `channels`에 썸네일 URL이 없다 → **마이그레이션 0009로 `channels.thumbnail_url` 추가 + `channels.list`(snippet.thumbnails) 1회 수집** 필요.
- **채널 클릭 → 상세**: `st.page_link`+쿼리파라미터(`?channel=`) 방식 권장(간단). plotly 클릭 이벤트는 별도 lib 필요.

## 페이지 B — 채널 상세  `pages/1_channel_detail.py`

- **시계열(좌/우 변동)**: 세로축 = %, 가로축 = 시간(주별). 좌%·우% 두 선(diverging 색). 중립은 옅게 or 생략.
  - **원시 / EMA 전환 드롭다운**: `st.selectbox`(원시 · EMA · 둘 다)로 보기를 전환. EMA는 지수이동평균(`span≈4주`, 위젯 조절).
    - (a) **선 교체 방식**: 한 그래프에서 plotly trace의 visible을 토글해 진한 선(선택)/옅은 선을 서로 교체(간단·안정).
    - (b) **별도 그래프 + 모션**: 원시·EMA 그래프를 각각 두고, 선택에 따라 **CSS transition(fade/slide)으로 자연스럽게 교체**
      (`@keyframes`를 `st.markdown(unsafe_allow_html=True)` 컨테이너에 주입; plotly 자체 `transition`도 병용). ⚠️ Streamlit은 리렌더 기반이라 매끄러움이 제한될 수 있어 구현 때 육안 검증.
  - EMA는 시간가중이라 표본 크기를 무시(약점) → 아래 영상/댓글 수 트랙이 표본 적은 구간을 눈으로 보완.
- **정점마다 영상 수·댓글 수 표시**(채널별 수집량이 달라 필수 맥락): 각 주의 영상/댓글 수를
  **hover 툴팁 + 시계열 하단에 x축을 공유하는 별도 막대 트랙**으로(성향 선과 다른 스케일이라 **이중축은 금지**, 트랙 분리로 회피).
- 표본이 적은 주는 신뢰구간이 넓으니 **주별 표본 수가 임계 미만이면 흐리게** 처리.

## 강도 축 미리 반영 (2-3)

- `comment_labels.axis`가 이미 `leaning`/`intensity`를 구분한다. 쿼리·위젯을 **axis 파라미터로** 짠다.
- 대시보드: 축 선택 토글(현재 leaning만, intensity는 비활성 자리). 상세: 강도 시계열 탭 placeholder.
- 강도 run(`model_khaters_v1`) 적재되면 토글만 켜면 되도록.

## 구현 TODO (다음 세션)

1. [ ] 마이그레이션 `0009_channel_thumbnail.sql`(`channels.thumbnail_url`) + 로고 1회 수집 스크립트.
2. [ ] `viz/queries.py` — `@st.cache_data` 집계 쿼리(채널별 좌/중립/우, 주별 좌%·우%, 주별 영상/댓글 수). run·axis 파라미터화. **미분류 배제 필터 포함.**
3. [ ] `.streamlit/config.toml` — Sentry 다크 테마(§4).
4. [ ] `app.py` — 대시보드: 로고 + 좌·중립·우 누적 바 + 우비율, 채널 클릭 네비.
5. [ ] `pages/1_channel_detail.py` — 시계열(%)+정점 영상/댓글 수 트랙, 강도 탭 placeholder.
6. [ ] `viz/requirements.txt`(streamlit·plotly·pandas·psycopg2) + 팔레트 `validate_palette.js` 검증(다크 배경) + 렌더 육안 확인.

## 결정 필요 (구현 전, 내 추천 표시)

- **중립 대부분 처리** → 좌|중립|우 누적 바 + 우비율 병기 **(추천)**.
- **채널 클릭 방식** → `st.page_link`+쿼리파라미터 **(추천, 무의존성)** / plotly 클릭(lib 추가).
- **시계열 표현** → 좌%·우% 2선 **(추천, "좌/우 변동" 직역)** / 우비율 1선(간결).
