-- 001_init: DESIGN.md 4장의 테이블 전부.
-- 시각은 모두 ISO-8601 UTC 문자열(예: 2026-10-30T12:00:00Z)로 저장한다.

-- 저장한 영상 하나 = 한 줄. 각 처리 단계는 status 로 자기 차례를 찾는다.
CREATE TABLE items (
    id             INTEGER PRIMARY KEY,
    platform       TEXT    NOT NULL CHECK (platform IN ('instagram', 'youtube')),
    source_id      TEXT    NOT NULL,  -- 인스타 shortcode, 유튜브 동영상 ID
    url            TEXT    NOT NULL,
    saved_at       TEXT,
    imported_from  TEXT,              -- instagram_export, youtube_sync, urllist, share ...
    collection     TEXT,
    title          TEXT,
    author         TEXT,
    description    TEXT,
    duration_sec   REAL,
    thumbnail_url  TEXT,
    thumbnail_path TEXT,
    media_type     TEXT    CHECK (media_type IN ('video', 'image')),
    status         TEXT    NOT NULL DEFAULT 'imported' CHECK (status IN (
                       'imported', 'enriched', 'analyzed', 'indexed',
                       'failed', 'unavailable', 'skipped', 'deleted')),
    error          TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (platform, source_id)
);

CREATE INDEX idx_items_status ON items (status);
CREATE INDEX idx_items_saved_at ON items (saved_at);

-- updated_at 을 직접 바꾸지 않은 UPDATE 에서만 자동 갱신 (재귀 방지)
CREATE TRIGGER trg_items_updated_at
AFTER UPDATE ON items
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE items
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE id = NEW.id;
END;

-- 자막 또는 음성 받아쓰기 결과
CREATE TABLE transcripts (
    item_id  INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    source   TEXT NOT NULL CHECK (source IN ('captions', 'whisper', 'none')),
    language TEXT,
    text     TEXT
);

-- AI 분석 결과. prompt_version 으로 재분석 대상을 판별한다.
CREATE TABLE analyses (
    item_id          INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    summary_ko       TEXT,
    key_points       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(key_points)),
    tags             TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    raw_topic        TEXT,
    content_language TEXT,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- 주제(바인더). is_user_edited = 1 이면 자동 정리가 덮어쓰지 않는다.
CREATE TABLE topics (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    description    TEXT,
    parent_id      INTEGER REFERENCES topics (id) ON DELETE SET NULL,
    is_user_edited INTEGER NOT NULL DEFAULT 0 CHECK (is_user_edited IN (0, 1))
);

-- 영상 ↔ 주제 연결. 영상마다 대표 주제(is_primary = 1)가 하나.
CREATE TABLE item_topics (
    item_id    INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    topic_id   INTEGER NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (item_id, topic_id)
);

CREATE INDEX idx_item_topics_topic ON item_topics (topic_id);
-- 영상 하나에 대표 주제는 최대 하나
CREATE UNIQUE INDEX idx_item_topics_one_primary ON item_topics (item_id) WHERE is_primary = 1;

-- 휴대폰에서 남기는 표시·메모와 숨김·휴지통 시각
CREATE TABLE user_meta (
    item_id    INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    favorite   INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    note       TEXT,
    hidden_at  TEXT,
    deleted_at TEXT
);

-- 키워드 검색 색인 (trigram: 3글자 조각 단위라 한국어 부분 검색에 유리).
-- rowid = items.id. 내용은 index 단계(Phase 4)가 채운다.
CREATE VIRTUAL TABLE items_fts USING fts5 (
    title, description, transcript, summary, tags,
    tokenize = 'trigram'
);

-- 의미 검색용 벡터 (sqlite-vec, bge-m3 = 1024차원)
CREATE VIRTUAL TABLE embeddings USING vec0 (
    item_id INTEGER PRIMARY KEY,
    vector  float[1024]
);
