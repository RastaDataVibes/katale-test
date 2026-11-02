// Katale service worker
//  - lets the app install on phones
//  - shows a friendly page when there is no internet
//  - shows order notifications (pop-ups), even when Katale is closed
const CACHE = 'katale-v2';
const OFFLINE = '/offline';

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll([OFFLINE, '/static/img/gc-logo.svg', '/static/css/style.css'])));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
  }
});

// A notification arrived from Katale
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data.json(); } catch (err) { d = { title: 'Katale', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Katale', {
    body: d.body || '',
    icon: '/static/img/gc-192.png',
    badge: '/static/img/gc-192.png',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    requireInteraction: /New order/.test(d.title || ''),   // new orders stay on screen until tapped
    vibrate: [200, 100, 200, 100, 300],
    data: { url: d.url || '/' },
  }));
});

// Tapping the notification opens (or brings back) the right Katale page
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = e.notification.data.url || '/';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) { if (c.url.indexOf(self.location.origin) === 0 && 'focus' in c) { c.navigate(url); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
