-- 라벨 값에 'unusable'을 추가한다.
--
-- 0004에서는 left/right/neutral 3종이면 충분하다고 봤는데, 실제 라벨링 툴을 만들면서
-- 'neutral'과 구분해야 하는 부류가 따로 있다는 걸 알게 됐다.
--
--   neutral  : 정치적 내용이긴 한데 어느 편인지 드러나지 않음 → 학습에 쓰는 정상 클래스
--   unusable : 애초에 판단 대상이 아님 ("ㅋㅋㅋㅋ", "1등", 광고, 의미 불명) → 학습·집계에서 제외
--
-- 왜 굳이 나누는가: 이 둘을 모두 neutral로 뭉치면 모델이 "노이즈 = 중립"을 학습한다.
-- 그러면 추론 시 대량의 잡음 댓글이 전부 neutral로 분류되어 중립 클래스가 부풀고,
-- 채널 성향 점수가 실제보다 0쪽으로 끌려가 채널 간 차이가 뭉개진다.
-- 집계에서 빼야 할 것과 중립으로 세야 할 것은 반드시 구분해야 한다.

ALTER TABLE comment_labels DROP CONSTRAINT comment_labels_label_check;
ALTER TABLE comment_labels ADD CONSTRAINT comment_labels_label_check
    CHECK (label IN ('left', 'right', 'neutral', 'unusable'));
