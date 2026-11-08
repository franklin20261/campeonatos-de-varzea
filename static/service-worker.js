const CACHE_NAME = "campeonatos-varzea-v1";

const ARQUIVOS = [
    "/",
    "/static/style.css",
    "/static/manifest.json"
];

self.addEventListener("install", event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(ARQUIVOS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(key => key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener("fetch", event => {
    const request = event.request;

    if (request.method !== "GET") return;

    if (request.mode === "navigate") {
        event.respondWith(
            fetch(request)
                .then(response => {
                    if (response.ok && new URL(request.url).pathname === "/") {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then(cache =>
                            cache.put("/", copy)
                        );
                    }
                    return response;
                })
                .catch(() =>
                    caches.match(request).then(response =>
                        response || caches.match("/")
                    )
                )
        );
        return;
    }

    if (new URL(request.url).origin === self.location.origin) {
        event.respondWith(
            fetch(request).catch(() => caches.match(request))
        );
    }
});