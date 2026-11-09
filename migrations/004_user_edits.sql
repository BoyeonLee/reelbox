-- 004_user_edits: 휴대폰에서 직접 고친 것을 자동 처리가 덮어쓰지 않게 표시한다 (Phase 7).

-- 내가 영상의 주제를 직접 고른 연결. organize --full 이 지우거나 다시 배정하지 않는다.
ALTER TABLE item_topics
    ADD COLUMN is_user_set INTEGER NOT NULL DEFAULT 0 CHECK (is_user_set IN (0, 1));

-- 실패 화면에서 "다시 시도"를 누른 항목. 내보내기로 들어온 항목이어도 워커가 처리한다.
ALTER TABLE items
    ADD COLUMN retry_requested INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0, 1));
