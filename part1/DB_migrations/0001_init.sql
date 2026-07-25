CREATE TABLE outlets (
    outlet_name TEXT PRIMARY KEY
);

CREATE TABLE channels (
    channel_id TEXT PRIMARY KEY,
    outlet_name TEXT NOT NULL REFERENCES outlets(outlet_name),
    title TEXT NOT NULL,
    uploads_playlist_id TEXT NOT NULL,
    backfill_cursor TIMESTAMPTZ
);

CREATE TABLE videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    title TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    category_id TEXT,
    is_political BOOLEAN,
    comments_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_checked_at TIMESTAMPTZ,
    raw JSONB
);

CREATE TABLE comments (
    comment_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id),
    text TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    label TEXT,
    label_source TEXT CHECK (label_source IN ('manual', 'model')),
    raw JSONB
);

CREATE INDEX idx_channels_outlet ON channels(outlet_name);
CREATE INDEX idx_videos_channel ON videos(channel_id);
CREATE INDEX idx_comments_video ON comments(video_id);
