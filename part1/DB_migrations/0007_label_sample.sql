-- 평가셋(정답셋)으로 쓸 댓글 표본을 '고정'한다.
--
-- 왜 필요한가 (실제로 터진 문제):
--   라벨링 툴은 표본 300건을 질의할 때마다 md5(comment_id) 순서로 '새로 뽑고' 있었다.
--   즉 표본이 DB 내용에 의존하는 파생값이라, 수집이 진행될 때마다 표본 자체가 바뀌었다.
--   2026-07-21 backfill 직후 실측: 손으로 매긴 120건 중 6건이 표본 밖으로 밀려났고,
--   라벨된 항목이 표본 0~176번 자리에 흩어지며 앞쪽에 미라벨 구멍 63개가 생겼다.
--
--   더 나쁜 점은 이 뒤섞임이 채널과 상관관계를 가졌다는 것이다. backfill에서 제외한
--   오마이TV는 표본이 그대로여서 24/24 전부 라벨된 반면, 댓글이 크게 늘어난 연합뉴스TV는
--   표본이 통째로 새것으로 교체되어 1건만 남았다. 결과적으로 라벨 120건이 오마이TV에
--   20% 쏠린 편향 표본이 되어, "채널별 편향 점검"이라는 원래 목적에 쓸 수 없게 됐다.
--
-- 평가셋은 한 번 뽑으면 고정되어야 한다(held-out set). 그래서 표본을 질의로 매번 계산하지 않고
-- 이 테이블에 못박고, 라벨링 툴은 여기에 조인해서 항상 같은 300건을 같은 순서로 보여준다.

CREATE TABLE label_sample (
    run_id TEXT NOT NULL REFERENCES label_runs(run_id) ON DELETE CASCADE,

    -- ON DELETE RESTRICT인 이유 (comment_labels의 CASCADE와 의도적으로 다르다):
    -- comment_labels는 CASCADE라, 보관 하한선을 내려 오래된 댓글을 지우면 손으로 매긴 라벨이
    -- 아무 경고 없이 함께 사라진다. 실제로 2026-04-21 하한 프루닝에서 120건 중 25건이
    -- 이렇게 날아갈 뻔했다(사전 백업으로 원문까지 보존해 두고 진행).
    -- 고정된 평가셋에까지 같은 일이 생기면 실험 자체가 무효가 되므로, 여기서는 삭제를
    -- 조용히 전파시키지 않고 아예 '막는다'. 표본에 든 댓글을 지우려면 사람이 먼저
    -- 평가셋을 어떻게 할지 결정해야 한다.
    comment_id TEXT NOT NULL REFERENCES comments(comment_id) ON DELETE RESTRICT,

    -- 고정된 제시 순서. 라벨링 순서가 곧 이 값이므로, 나중에 "앞부분과 뒷부분의 판정 기준이
    -- 흔들렸는지(drift)"를 검사할 수 있다. 표본을 매번 새로 뽑던 때는 순서 정보가 없어
    -- 이 검사가 불가능했다.
    ord INTEGER NOT NULL,

    frozen_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, comment_id)
);

-- 순서가 run 안에서 유일해야 "n번 항목"이 항상 한 건을 가리킨다.
CREATE UNIQUE INDEX idx_label_sample_ord ON label_sample(run_id, ord);
