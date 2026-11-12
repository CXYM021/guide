/* 熟练度 · 修行录 — 只缓存应用外壳，绝不缓存任何 /api/ 响应。
 * 原因：API 返回的是「当前登录用户」的数据，按 URL 缓存会让不同账号串数据。
 * Service Worker 只在 HTTPS 或 localhost 下生效；局域网 http://192.168.x.x 不会注册。
 */
var CACHE = 'proficiency-shell-v1';
var SHELL = ['/', '/index.html', '/manifest.webmanifest',
             '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') { return; }
  var url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) { return; }
  if (url.pathname.indexOf('/api/') === 0 || url.pathname === '/healthz') {
    return; // 一律走网络，不进缓存
  }
  event.respondWith(
    caches.match(req).then(function (hit) {
      if (hit) { return hit; }
      return fetch(req).then(function (res) {
        if (res.ok && SHELL.indexOf(url.pathname) >= 0) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { return c.put(req, copy); });
        }
        return res;
      });
    })
  );
});
