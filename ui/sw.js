/* 熟练度 · 修行录 — 只缓存应用外壳，绝不缓存任何 /api/ 响应。
 * 原因：API 返回的是「当前登录用户」的数据，按 URL 缓存会让不同账号串数据。
 *
 * 页面外壳走「网络优先、离线回退」：升级后哪怕 Service Worker 还是旧的，
 * 下一次打开也会先拿服务器上的新页面，不会把旧版本钉死在缓存里。
 * 图标与 manifest 走缓存优先（它们不随版本变化）。
 *
 * 缓存名由 build_ui.py 与 server.py 的 PRODUCT_VERSION 同步：
 * 版本一变，旧的壳缓存会在 activate 时整体删掉。
 *
 * Service Worker 只在 HTTPS 或 localhost 下生效；局域网 http://192.168.x.x 不会注册。
 */
var CACHE = 'proficiency-shell-1.0.7';
var SHELL = ['/', '/index.html'];
var ASSETS = ['/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (c) { return c.addAll(ASSETS).catch(function () { /* 单个资源缺失不该卡住安装 */ }); })
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

function offlineFallback() {
  return caches.match('/').then(function (hit) {
    if (hit) { return hit; }
    return new Response('离线，且没有可用的缓存副本。恢复网络后刷新。', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' }
    });
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') { return; }
  var url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) { return; }
  if (url.pathname.indexOf('/api/') === 0 || url.pathname === '/healthz') {
    return; // 一律走网络，不进缓存
  }

  var isShell = req.mode === 'navigate' || SHELL.indexOf(url.pathname) >= 0;
  if (isShell) {
    event.respondWith(
      fetch(req).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { return c.put(req, copy); })
            .catch(function () { /* 存不下也不影响这次返回 */ });
        }
        return res;
      }).catch(offlineFallback)
    );
    return;
  }

  if (ASSETS.indexOf(url.pathname) >= 0) {
    event.respondWith(
      caches.match(req).then(function (hit) {
        if (hit) { return hit; }
        return fetch(req).then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE).then(function (c) { return c.put(req, copy); }).catch(function () {});
          }
          return res;
        });
      })
    );
  }
});
