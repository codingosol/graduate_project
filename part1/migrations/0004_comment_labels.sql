-- 댓글 성향/강도 라벨을 comments 테이블에서 분리해 별도 테이블로 옮긴다.
--
-- 왜 분리하는가:
--  1) 용량 안전성 (결정적). comments에 label 컬럼을 두고 UPDATE로 채우면 PostgreSQL은 MVCC 때문에
--     행을 제자리에서 고치지 않고 새 버전을 새로 쓴다. 즉 39만 행을 라벨링하면 132MB 테이블이
--     일시적으로 2배로 부풀고, 무료 티어 512MB에서는 0002 때와 똑같은 DiskFull이 재현된다.
--     별도 테이블에 INSERT하면 추가되는 건 라벨 몇 바이트뿐이라 이 문제가 아예 생기지 않는다.
--  2) 재라벨링이 파괴적이지 않음. 모델을 새로 학습할 때마다 기존 라벨을 덮어쓰는 게 아니라
--     run_id를 새로 만들어 나란히 쌓는다. 이전 모델과 결과를 비교할 수 있고,
--     무엇보다 손으로 매긴 정답셋(manual)이 모델 추론에 덮여 날아가는 사고를 구조적으로 막는다.
--  3) 2축 설계 수용. 성향(좌/우)과 강도(온건/과격)는 서로 다른 모델이 만드는 별개의 축이라
--     컬럼 2개를 더 붙이는 것보다 "축이 다른 run"으로 표현하는 편이 깔끔하다.
--
-- 기존 comments.label / label_source는 전부 NULL 상태(한 건도 라벨링 안 됨)라 버려도 손실이 없다.
-- PostgreSQL의 DROP COLUMN은 테이블을 재작성하지 않는 메타데이터 연산이라 용량이 빠듯해도 안전하다.

CREATE TABLE label_runs (
    run_id TEXT PRIMARY KEY,
    -- leaning  : 정치 성향 축. score = P(우) - P(좌), -1(좌) ~ +1(우)
    -- intensity: 표현 강도 축. score = 0(온건) ~ 1(과격)
    axis TEXT NOT NULL CHECK (axis IN ('leaning', 'intensity')),
    label_source TEXT NOT NULL CHECK (label_source IN ('manual', 'model', 'llm')),
    model_name TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE comment_labels (
    comment_id TEXT NOT NULL REFERENCES comments(comment_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES label_runs(run_id) ON DELETE CASCADE,
    -- leaning 축에서만 사용('left'/'right'/'neutral'). intensity 축은 점수만 쓰므로 NULL 허용.
    label TEXT CHECK (label IN ('left', 'right', 'neutral')),
    score REAL,
    -- 모델이 얼마나 확신하는지(최대 클래스 확률). 임계값 미만은 집계에서 제외하는 데 사용.
    confidence REAL,

    -- PK 컬럼 순서가 (run_id, comment_id)인 이유:
    -- 분석 질의는 항상 "특정 run의 라벨 전체를 comments와 조인"하는 형태다.
    -- run_id가 앞이면 해당 run의 행들이 인덱스 상에서 연속으로 모여 범위 스캔이 되고,
    -- 그 안에서 comment_id로 정렬되어 있어 comments(PK=comment_id)와 merge join까지 가능해진다.
    -- (comment_id, run_id) 순서였다면 run 필터에 인덱스를 못 써서 전체 스캔이 된다.
    PRIMARY KEY (run_id, comment_id)
);

-- 한 댓글에 대해 여러 run의 라벨을 가로로 비교할 때 사용
-- (예: 손라벨 vs 모델 예측 불일치 분석, 성향 run과 강도 run 결합).
CREATE INDEX idx_comment_labels_comment ON comment_labels(comment_id);

ALTER TABLE comments DROP COLUMN IF EXISTS label;
ALTER TABLE comments DROP COLUMN IF EXISTS label_source;
