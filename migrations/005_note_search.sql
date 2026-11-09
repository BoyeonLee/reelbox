-- 005_note_search: 메모(user_meta.note)도 키워드 검색에 넣는다 (Phase 7).
-- FTS5 표는 칸을 나중에 추가할 수 없어서, note 칸이 있는 새 표를 만들고
-- 기존 색인 내용 + 메모를 그대로 복사한 뒤 바꿔치기한다 (다시 색인할 필요 없음).
CREATE VIRTUAL TABLE items_fts_new USING fts5 (
    title, description, transcript, summary, tags, note,
    tokenize = 'trigram'
);

INSERT INTO items_fts_new (rowid, title, description, transcript, summary, tags, note)
SELECT f.rowid, f.title, f.description, f.transcript, f.summary, f.tags, COALESCE(um.note, '')
FROM items_fts AS f
LEFT JOIN user_meta AS um ON um.item_id = f.rowid;

DROP TABLE items_fts;
ALTER TABLE items_fts_new RENAME TO items_fts;
