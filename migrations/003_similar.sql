-- 003_similar: 영상마다 "비슷한 영상" 몇 개를 미리 계산해 둔다 (DESIGN.md 5-5).
-- index 단계가 실행될 때마다 전부 다시 계산한다. 상세 화면과 보기 전용 사이트에서 쓴다.
CREATE TABLE item_similar (
    item_id    INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    similar_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,  -- 1 이 가장 비슷함
    score      REAL    NOT NULL,  -- 코사인 유사도 (1 에 가까울수록 비슷)
    PRIMARY KEY (item_id, similar_id)
);

CREATE INDEX idx_item_similar_rank ON item_similar (item_id, rank);
