// Reelbox 데이터 창구. 화면(app.js)은 이 파일의 ReelboxData 만 통해 데이터를 읽고 쓴다.
// 지금은 PC API(/api/*)만 쓴다. Phase 8 에서 여기만 고쳐서 PC 가 꺼져 있으면
// 스냅샷 JSON 을 읽게 바꾼다 (DESIGN.md 6장 "보기 전용 사이트": 같은 TV, 채널만 바꾸기).
//
// 돌려주는 항목(카드) 모양:
//   { id, platform, url, title, author, summary, topic_id, topic, saved_at,
//     duration_sec, thumb, media_type, status, error, hidden, favorite, deleted_at, purge_at }

(function () {
  "use strict";

  // 토큰이 없거나 틀렸을 때. 화면은 이걸 받으면 토큰 입력 화면을 띄운다.
  class AuthError extends Error {
    constructor() {
      super("토큰이 필요합니다");
      this.name = "AuthError";
    }
  }

  async function request(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new Error("PC에 연결할 수 없어요");
    }
    if (res.status === 401) throw new AuthError();
    if (!res.ok) {
      let detail = `요청 실패 (${res.status})`;
      try {
        const data = await res.json();
        if (typeof data.detail === "string") detail = data.detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function query(params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    }
    const s = q.toString();
    return s ? `?${s}` : "";
  }

  window.ReelboxData = {
    AuthError,
    mode: "pc", // Phase 8: "pc" | "snapshot"

    // 인증
    login: (token) => request("POST", "/api/login", { token }),
    logout: () => request("POST", "/api/logout"),

    // 조회
    home: () => request("GET", "/api/home"),
    topic: (id) => request("GET", `/api/topics/${id}`),
    // { topicId, favorite, sort: "recent"|"oldest", limit, offset } → { items, next_offset }
    items: ({ topicId, favorite, sort, limit, offset } = {}) =>
      request("GET", "/api/items" + query({ topic_id: topicId, favorite: favorite ? "true" : "", sort, limit, offset })),
    item: (id) => request("GET", `/api/items/${id}`),
    // { q, platform, topic, since, until, favorite, limit, offset } → { items, next_offset, semantic }
    search: (params) => request("GET", "/api/search" + query(params)),
    status: () => request("GET", "/api/status"),
    // 실패한 항목 전체 → [{ id, platform, url, title, error, attempts, retry_requested, updated_at }]
    failedItems: () => request("GET", "/api/failed"),
    // 주제 고르기용 → [{ id, name, count, fixed }]
    topics: () => request("GET", "/api/topics"),
    hiddenItems: () => request("GET", "/api/hidden"),
    trashItems: () => request("GET", "/api/trash"),
    recentAdds: () => request("GET", "/api/adds"),
    // 자동 처리 워커 → { enabled, busy, step, pending, paused: { 단계: { reason, until } }, notice }
    workerState: () => request("GET", "/api/worker"),

    // 숨기기·삭제·복원. 모두 { ids: [바뀐 id] } 를 돌려준다.
    setHidden: (id, hidden) => request("PATCH", `/api/items/${id}`, { hidden }),
    trash: (id) => request("DELETE", `/api/items/${id}`),
    restore: (id) => request("POST", `/api/items/${id}/restore`),
    // action: "hide" | "unhide" | "delete" | "restore"
    bulk: (ids, action) => request("POST", "/api/items/bulk", { ids, action }),
    emptyTrash: () => request("POST", "/api/trash/empty"),

    // 즐겨찾기·메모 (Phase 7). note 가 빈 글자면 메모를 지운다.
    setFavorite: (id, favorite) => request("PATCH", `/api/items/${id}`, { favorite }),
    setNote: (id, note) => request("PATCH", `/api/items/${id}`, { note }),

    // 주제 고치기 (Phase 7). 이름 바꾸기·합치기는 남은 주제 { id, name, count } 를 돌려준다.
    renameTopic: (id, name) => request("PATCH", `/api/topics/${id}`, { name }),
    mergeTopic: (id, into) => request("POST", `/api/topics/${id}/merge`, { into }),
    setItemTopic: (itemId, topicId) => request("PUT", `/api/items/${itemId}/topic`, { topic_id: topicId }),

    // 실패 다시 시도 (Phase 7) → { ids }
    retry: (id) => request("POST", `/api/items/${id}/retry`),
    retryAll: () => request("POST", "/api/failed/retry"),

    // 추가 → { result, message, item }
    addUrl: (url) => request("POST", "/api/items", { url }),
  };
})();
