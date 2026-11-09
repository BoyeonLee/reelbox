// Reelbox 데이터 창구. 화면(app.js)은 이 파일의 ReelboxData 만 통해 데이터를 읽고 쓴다.
// 같은 TV 에서 채널만 바꾸듯, 여기서 PC API 와 스냅샷 중 하나를 고른다 (DESIGN.md 6장 "보기 전용 사이트").
//
// 어디서 열렸나:
//   - PC 주소(tailscale serve)에서 열림 → /snapshot/meta.json 이 없다 → 항상 PC API (쿠키 로그인)
//   - Pages 주소에서 열림 → meta.json 의 pc_url 로 2초 안에 연결을 시도
//       · 답이 오면(401 포함) "PC 연결됨": 토큰을 Authorization 헤더로 보낸다
//         (아이폰 Safari 는 다른 사이트로 보내는 요청에 쿠키를 붙여 주지 않아서)
//       · 안 오면 "스냅샷": snapshot/*.json 을 읽고, 검색은 MiniSearch 로 휴대폰 안에서
// 스냅샷에서 누른 숨기기·삭제·즐겨찾기·추가는 IndexedDB 대기열에 쌓았다가 PC 에 연결되면 보낸다.
//
// 돌려주는 항목(카드) 모양은 PC API 와 같다:
//   { id, platform, url, title, author, summary, topic_id, topic, saved_at,
//     duration_sec, thumb, media_type, status, error, hidden, favorite, deleted_at, purge_at }

(function () {
  "use strict";

  const PROBE_MS = 2000; // PC 연결을 기다리는 시간
  const TOKEN_KEY = "reelbox_token";
  const TRASH_DAYS = 30;

  // 토큰이 없거나 틀렸을 때. 화면은 이걸 받으면 토큰 입력 화면을 띄운다.
  class AuthError extends Error {
    constructor() {
      super("토큰이 필요합니다");
      this.name = "AuthError";
    }
  }
  // PC 에 닿지 못함 (꺼짐, Tailscale 꺼짐, 2초 초과)
  class NetworkError extends Error {
    constructor() {
      super("PC에 연결할 수 없어요");
      this.name = "NetworkError";
    }
  }
  // 스냅샷에서는 할 수 없는 일
  class OfflineError extends Error {
    constructor(what) {
      super(`${what}는 PC에 연결됐을 때 할 수 있어요`);
      this.name = "OfflineError";
    }
  }
  class HttpError extends Error {
    constructor(status, message) {
      super(message);
      this.name = "HttpError";
      this.status = status;
    }
  }

  // --- 상태 ---
  let site = null; // Pages 에서 열렸을 때 snapshot/meta.json { generated_at, pc_url, items, topics }
  let apiBase = ""; // "" = 같은 주소(PC 에서 열림), 아니면 PC 주소
  let mode = "pc"; // "pc" | "snapshot"
  let snap = null; // { items, byId, topics, mini }
  let queue = []; // 대기열 [{ seq, kind, ... }]
  let ready = null; // 연결 확인 Promise (한 번만)
  let flushing = false;
  const listeners = [];

  function readToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (_) { return null; }
  }
  function writeToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (_) {}
  }

  function state() {
    return {
      mode,
      site: !!site, // Pages 에서 열렸는지
      generatedAt: site ? site.generated_at : null,
      queued: queue.length,
    };
  }
  function notify() {
    const s = state();
    for (const fn of listeners) {
      try { fn(s); } catch (_) {}
    }
  }

  // --- PC API ---
  async function rawRequest(method, path, body, timeoutMs) {
    const opts = { method, headers: {} };
    if (apiBase) {
      opts.credentials = "omit";
      const t = readToken();
      if (t) opts.headers["Authorization"] = `Bearer ${t}`;
    } else {
      opts.credentials = "same-origin";
    }
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let timer = null;
    if (timeoutMs) {
      const ctrl = new AbortController();
      opts.signal = ctrl.signal;
      timer = setTimeout(() => ctrl.abort(), timeoutMs);
    }
    let res;
    try {
      res = await fetch(apiBase + path, opts);
    } catch (e) {
      throw new NetworkError();
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (res.status === 401) throw new AuthError();
    if (!res.ok) {
      let detail = `요청 실패 (${res.status})`;
      try {
        const data = await res.json();
        if (typeof data.detail === "string") detail = data.detail;
      } catch (_) {}
      throw new HttpError(res.status, detail);
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

  // --- 대기열 (IndexedDB). 못 쓰는 환경이면 메모리에만 둔다 ---
  const IDB = { name: "reelbox", store: "queue" };
  let idb = null;

  function openIdb() {
    if (idb) return idb;
    idb = new Promise((resolve) => {
      try {
        const req = indexedDB.open(IDB.name, 1);
        req.onupgradeneeded = () => req.result.createObjectStore(IDB.store, { keyPath: "seq", autoIncrement: true });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      } catch (_) {
        resolve(null);
      }
    });
    return idb;
  }

  async function idbTx(mode_, fn) {
    const db = await openIdb();
    if (!db) return null;
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(IDB.store, mode_);
        const req = fn(tx.objectStore(IDB.store));
        tx.oncomplete = () => resolve(req ? req.result : null);
        tx.onerror = () => resolve(null);
      } catch (_) {
        resolve(null);
      }
    });
  }

  let memSeq = 1;
  async function loadQueue() {
    const all = await idbTx("readonly", (s) => s.getAll());
    if (all) queue = all.sort((a, b) => a.seq - b.seq);
  }
  async function enqueue(op) {
    const entry = { ...op, at: new Date().toISOString() };
    const seq = await idbTx("readwrite", (s) => s.add(entry));
    entry.seq = seq != null ? seq : memSeq++;
    queue.push(entry);
    notify();
  }
  async function dequeue(seq) {
    await idbTx("readwrite", (s) => s.delete(seq));
    queue = queue.filter((o) => o.seq !== seq);
  }

  function sendOp(op) {
    switch (op.kind) {
      case "bulk": return rawRequest("POST", "/api/items/bulk", { ids: op.ids, action: op.action });
      case "favorite": return rawRequest("PATCH", `/api/items/${op.id}`, { favorite: op.value });
      case "add": return rawRequest("POST", "/api/items", { url: op.url });
      default: return Promise.resolve(null);
    }
  }

  // PC 에 연결돼 있으면 대기열을 순서대로 보낸다. 연결·토큰 문제면 멈추고 다음에 다시.
  async function flushQueue() {
    if (mode !== "pc" || flushing || !queue.length) return;
    flushing = true;
    try {
      for (const op of [...queue]) {
        try {
          await sendOp(op);
        } catch (e) {
          if (e instanceof NetworkError || e instanceof AuthError) break;
          if (e instanceof HttpError && e.status >= 500) break;
          // 4xx: 이미 영구 삭제된 영상 등 → 다시 보내도 안 되므로 버린다
          console.warn("대기열 항목을 버림", op, e.message);
        }
        await dequeue(op.seq);
      }
    } finally {
      flushing = false;
      notify();
    }
  }

  // --- 연결 확인 ---
  async function loadSite() {
    try {
      const res = await fetch("/snapshot/meta.json", { cache: "no-cache" });
      if (!res.ok) return null;
      return await res.json();
    } catch (_) {
      return null;
    }
  }

  async function probe() {
    if (!site) return "pc"; // PC 에서 열린 화면
    if (!site.pc_url) return "snapshot";
    try {
      await rawRequest("GET", "/api/status", undefined, PROBE_MS);
      return "pc";
    } catch (e) {
      return e instanceof AuthError ? "pc" : "snapshot"; // 401 도 PC 가 답한 것
    }
  }

  async function connect() {
    if (site === null) site = await loadSite();
    apiBase = site && site.pc_url ? site.pc_url.replace(/\/+$/, "") : "";
    await loadQueue();
    mode = await probe();
    if (mode === "snapshot") await loadSnapshot();
    notify();
    if (mode === "pc") flushQueue(); // 기다리지 않는다 (화면 먼저)
  }

  function ensure() {
    if (!ready) ready = connect().catch((e) => { ready = null; throw e; });
    return ready;
  }

  // 앱을 다시 열 때 등: 연결을 다시 확인한다. 모드가 바뀌었으면 true.
  async function recheck() {
    const before = mode;
    ready = null;
    await ensure();
    return before !== mode;
  }

  // --- 스냅샷 ---
  async function loadSnapshot() {
    if (snap) return;
    const get = async (name) => {
      const res = await fetch(`/snapshot/${name}`, { cache: "no-cache" });
      if (!res.ok) throw new Error("스냅샷을 불러오지 못했어요");
      return res.json();
    };
    const [items, topics] = await Promise.all([get("items.json"), get("topics.json")]);
    const byId = new Map(items.map((i) => [i.id, i]));
    let mini = null;
    if (window.MiniSearch && window.ReelboxSearchKo) {
      mini = new window.MiniSearch(window.ReelboxSearchKo.MINISEARCH_OPTIONS);
      mini.addAll(items.map((i) => ({ ...i, tags: i.tags.join(" "), key_points: i.key_points.join(" ") })));
    }
    snap = { items, byId, topics, mini };
  }

  // 대기열을 스냅샷 위에 덮어 본다 (화면에 먼저 반영)
  function overlay() {
    const o = new Map();
    const get = (id) => o.get(id) || o.set(id, {}).get(id);
    for (const op of queue) {
      if (op.kind === "bulk") {
        for (const id of op.ids) {
          const s = get(id);
          if (op.action === "hide") s.hidden = true;
          if (op.action === "unhide") s.hidden = false;
          if (op.action === "delete") s.deleted = op.at;
          if (op.action === "restore") s.deleted = null;
        }
      } else if (op.kind === "favorite") {
        get(op.id).favorite = op.value;
      }
    }
    return o;
  }

  function card(item, o) {
    const s = (o && o.get(item.id)) || {};
    const deleted = s.deleted || null;
    return {
      id: item.id,
      platform: item.platform,
      url: item.url,
      title: item.title,
      author: item.author,
      summary: item.summary,
      topic_id: item.topic_id,
      topic: item.topic,
      saved_at: item.saved_at,
      duration_sec: item.duration_sec,
      thumb: item.thumb,
      media_type: item.media_type,
      status: item.status,
      error: null,
      hidden: !!s.hidden,
      favorite: s.favorite !== undefined ? s.favorite : item.favorite,
      deleted_at: deleted,
      purge_at: deleted ? new Date(new Date(deleted).getTime() + TRASH_DAYS * 86400000).toISOString() : null,
    };
  }

  function visibleCards() {
    const o = overlay();
    return snap.items.map((i) => card(i, o)).filter((c) => !c.hidden && !c.deleted_at);
  }

  function byRecent(a, b) {
    return a.saved_at < b.saved_at ? 1 : a.saved_at > b.saved_at ? -1 : b.id - a.id;
  }

  function page(list, limit, offset) {
    const items = list.slice(offset, offset + limit);
    return { items, next_offset: items.length === limit ? offset + limit : null };
  }

  function topicCards(cards) {
    return snap.topics
      .map((t) => {
        const mine = cards.filter((c) => c.topic_id === t.id).sort(byRecent);
        return {
          id: t.id,
          name: t.name,
          description: t.description,
          count: mine.length,
          thumbs: mine.filter((c) => c.thumb).slice(0, 3).map((c) => c.thumb),
        };
      })
      .filter((t) => t.count > 0)
      .sort((a, b) => b.count - a.count);
  }

  const S = {
    home() {
      const cards = visibleCards();
      return { total: cards.length, topics: topicCards(cards), recent: [...cards].sort(byRecent).slice(0, 10) };
    },
    topic(id) {
      const t = topicCards(visibleCards()).find((t) => t.id === id);
      if (!t) throw new Error("주제를 찾을 수 없습니다");
      return { ...t, thumbs: [] };
    },
    items({ topicId, favorite, sort, limit = 30, offset = 0 } = {}) {
      let list = visibleCards();
      if (topicId != null) list = list.filter((c) => c.topic_id === Number(topicId));
      if (favorite) list = list.filter((c) => c.favorite);
      list.sort(byRecent);
      if (sort === "oldest") list.reverse();
      return page(list, limit, offset);
    },
    item(id) {
      const raw = snap.byId.get(id);
      if (!raw) throw new Error("스냅샷에 없는 영상이에요. PC에 연결하면 볼 수 있어요");
      const o = overlay();
      const visible = new Set(visibleCards().map((c) => c.id));
      return {
        ...card(raw, o),
        description: null,
        note: raw.note,
        key_points: raw.key_points,
        tags: raw.tags,
        transcript_source: null,
        transcript: null, // 자막 원문은 스냅샷에 없다
        similar: raw.similar.filter((sid) => visible.has(sid)).map((sid) => card(snap.byId.get(sid), o)),
      };
    },
    search({ q = "", platform, topic, since, until, favorite, limit = 20, offset = 0 } = {}) {
      const cards = visibleCards();
      const byId = new Map(cards.map((c) => [c.id, c]));
      let list = [];
      if (q.trim() && snap.mini) {
        list = snap.mini.search(q).map((r) => byId.get(r.id)).filter(Boolean);
      }
      if (platform) list = list.filter((c) => c.platform === platform);
      if (topic) list = list.filter((c) => String(c.topic_id) === String(topic) || c.topic === topic);
      if (since) list = list.filter((c) => c.saved_at.slice(0, 10) >= since);
      if (until) list = list.filter((c) => c.saved_at.slice(0, 10) <= until);
      if (favorite) list = list.filter((c) => c.favorite);
      return { ...page(list, Number(limit), Number(offset)), semantic: false };
    },
    status() {
      const n = visibleCards().length;
      return {
        counts: { imported: 0, enriched: 0, analyzed: 0, indexed: n, failed: 0, unavailable: 0 },
        total: n,
        hidden: null,
        trash: null,
        failures: [],
      };
    },
    recentAdds() {
      // 대기열에 있는 추가만 보여준다 (처리 상태는 PC 가 켜져야 생긴다)
      return queue
        .filter((op) => op.kind === "add")
        .reverse()
        .map((op) => ({
          id: -op.seq,
          platform: /youtu/.test(op.url) ? "youtube" : "instagram",
          url: op.url,
          title: null,
          summary: null,
          thumb: null,
          status: "queued",
          error: null,
        }));
    },
  };

  const LINK_RE = /https?:\/\/(www\.|m\.)?(instagram\.com|youtube\.com|youtu\.be)\/\S+/i;

  // 스냅샷에서 바꾸기: 대기열에 넣고 바뀐 id 를 돌려준다 (PC API 와 같은 모양)
  const W = {
    async bulk(ids, action) {
      await enqueue({ kind: "bulk", ids, action });
      return { ids };
    },
    async setFavorite(id, value) {
      await enqueue({ kind: "favorite", id, value });
      return { ids: [id] };
    },
    async addUrl(text) {
      const m = String(text).match(LINK_RE);
      if (!m) return { result: "unrecognized", message: "인스타그램 릴스나 유튜브 쇼츠 링크가 아니에요", item: null };
      await enqueue({ kind: "add", url: m[0] });
      return { result: "queued", message: "PC가 꺼져 있어 대기열에 넣었어요. PC에 연결되면 보내요", item: null };
    },
  };

  // PC 모드면 pc(), 스냅샷이면 offline(). PC 가 도중에 꺼지면 스냅샷으로 바꿔서 한 번 더.
  async function call(pc, offline) {
    await ensure();
    if (mode === "snapshot") return offline();
    try {
      return await pc();
    } catch (e) {
      if (e instanceof NetworkError && site) {
        ready = null;
        await ensure();
        if (mode === "snapshot") return offline();
      }
      throw e;
    }
  }
  const pcOnly = (what) => () => {
    throw new OfflineError(what);
  };
  const req = (method, path, body) => () => rawRequest(method, path, body);

  window.ReelboxData = {
    AuthError,
    OfflineError,
    get mode() {
      return mode;
    },
    state,
    // 연결 상태가 바뀔 때 부른다: fn({ mode, site, generatedAt, queued })
    subscribe(fn) {
      listeners.push(fn);
      fn(state());
    },
    ready: ensure,
    recheck,
    // 단축어에 넣을 PC 주소
    pcUrl: () => apiBase || location.origin,

    // 인증. Pages 에서는 토큰을 이 기기에 저장해 헤더로 보내고, PC 에서 열면 쿠키.
    async login(token) {
      await ensure();
      if (!apiBase) return rawRequest("POST", "/api/login", { token });
      writeToken(token);
      try {
        await rawRequest("GET", "/api/status");
      } catch (e) {
        if (e instanceof AuthError) writeToken(null);
        throw e;
      }
      flushQueue();
      return { ok: true };
    },
    async logout() {
      await ensure();
      if (!apiBase) return rawRequest("POST", "/api/logout");
      writeToken(null);
      return { ok: true };
    },

    // 조회
    home: () => call(req("GET", "/api/home"), () => S.home()),
    topic: (id) => call(req("GET", `/api/topics/${id}`), () => S.topic(id)),
    // { topicId, favorite, sort: "recent"|"oldest", limit, offset } → { items, next_offset }
    items: (p = {}) =>
      call(
        req("GET", "/api/items" + query({ topic_id: p.topicId, favorite: p.favorite ? "true" : "", sort: p.sort, limit: p.limit, offset: p.offset })),
        () => S.items(p)
      ),
    item: (id) => call(req("GET", `/api/items/${id}`), () => S.item(id)),
    // { q, platform, topic, since, until, favorite, limit, offset } → { items, next_offset, semantic }
    search: (params) => call(req("GET", "/api/search" + query(params)), () => S.search(params)),
    status: () => call(req("GET", "/api/status"), () => S.status()),
    // 실패한 항목 전체 → [{ id, platform, url, title, error, attempts, retry_requested, updated_at }]
    failedItems: () => call(req("GET", "/api/failed"), pcOnly("실패한 항목 보기")),
    // 주제 고르기용 → [{ id, name, count, fixed }]
    topics: () => call(req("GET", "/api/topics"), pcOnly("주제 바꾸기")),
    hiddenItems: () => call(req("GET", "/api/hidden"), pcOnly("숨긴 항목 보기")),
    trashItems: () => call(req("GET", "/api/trash"), pcOnly("휴지통 보기")),
    recentAdds: () => call(req("GET", "/api/adds"), () => S.recentAdds()),
    // 자동 처리 워커 → { enabled, busy, step, pending, paused, notice }. 스냅샷이면 null
    workerState: () => call(req("GET", "/api/worker"), () => null),

    // 숨기기·삭제·복원. 모두 { ids: [바뀐 id] } 를 돌려준다. 스냅샷이면 대기열로.
    setHidden: (id, hidden) => call(req("PATCH", `/api/items/${id}`, { hidden }), () => W.bulk([id], hidden ? "hide" : "unhide")),
    trash: (id) => call(req("DELETE", `/api/items/${id}`), () => W.bulk([id], "delete")),
    restore: (id) => call(req("POST", `/api/items/${id}/restore`), () => W.bulk([id], "restore")),
    // action: "hide" | "unhide" | "delete" | "restore"
    bulk: (ids, action) => call(req("POST", "/api/items/bulk", { ids, action }), () => W.bulk(ids, action)),
    emptyTrash: () => call(req("POST", "/api/trash/empty"), pcOnly("휴지통 비우기")),

    // 즐겨찾기·메모 (Phase 7). note 가 빈 글자면 메모를 지운다.
    setFavorite: (id, favorite) => call(req("PATCH", `/api/items/${id}`, { favorite }), () => W.setFavorite(id, favorite)),
    setNote: (id, note) => call(req("PATCH", `/api/items/${id}`, { note }), pcOnly("메모 고치기")),

    // 주제 고치기 (Phase 7). 이름 바꾸기·합치기는 남은 주제 { id, name, count } 를 돌려준다.
    renameTopic: (id, name) => call(req("PATCH", `/api/topics/${id}`, { name }), pcOnly("주제 이름 바꾸기")),
    mergeTopic: (id, into) => call(req("POST", `/api/topics/${id}/merge`, { into }), pcOnly("주제 합치기")),
    setItemTopic: (itemId, topicId) => call(req("PUT", `/api/items/${itemId}/topic`, { topic_id: topicId }), pcOnly("주제 바꾸기")),

    // 실패 다시 시도 (Phase 7) → { ids }
    retry: (id) => call(req("POST", `/api/items/${id}/retry`), pcOnly("다시 시도")),
    retryAll: () => call(req("POST", "/api/failed/retry"), pcOnly("다시 시도")),

    // 추가 → { result, message, item }. 스냅샷이면 result "queued"
    addUrl: (url) => call(req("POST", "/api/items", { url }), () => W.addUrl(url)),
  };
})();
