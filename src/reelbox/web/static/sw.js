// Reelbox 서비스 워커 (간단 버전).
// - 화면 파일(HTML·JS·아이콘): 네트워크 먼저, 안 되면 저장해 둔 것 → 고친 화면이 바로 반영된다
// - CDN(Tailwind·Alpine·글꼴): 저장해 둔 것 먼저 → 빠르게 뜬다
// - /api, /thumbs: 손대지 않는다 (항상 PC 에서 최신 데이터. 오프라인 대기열은 Phase 8)
const VERSION = "reelbox-v2"; // 올리면 옛 캐시를 지운다
const SHELL = ["/", "/index.html", "/app.js", "/data.js", "/manifest.json", "/icons/icon-192.png"];
const CDN_HOSTS = ["cdn.tailwindcss.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    // cache: "no-cache": 브라우저 HTTP 캐시의 옛 파일을 쓰지 말고 서버에 확인 (Safari 대비)
    const res = await fetch(request, { cache: "no-cache" });
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch (e) {
    const hit = await cache.match(request, { ignoreSearch: true });
    if (hit) return hit;
    throw e;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok || res.type === "opaque") cache.put(request, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/thumbs/")) return;
    event.respondWith(networkFirst(req));
  } else if (CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(req));
  }
});
