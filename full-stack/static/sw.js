// Nepeta 的 Service Worker。砖 1 只做推送，不做离线缓存。
// 🔴 必须从根路径 /sw.js 提供：放 /static/sw.js 的话 scope 只覆盖 /static/*，
//    控不到主页面，推送根本到不了。路由见 main.py 的 sw_js()。
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (err) {}
  event.waitUntil(self.registration.showNotification(data.title || '梁忱', {
    body: data.body || '',
    // 🔴 每条独立 tag，否则新通知会覆盖掉上一条还没看的
    tag: 'nepeta-' + Date.now(),
    renotify: true,
    data: { url: data.url || '/' }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.registration.scope)) return c.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
