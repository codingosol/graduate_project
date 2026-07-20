-- 0004에서 만든 idx_comment_labels_comment를 제거한다.
--
-- 0004 작성 시엔 "한 댓글의 여러 run 라벨을 가로로 비교할 때 필요할 것"이라고 예상했으나,
-- 39.8만 x 2 run + 수동 3천 = 79.9만 행을 넣고 실측한 결과 플래너가 이 인덱스를 한 번도 쓰지 않았다.
--
--   질의                        인덱스 있음   인덱스 없음   실제 사용된 계획
--   정답셋 대조(3천 x 39.8만)      95ms         96ms       comment_labels_pkey 로 probe
--   2축 결합(39.8만 x 39.8만)     579ms        585ms       Parallel Hash Join + Seq Scan
--   comment_labels 총 크기        153MB        119MB
--
-- 이유는 PK가 (run_id, comment_id)이기 때문이다. run 간 비교는 언제나 "run A 전체 x run B 전체"
-- 형태라, 한쪽은 PK 범위 스캔으로 뽑고 다른 쪽은 PK로 probe하거나 통째로 해시 조인하는 게 최적이다.
-- comment_id 단독 인덱스가 유리한 경우는 "특정 댓글 하나를 run 전체에서 찾기"뿐인데,
-- 이건 Neon 왕복 지연이 건당 76ms라 애초에 쓰면 안 되는 접근 패턴이다(일괄 조회가 246배 빠름).
--
-- 즉 이 인덱스는 33MB를 쓰면서 측정 가능한 이득이 0이었다. 무료 티어 512MB에서는 그 자체로 탈락 사유.

DROP INDEX IF EXISTS idx_comment_labels_comment;
