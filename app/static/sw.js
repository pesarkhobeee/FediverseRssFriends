/* rssFriends service worker.
 *
 * Deliberately minimal. Two rules:
 *   1. GET /static/*  -> stale-while-revalidate against a versioned cache.
 *      Fast repeat loads; background refresh keeps assets current.
 *   2. Everything else -> passthrough to the network. NO html caching.
 *      HTMX fragments + SSR pages depend on live server state
 *      (bookmarks, subscriptions, follow state). Caching HTML would
 *      show stale content to users who clicked ★ or subscribed and
 *      came back.
 *
 * The cache name is pinned to __VERSION__ (server-injected at fetch
 * time). On activate we sweep every cache that doesn't match — that's
 * the entire garbage collection strategy.
 */

const VERSION = "__VERSION__";
const CACHE = `rssfriends-static-${VERSION}`;

self.addEventListener("install", (event) => {
  // Skip the "wait for the old worker to release control" step so
  // version bumps take effect on the next navigation.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n !== CACHE)
          .map((n) => caches.delete(n))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // /static/* -> stale-while-revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE);
        const cached = await cache.match(req);
        const network = fetch(req)
          .then((res) => {
            // Only cache good, complete responses.
            if (res && res.status === 200 && res.type === "basic") {
              cache.put(req, res.clone());
            }
            return res;
          })
          .catch(() => cached);  // offline: fall back to cache if we have it
        return cached || network;
      })()
    );
    return;
  }

  // Everything else: passthrough. No cache means no stale HTML.
});
