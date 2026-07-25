-- raw JSONB 컬럼이 무료 티어 용량을 잠식(comments.raw만 253MB, 실제 쓰는 text는 25MB)해서
-- 필요한 필드만 일반 컬럼으로 추출하고 raw를 제거한다.
--
-- 추출 대상 (comments):
--   authorChannelId.value -> author_channel_id : 작성자 고유 ID. 브리게이딩(동일인 다중 도배) 탐지용
--   totalReplyCount       -> total_reply_count : 대댓글 수(논쟁 유발도), 좋아요와 다른 참여도 축
--   textOriginal          -> text 로 사용       : 기존 text는 textDisplay라 <br>·&quot; 등 HTML이 섞임(23.5%)
--                                                 추가 용량 없이 NLP 품질만 개선
-- 나머지 필드는 이미 별도 컬럼에 있거나(id/videoId/likeCount/publishedAt),
-- API 내부용이거나(etag/kind/canReply/viewerRating), 순수 낭비(authorProfileImageUrl 24MB)라 버림.
--
-- videos.raw(21MB)도 같은 이유로 제거. title/publishedAt/channelId는 이미 컬럼이고
-- 나머지(thumbnails 5종 URL, etag, position, description 정형문구)는 분석에 쓰지 않음.
--
-- ※ UPDATE로 컬럼을 채우는 방식은 22만 행을 제자리에서 재작성해 일시적으로 용량이 2배 필요한데,
--   무료 티어 512MB 한도에 걸려 실패했음(DiskFull). 그래서 raw가 없는 새 테이블을 만들어
--   데이터를 옮기고 통째로 교체하는 방식으로 구현한다. 새 테이블은 raw가 없어 훨씬 작아서
--   용량이 빠듯한 상황에서도 안전하게 수행된다.

ALTER TABLE videos DROP COLUMN IF EXISTS raw;

CREATE TABLE comments_new (
    comment_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    text TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    label TEXT,
    label_source TEXT CHECK (label_source IN ('manual', 'model')),
    author_channel_id TEXT,
    total_reply_count INTEGER
);

INSERT INTO comments_new (
    comment_id, video_id, text, published_at, like_count,
    label, label_source, author_channel_id, total_reply_count
)
SELECT
    comment_id,
    video_id,
    COALESCE(raw->'snippet'->'topLevelComment'->'snippet'->>'textOriginal', text),
    published_at,
    like_count,
    label,
    label_source,
    raw->'snippet'->'topLevelComment'->'snippet'->'authorChannelId'->>'value',
    (raw->'snippet'->>'totalReplyCount')::INTEGER
FROM comments;

DROP TABLE comments;
ALTER TABLE comments_new RENAME TO comments;

CREATE INDEX idx_comments_video ON comments(video_id);
CREATE INDEX idx_comments_author ON comments(author_channel_id);
