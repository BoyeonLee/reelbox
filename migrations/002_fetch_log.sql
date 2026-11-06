-- 002_fetch_log: 보강 단계가 플랫폼에 요청한 기록. 인스타 일일 한도(최근 24시간 개수)를 센다.
-- 실행할 때마다 프로그램이 새로 뜨므로, 이전 실행에서 보낸 요청 수는 여기서만 알 수 있다.
CREATE TABLE fetch_log (
    id         INTEGER PRIMARY KEY,
    platform   TEXT NOT NULL CHECK (platform IN ('instagram', 'youtube')),
    item_id    INTEGER REFERENCES items (id) ON DELETE SET NULL,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_fetch_log_platform_time ON fetch_log (platform, fetched_at);
