const CACHE_NAME = "smarttask-v3";
const API_CACHE = "smarttask-api-v2";
const NOTIFIED_TASKS = "smarttask-notified-v2";

const STATIC_ASSETS = [
    "/",
    "/static/offline.html",
    "/static/js/chart.js",
    "/static/js/confetti.js",
    "/static/js/socket.io.min.js",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png"
];

// INSTALL (SAFE CACHE)
self.addEventListener("install", event => {
    self.skipWaiting();

    event.waitUntil(
        caches.open(CACHE_NAME).then(async (cache) => {

            for (const asset of STATIC_ASSETS) {
                try {
                    await cache.add(asset);
                } catch (err) {
                    console.warn("❌ Failed to cache:", asset);
                }
            }

            console.log("✅ Service Worker installed (safe caching)");

        })
    );
});

// ACTIVATE
self.addEventListener("activate", event => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys
                    .filter(key => ![CACHE_NAME, API_CACHE, NOTIFIED_TASKS].includes(key))
                    .map(key => caches.delete(key))
            );
        }).then(() => self.clients.claim())
    );
});

// PUSH NOTIFICATION
self.addEventListener("push", event => {
    event.waitUntil((async () => {
        try {
            const data = event.data ? event.data.json() : { title: "You have a new task!", id: null, overdue: false };
            const options = {
                body: data.title + (data.overdue ? " ⚠️ Overdue!" : ""),
                icon: data.overdue ? "/static/icons/icon-512-red.png" : "/static/icons/icon-192.png",
                badge: data.overdue ? "/static/icons/icon-512-red.png" : "/static/icons/icon-192.png",
                vibrate: [200, 100, 200],
                data: { url: "/" },
                tag: `task-${data.id || Math.random()}`
            };
            await self.registration.showNotification("⏰ SmartTask Reminder", options);
        } catch (err) {
            console.error("Push error:", err);
        }
    })());
});

// NOTIFICATION CLICK
self.addEventListener("notificationclick", event => {
    event.notification.close();
    event.waitUntil((async () => {
        const allClients = await clients.matchAll({ includeUncontrolled: true });
        const appClient = allClients.find(c => c.url.includes("/") && "focus" in c);
        if (appClient) appClient.focus();
        else await clients.openWindow("/");
    })());
});

// BACKGROUND SYNC
self.addEventListener("sync", event => {
    if(event.tag === "check-tasks") event.waitUntil(updateTasks());
});

// REAL-TIME TASK UPDATES
self.addEventListener("message", event => {
    if(event.data && event.data.type === "TASK_UPDATE") {
        updateTasks();
    }
});

// FETCH (SMART CACHING + SAFE FALLBACK)
self.addEventListener("fetch", event => {
    if (event.request.method !== "GET") return;

    const url = new URL(event.request.url);

    // ----------------------------
    // API: NETWORK FIRST
    // ----------------------------
    if (url.pathname === "/api/tasks") {
        event.respondWith(
            fetch(event.request)
                .then(networkResp => {
                    // Only cache valid responses
                    if (networkResp && networkResp.status === 200) {
                        const clone = networkResp.clone();
                        caches.open(API_CACHE).then(cache => cache.put(event.request, clone));
                    }
                    return networkResp;
                })
                .catch(async () => {
                    const cached = await caches.match(event.request);
                    return cached || new Response(JSON.stringify({
                        tasks: [],
                        offline: true
                    }), {
                        headers: { "Content-Type": "application/json" }
                    });
                })
        );
        return;
    }

    // ----------------------------
    // STATIC: CACHE FIRST
    // ----------------------------
    event.respondWith(
        caches.match(event.request).then(cachedResp => {
            if (cachedResp) return cachedResp;

            return fetch(event.request)
                .then(networkResp => {
                    // Avoid caching bad responses
                    if (
                        !networkResp ||
                        networkResp.status !== 200 ||
                        networkResp.type === "opaque"
                    ) {
                        return networkResp;
                    }

                    const clone = networkResp.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));

                    return networkResp;
                })
                .catch(() => {
                    // Offline fallback for pages
                    if (event.request.mode === "navigate") {
                        return caches.match("/offline.html");
                    }
                });
        })
    );
});

// TASKS MANAGEMENT & BADGE
async function updateTasks(){
    try {
        const res = await fetch("/api/tasks");
        if(!res.ok) return;

        const data = await res.json();

        // cache API response
        const apiCache = await caches.open(API_CACHE);
        apiCache.put("/api/tasks", new Response(JSON.stringify(data)));

        // load notified tasks
        const notifiedCache = await caches.open(NOTIFIED_TASKS);
        const notifiedResp = await notifiedCache.match("tasks");
        let notifiedIds = notifiedResp ? await notifiedResp.json() : [];

        const currentIds = data.tasks.map(t => t.id);
        notifiedIds = notifiedIds.filter(id => currentIds.includes(id));

        const newNotifiedIds = [];
        let hasOverdue = false;

        for(const task of data.tasks){
            const taskId = task.id;
            const deadlineDate = task.deadline ? new Date(task.deadline + "T23:59:59") : null;
            const isOverdue = deadlineDate && deadlineDate < new Date();

            if(isOverdue) hasOverdue = true;

            if(!notifiedIds.includes(taskId)){
                await self.registration.showNotification("⏰ SmartTask Reminder", {
                    body: task.task + (isOverdue ? " ⚠️ Overdue!" : ""),
                    icon: isOverdue ? "/static/icons/icon-512-red.png" : "/static/icons/icon-192.png",
                    badge: isOverdue ? "/static/icons/icon-512-red.png" : "/static/icons/icon-192.png",
                    vibrate: [200, 100, 200],
                    data: { url: "/" },
                    tag: `task-${taskId}`
                });
            }

            if(isOverdue) newNotifiedIds.push(taskId);
        }

        // update notified tasks cache
        await notifiedCache.put("tasks", new Response(JSON.stringify(newNotifiedIds)));

        // update app badge
        if("setAppBadge" in navigator){
            try {
                if(hasOverdue) await navigator.setAppBadge(newNotifiedIds.length);
                else await navigator.clearAppBadge();
            } catch(e){ console.warn("Badge update failed:", e); }
        }

    } catch(err){
        console.error("Error updating tasks:", err);
    }
}