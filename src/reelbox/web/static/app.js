// Reelbox 화면 로직 (Alpine.js). 데이터는 반드시 ReelboxData(data.js)로만 읽고 쓴다.
// 주소는 해시 라우팅: #/ · #/search?q= · #/topic/3 · #/item/5 · #/add · #/favorites
//                    #/settings · #/hidden · #/trash · #/failed

(function () {
  "use strict";

  const D = window.ReelboxData;
  const PAGE = 20;
  const LONG_PRESS_MS = 450;
  const UNDO_MS = 5000;

  const PLATFORM_NAMES = { youtube: "유튜브", instagram: "인스타그램" };
  const PERIODS = [
    { key: "", label: "전체" },
    { key: "7", label: "최근 1주일" },
    { key: "30", label: "최근 1개월" },
    { key: "90", label: "최근 3개월" },
  ];

  function parseHash() {
    const raw = location.hash.replace(/^#/, "") || "/";
    const [path, qs] = raw.split("?");
    const parts = path.split("/").filter(Boolean);
    return { name: parts[0] || "home", id: parts[1] ? Number(parts[1]) : null, params: new URLSearchParams(qs || "") };
  }

  function daysAgoDate(days) {
    const d = new Date(Date.now() - days * 86400000);
    return d.toISOString().slice(0, 10);
  }

  let undoAction = null; // 마지막 알림의 되돌리기 (async 함수)

  function reelbox() {
    return {
      // --- 공통 상태 ---
      route: parseHash(),
      authed: true,
      tokenInput: "",
      loginError: "",
      loading: false,
      error: "",
      // 알림: { text, canUndo }. 되돌리기 함수는 템플릿에 노출하지 않는다 —
      // Alpine 은 식의 결과가 함수면 실행해 버려서, x-show 가 되돌리기를 몰래 부를 수 있다.
      toast: null,
      toastTimer: null,
      menuOpen: false,
      confirmEmpty: false,

      // Phase 7: 주제 고치기·즐겨찾기·메모·실패 다시 시도
      allTopics: [], // 주제 고르기 시트용 { id, name, count, fixed }
      topicSheet: null, // null | "move"(영상 주제 바꾸기) | "merge"(이 주제를 다른 주제로 합치기)
      renameOpen: false,
      renameInput: "",
      renameError: "",
      mergeInto: null, // 합치기 확인 창에 띄울 대상 주제
      noteDraft: "",
      noteSaving: false,
      failures: [],
      retrying: false,

      // 여러 개 선택 (카드를 길게 눌러 시작)
      selecting: false,
      selected: [],
      pressTimer: null,
      pressStart: null,
      suppressClick: false,

      // 화면별 데이터
      home: null,
      topic: null,
      topicItems: [],
      topicSort: "recent",
      topicNext: null,
      favorites: [],
      favoritesNext: null,
      favoritesLoaded: false,
      detail: null,
      transcriptOpen: false,
      hidden: [],
      trash: [],
      status: null,
      adds: [],
      worker: null,
      addTimer: null,
      guideOpen: false,
      copied: false,
      addUrl: "",
      addMessage: null,
      adding: false,
      refreshing: false,

      // 검색
      q: "",
      results: [],
      resultsNext: null,
      semantic: false,
      searched: false,
      filterOpen: false,
      filters: { platform: "", topic: "", period: "", favorite: "" },
      draft: { platform: "", topic: "", period: "", favorite: "" },
      topicsForFilter: [],

      // 연결 상태 (Phase 8): mode "pc" | "snapshot", 스냅샷 시각, 대기열 개수
      conn: { mode: "pc", site: false, generatedAt: null, queued: 0 },

      PERIODS,

      init() {
        D.subscribe((s) => (this.conn = s));
        window.addEventListener("hashchange", () => this.go());
        this.go();
        // 앱을 다시 열 때마다 PC 연결을 확인한다. 바뀌었으면(켜짐↔꺼짐) 지금 화면을 새로 불러온다.
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState !== "visible") return;
          D.recheck().then((changed) => changed && this.go()).catch(() => {});
        });
        // 화면 맨 아래 가까이 오면 더 불러오기 (검색 결과, 주제 상세)
        window.addEventListener("scroll", () => this.nearBottom() && this.loadMore(), { passive: true });
      },

      nearBottom() {
        return window.innerHeight + window.scrollY > document.documentElement.scrollHeight - 600;
      },

      // --- 라우팅 ---
      async go() {
        this.route = parseHash();
        clearTimeout(this.addTimer);
        this.cancelSelect();
        this.menuOpen = false;
        this.filterOpen = false;
        this.topicSheet = null;
        this.renameOpen = false;
        this.mergeInto = null;
        this.error = "";
        window.scrollTo(0, 0);
        const r = this.route;
        try {
          switch (r.name) {
            case "home": await this.loadHome(); break;
            case "search": await this.loadSearchRoute(); break;
            case "topic": await this.loadTopic(r.id); break;
            case "item": await this.loadDetail(r.id); break;
            case "add": await this.loadAdds(); break;
            case "favorites": await this.loadFavorites(); break;
            case "settings": await this.loadStatus(); break;
            case "failed": this.failures = await D.failedItems(); break;
            case "hidden": this.hidden = await D.hiddenItems(); break;
            case "trash": this.trash = await D.trashItems(); break;
          }
          this.authed = true;
        } catch (e) {
          this.handleError(e);
        }
      },

      navigate(hash) {
        if (location.hash === hash) this.go();
        else location.hash = hash;
      },

      back() {
        if (history.length > 1) history.back();
        else this.navigate("#/");
      },

      tab(name) {
        const n = this.route.name;
        if (name === "home") return n === "home" || n === "topic";
        if (name === "settings") return ["settings", "hidden", "trash", "failed"].includes(n);
        return n === name;
      },

      handleError(e) {
        if (e instanceof D.AuthError) {
          this.authed = false;
          return;
        }
        this.error = e.message || String(e);
      },

      async login() {
        this.loginError = "";
        try {
          await D.login(this.tokenInput.trim());
          this.tokenInput = "";
          this.authed = true;
          this.go();
        } catch (e) {
          this.loginError = e instanceof D.AuthError ? "토큰이 맞지 않아요" : e.message;
        }
      },

      async logout() {
        await D.logout().catch(() => {});
        this.authed = false;
      },

      // --- 화면별 불러오기 ---
      async loadHome() {
        this.home = await D.home();
        this.topicsForFilter = this.home.topics;
      },

      async loadTopic(id) {
        this.topic = null;
        this.topicItems = [];
        const [topic, page] = await Promise.all([
          D.topic(id),
          D.items({ topicId: id, sort: this.topicSort, limit: PAGE }),
        ]);
        this.topic = topic;
        this.topicItems = page.items;
        this.topicNext = page.next_offset;
      },

      async loadFavorites() {
        this.favoritesLoaded = false;
        const page = await D.items({ favorite: true, limit: PAGE });
        this.favorites = page.items;
        this.favoritesNext = page.next_offset;
        this.favoritesLoaded = true;
      },

      async changeSort(sort) {
        this.topicSort = sort;
        await this.loadTopic(this.route.id).catch((e) => this.handleError(e));
      },

      async loadDetail(id) {
        this.detail = null;
        this.transcriptOpen = false;
        this.detail = await D.item(id);
        this.noteDraft = this.detail.note || "";
      },

      async loadStatus() {
        this.status = await D.status();
      },

      async loadAdds() {
        const [adds, worker] = await Promise.all([D.recentAdds(), D.workerState()]);
        this.adds = adds;
        this.worker = worker;
        this.scheduleAddsRefresh();
      },

      // 새로고침 버튼: 누른 게 보이게 잠깐이라도 아이콘을 돌리고, 실패하면 오류를 띄운다
      async refreshAdds() {
        if (this.refreshing) return;
        this.refreshing = true;
        this.error = "";
        const started = Date.now();
        try {
          await this.loadAdds();
        } catch (e) {
          this.handleError(e);
        } finally {
          setTimeout(() => (this.refreshing = false), Math.max(0, 500 - (Date.now() - started)));
        }
      },

      // 처리 중인 항목이 있으면 추가 화면을 5초마다 새로고침한다 (다른 화면으로 가면 멈춤)
      scheduleAddsRefresh() {
        clearTimeout(this.addTimer);
        const w = this.worker;
        const working = w && w.enabled && (w.busy || this.adds.some((a) => [1, 2].includes(this.stage(a).step)));
        if (this.route.name !== "add" || !working) return;
        this.addTimer = setTimeout(() => {
          if (this.route.name !== "add") return;
          if (document.visibilityState === "hidden") return this.scheduleAddsRefresh();
          this.loadAdds().catch(() => this.scheduleAddsRefresh());
        }, 5000);
      },

      // 추가 화면 맨 위의 워커 상태 한 줄
      workerLine() {
        const w = this.worker;
        if (!w) return null;
        if (!w.enabled) return { tone: "wait", icon: "pause_circle", text: "자동 처리가 꺼져 있어요" };
        if (w.busy && w.step) return { tone: "work", icon: "autorenew", text: `PC에서 ${w.step} 중이에요` };
        if (w.pending) return { tone: "wait", icon: "hourglass_top", text: `처리를 기다리는 영상 ${w.pending}개` };
        return { tone: "done", icon: "task_alt", text: "공유한 영상을 모두 처리했어요" };
      },

      pauses() {
        const names = { instagram: "인스타그램", youtube: "유튜브", ai: "AI 분석" };
        const w = this.worker;
        if (!w || !w.paused) return [];
        return Object.entries(w.paused).map(([key, p]) => {
          const min = Math.max(1, Math.ceil((new Date(p.until).getTime() - Date.now()) / 60000));
          return { key, title: `${names[key] || key} 처리를 잠시 멈췄어요 · ${min}분 뒤 다시 시도`, reason: p.reason };
        });
      },

      // --- 연결 상태 (상단 바) ---
      snapshotMode() {
        return this.conn.mode === "snapshot";
      },

      connLabel() {
        if (!this.snapshotMode()) return "PC 연결됨";
        const when = this.conn.generatedAt ? this.ago(this.conn.generatedAt) : "";
        return when ? `스냅샷 · 마지막 업데이트 ${when}` : "스냅샷";
      },

      // 스냅샷에서 바꾼 것은 대기열에 들어갔다가 PC 에 연결되면 반영된다
      pendingSuffix() {
        return this.snapshotMode() ? " · PC에 연결되면 반영돼요" : "";
      },

      // --- 설정: 아이폰 단축어 ---
      shortcutUrl() {
        return `${D.pcUrl()}/api/items`;
      },

      async copyShortcutUrl() {
        try {
          await navigator.clipboard.writeText(this.shortcutUrl());
          this.copied = true;
          setTimeout(() => (this.copied = false), 2000);
        } catch (_) {
          this.copied = false;
        }
      },

      // --- 검색 ---
      async loadSearchRoute() {
        const p = this.route.params;
        this.q = p.get("q") || "";
        this.filters = {
          platform: p.get("platform") || "",
          topic: p.get("topic") || "",
          period: p.get("period") || "",
          favorite: p.get("favorite") || "",
        };
        if (!this.topicsForFilter.length) {
          D.home().then((h) => (this.topicsForFilter = h.topics)).catch(() => {});
        }
        this.results = [];
        this.resultsNext = null;
        this.searched = false;
        if (!this.q.trim()) return;
        await this.fetchResults(0);
      },

      searchParams(offset) {
        const f = this.filters;
        return {
          q: this.q.trim(),
          platform: f.platform,
          topic: f.topic,
          since: f.period ? daysAgoDate(Number(f.period)) : "",
          favorite: f.favorite ? "true" : "",
          limit: PAGE,
          offset,
        };
      },

      async fetchResults(offset) {
        this.loading = true;
        try {
          const page = await D.search(this.searchParams(offset));
          this.results = offset === 0 ? page.items : this.results.concat(page.items);
          this.resultsNext = page.next_offset;
          this.semantic = page.semantic;
          this.searched = true;
        } finally {
          this.loading = false;
        }
      },

      submitSearch(q) {
        if (q !== undefined) this.q = q;
        const params = new URLSearchParams();
        if (this.q.trim()) params.set("q", this.q.trim());
        for (const [k, v] of Object.entries(this.filters)) if (v) params.set(k, v);
        this.navigate("#/search" + (params.toString() ? "?" + params : ""));
      },

      openFilter() {
        this.draft = { ...this.filters };
        this.filterOpen = true;
      },

      applyFilter() {
        this.filters = { ...this.draft };
        this.filterOpen = false;
        this.submitSearch();
      },

      clearFilter(key) {
        if (key) this.filters[key] = "";
        else this.filters = { platform: "", topic: "", period: "", favorite: "" };
        this.submitSearch();
      },

      filterChips() {
        const chips = [];
        const f = this.filters;
        if (f.platform) chips.push({ key: "platform", label: PLATFORM_NAMES[f.platform] });
        if (f.topic) {
          const t = this.topicsForFilter.find((t) => String(t.id) === f.topic);
          chips.push({ key: "topic", label: t ? t.name : "주제" });
        }
        if (f.period) chips.push({ key: "period", label: PERIODS.find((p) => p.key === f.period).label });
        if (f.favorite) chips.push({ key: "favorite", label: "즐겨찾기" });
        return chips;
      },

      async loadMore() {
        const n = this.route.name;
        const hasMore =
          (n === "search" && this.resultsNext !== null) ||
          (n === "topic" && this.topicNext !== null) ||
          (n === "favorites" && this.favoritesNext !== null);
        if (this.loading || !hasMore) return;
        try {
          if (n === "search") {
            await this.fetchResults(this.resultsNext);
          } else if (n === "favorites") {
            this.loading = true;
            const page = await D.items({ favorite: true, limit: PAGE, offset: this.favoritesNext });
            this.favorites = this.favorites.concat(page.items);
            this.favoritesNext = page.next_offset;
          } else {
            this.loading = true;
            const page = await D.items({ topicId: this.route.id, sort: this.topicSort, limit: PAGE, offset: this.topicNext });
            this.topicItems = this.topicItems.concat(page.items);
            this.topicNext = page.next_offset;
          }
        } catch (e) {
          this.handleError(e);
        } finally {
          this.loading = false;
        }
        // 화면이 길지 않아 아직 바닥이 보이면 한 번 더
        this.$nextTick(() => this.nearBottom() && this.loadMore());
      },

      // --- 카드: 탭하면 상세, 길게 누르면 선택 모드 ---
      pressDown(item, ev) {
        this.pressStart = { x: ev.clientX, y: ev.clientY };
        clearTimeout(this.pressTimer);
        this.pressTimer = setTimeout(() => {
          this.suppressClick = true;
          if (navigator.vibrate) navigator.vibrate(15);
          if (!this.selecting) this.selecting = true;
          if (!this.selected.includes(item.id)) this.selected.push(item.id);
        }, LONG_PRESS_MS);
      },

      pressMove(ev) {
        if (!this.pressStart) return;
        if (Math.abs(ev.clientX - this.pressStart.x) > 10 || Math.abs(ev.clientY - this.pressStart.y) > 10) {
          this.pressUp();
        }
      },

      pressUp() {
        clearTimeout(this.pressTimer);
        this.pressStart = null;
      },

      tapCard(item) {
        if (this.suppressClick) {
          this.suppressClick = false;
          return;
        }
        if (this.selecting) this.toggleSelect(item.id);
        else this.navigate(`#/item/${item.id}`);
      },

      toggleSelect(id) {
        const i = this.selected.indexOf(id);
        if (i >= 0) this.selected.splice(i, 1);
        else this.selected.push(id);
        if (!this.selected.length) this.selecting = false;
      },

      isSelected(id) {
        return this.selected.includes(id);
      },

      visibleItems() {
        switch (this.route.name) {
          case "home": return this.home ? this.home.recent : [];
          case "search": return this.results;
          case "favorites": return this.favorites;
          case "topic": return this.topicItems;
          case "hidden": return this.hidden;
          case "trash": return this.trash;
          default: return [];
        }
      },

      selectAll() {
        const ids = this.visibleItems().map((i) => i.id);
        this.selected = this.selected.length === ids.length ? [] : ids;
        if (!this.selected.length) this.selecting = false;
      },

      cancelSelect() {
        this.selecting = false;
        this.selected = [];
      },

      // --- 숨기기·삭제·복원 + 되돌리기 알림 ---
      showToast(text, undo) {
        clearTimeout(this.toastTimer);
        undoAction = undo || null;
        this.toast = { text, canUndo: !!undo };
        this.toastTimer = setTimeout(() => (this.toast = null), UNDO_MS);
      },

      async runUndo() {
        const undo = undoAction;
        undoAction = null;
        this.toast = null;
        clearTimeout(this.toastTimer);
        if (!undo) return;
        try {
          await undo();
          this.showToast("되돌렸어요", null);
          await this.go();
        } catch (e) {
          this.handleError(e);
        }
      },

      // action: hide | unhide | delete | restore. 목록에서 바로 빼고, 되돌리기를 띄운다.
      async act(ids, action) {
        if (!ids.length) return;
        const reverse = { hide: "unhide", unhide: "hide", delete: "restore", restore: "delete" }[action];
        const labels = {
          hide: "숨겼어요",
          unhide: "숨김을 풀었어요",
          delete: "휴지통으로 옮겼어요",
          restore: "복원했어요",
        };
        try {
          const res = await D.bulk(ids, action);
          const changed = res.ids;
          this.cancelSelect();
          const n = changed.length;
          const text = (n > 1 ? `${n}개를 ${labels[action]}` : labels[action]) + this.pendingSuffix();
          this.showToast(text, () => D.bulk(changed, reverse));
          this.removeFromLists(changed, action);
        } catch (e) {
          this.handleError(e);
        }
      },

      removeFromLists(ids, action) {
        const drop = (list) => list.filter((i) => !ids.includes(i.id));
        if (this.home) this.home.recent = drop(this.home.recent);
        this.results = drop(this.results);
        this.topicItems = drop(this.topicItems);
        this.favorites = drop(this.favorites);
        if (action === "unhide" || action === "delete") this.hidden = drop(this.hidden);
        if (action === "restore" || action === "delete") this.trash = drop(this.trash);
        if (this.route.name === "item") this.loadDetail(this.route.id).catch((e) => this.handleError(e));
        if (this.route.name === "home") this.loadHome().catch(() => {});
      },

      async doEmptyTrash() {
        this.confirmEmpty = false;
        try {
          const res = await D.emptyTrash();
          this.trash = [];
          this.showToast(`${res.ids.length}개를 영구 삭제했어요`, null);
        } catch (e) {
          this.handleError(e);
        }
      },

      // --- 즐겨찾기·메모 (영상 상세) ---
      async toggleFavorite() {
        const d = this.detail;
        if (!d) return;
        const next = !d.favorite;
        d.favorite = next; // 먼저 화면에 반영하고, 실패하면 되돌린다
        try {
          await D.setFavorite(d.id, next);
          this.showToast((next ? "즐겨찾기에 넣었어요" : "즐겨찾기에서 뺐어요") + this.pendingSuffix(), null);
        } catch (e) {
          d.favorite = !next;
          this.handleError(e);
        }
      },

      noteChanged() {
        return this.detail && this.noteDraft.trim() !== (this.detail.note || "");
      },

      async saveNote() {
        const d = this.detail;
        if (!d || this.noteSaving || !this.noteChanged()) return;
        this.noteSaving = true;
        try {
          await D.setNote(d.id, this.noteDraft);
          d.note = this.noteDraft.trim() || null;
          this.noteDraft = d.note || "";
          this.showToast(d.note ? "메모를 저장했어요" : "메모를 지웠어요", null);
        } catch (e) {
          this.handleError(e);
        } finally {
          this.noteSaving = false;
        }
      },

      // --- 주제 고르기 시트 (영상 주제 바꾸기 · 주제 합치기) ---
      async openTopicSheet(mode) {
        this.menuOpen = false;
        try {
          this.allTopics = await D.topics();
          this.topicSheet = mode;
        } catch (e) {
          this.handleError(e);
        }
      },

      // 시트에 보여줄 주제: 지금 주제는 뺀다
      sheetTopics() {
        const current = this.topicSheet === "move" ? this.detail && this.detail.topic_id : this.topic && this.topic.id;
        return this.allTopics.filter((t) => t.id !== current);
      },

      async pickTopic(t) {
        if (this.topicSheet === "merge") {
          this.topicSheet = null;
          this.mergeInto = t; // 확인 창을 한 번 띄운다
          return;
        }
        const d = this.detail;
        this.topicSheet = null;
        try {
          await D.setItemTopic(d.id, t.id);
          d.topic_id = t.id;
          d.topic = t.name;
          this.showToast(`'${t.name}' 주제로 옮겼어요`, null);
        } catch (e) {
          this.handleError(e);
        }
      },

      // --- 주제 이름 바꾸기·합치기 (주제 상세) ---
      openRename() {
        this.menuOpen = false;
        this.renameInput = this.topic ? this.topic.name : "";
        this.renameError = "";
        this.renameOpen = true;
      },

      async saveRename() {
        const name = this.renameInput.trim();
        if (!name) {
          this.renameError = "주제 이름을 적어 주세요";
          return;
        }
        try {
          const t = await D.renameTopic(this.topic.id, name);
          this.topic.name = t.name;
          this.renameOpen = false;
          this.showToast("주제 이름을 바꿨어요", null);
        } catch (e) {
          if (e instanceof D.AuthError) return this.handleError(e);
          this.renameError = e.message;
        }
      },

      async doMerge() {
        const into = this.mergeInto;
        this.mergeInto = null;
        if (!into || !this.topic) return;
        try {
          const t = await D.mergeTopic(this.topic.id, into.id);
          this.showToast(`'${t.name}' 주제로 합쳤어요`, null);
          // 합친 주제는 없어졌으니 남은 주제 화면으로 (뒤로 가기로 사라진 주제에 오지 않게 replace)
          history.replaceState(null, "", `#/topic/${t.id}`);
          await this.go();
        } catch (e) {
          this.handleError(e);
        }
      },

      // --- 실패한 항목 다시 시도 ---
      async retryOne(f) {
        try {
          await D.retry(f.id);
          f.retry_requested = true;
          f.attempts = 0;
          this.showToast("다시 시도할게요. PC가 곧 처리해요", null);
        } catch (e) {
          this.handleError(e);
        }
      },

      async retryAll() {
        if (this.retrying) return;
        this.retrying = true;
        try {
          const res = await D.retryAll();
          this.failures = await D.failedItems();
          this.showToast(`${res.ids.length}개를 다시 시도할게요`, null);
        } catch (e) {
          this.handleError(e);
        } finally {
          this.retrying = false;
        }
      },

      // --- 추가 ---
      async pasteFromClipboard() {
        try {
          this.addUrl = (await navigator.clipboard.readText()).trim();
        } catch (_) {
          this.addMessage = { ok: false, text: "클립보드를 읽을 수 없어요. 직접 붙여넣어 주세요" };
        }
      },

      async submitAdd() {
        if (!this.addUrl.trim() || this.adding) return;
        this.adding = true;
        this.addMessage = null;
        try {
          const res = await D.addUrl(this.addUrl.trim());
          this.addMessage = { ok: ["added", "restored", "duplicate", "queued"].includes(res.result), text: res.message };
          if (["added", "restored", "queued"].includes(res.result)) this.addUrl = "";
          await this.loadAdds();
        } catch (e) {
          this.handleError(e);
        } finally {
          this.adding = false;
        }
      },

      // 처리 상태: 대기 → 분석 중 → 완료 (DESIGN.md 6장 "추가")
      stage(item) {
        switch (item.status) {
          case "imported": return { step: 1, label: "대기 중", tone: "wait" };
          case "enriched":
          case "analyzed": return { step: 2, label: "분석 중", tone: "work" };
          case "indexed": return { step: 3, label: "완료", tone: "done" };
          case "unavailable": return { step: 0, label: "볼 수 없는 영상", tone: "fail" };
          case "queued": return { step: 0, label: "PC 연결 기다리는 중", tone: "wait" }; // 스냅샷 대기열
          default: return { step: 0, label: "처리 실패", tone: "fail" };
        }
      },

      // --- 표시 도우미 ---
      platformName(p) {
        return PLATFORM_NAMES[p] || p;
      },

      openLabel(p) {
        return `${PLATFORM_NAMES[p] || "원본"}에서 보기`;
      },

      title(item) {
        return item.title || (item.summary ? item.summary.slice(0, 40) : item.url);
      },

      duration(sec) {
        if (!sec && sec !== 0) return "";
        const s = Math.round(sec);
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      },

      ago(iso) {
        if (!iso) return "";
        const diff = (Date.now() - new Date(iso).getTime()) / 1000;
        if (diff < 60) return "방금 전";
        if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
        const days = Math.floor(diff / 86400);
        if (days === 1) return "어제";
        if (days < 7) return `${days}일 전`;
        if (days < 30) return `${Math.floor(days / 7)}주 전`;
        if (days < 365) return `${Math.floor(days / 30)}개월 전`;
        return `${Math.floor(days / 365)}년 전`;
      },

      date(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
      },

      daysLeft(item) {
        if (!item.purge_at) return null;
        return Math.max(0, Math.ceil((new Date(item.purge_at).getTime() - Date.now()) / 86400000));
      },

      pending(s) {
        return s ? s.counts.imported + s.counts.enriched + s.counts.analyzed : 0;
      },

      pct(s, n) {
        return s && s.total ? `${(n / s.total) * 100}%` : "0%";
      },
    };
  }

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("reelbox", reelbox);
  });

  // 서비스 워커 (PWA). HTTPS 또는 localhost 에서만 동작한다.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
