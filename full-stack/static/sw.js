// Nepeta 的 Service Worker。砖 1 只做推送，不做离线缓存。
// 🔴 必须从根路径 /sw.js 提供：放 /static/sw.js 的话 scope 只覆盖 /static/*，
//    控不到主页面，推送根本到不了。路由见 main.py 的 sw_js()。
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

// 主屏图标上那个红点（Badging API）。
// 🔴 角标是装饰，通知才是信号。这一段里任何一步失败都必须静默跳过：
//    Android、桌面、iOS 16.4 以下都没有这个 API，为了一个红点把推送本身
//    弄挂是本末倒置。所以整段包在 try 里，而且是在 showNotification
//    **之后**才跑——通知一定先送到。
// 🔴 数字取「还挂着的通知条数」，不是自己维护一个计数器：
//    Service Worker 随时会被系统杀掉，内存里的计数活不过两条推送。
//    通知本身就是持久的，数它最准，也不用存任何东西。
//    她一回到前台，页面会把这些通知收掉（见 index.html 的
//    clearSeenNotifications），于是下一轮重新从 0 开始算。
async function paintBadge() {
  try {
    if (!self.navigator || !self.navigator.setAppBadge) return;
    let n = 1;
    try { n = (await self.registration.getNotifications()).length || 1; } catch (e) {}
    await self.navigator.setAppBadge(n);
  } catch (e) {}
}

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (err) {}
  event.waitUntil(self.registration.showNotification(data.title || '梁忱', {
    body: data.body || '',
    // 🔴 每条独立 tag，否则新通知会覆盖掉上一条还没看的
    tag: 'nepeta-' + Date.now(),
    renotify: true,
    data: { url: data.url || '/' }
  }).then(paintBadge));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  try { self.navigator && self.navigator.clearAppBadge && self.navigator.clearAppBadge(); } catch (e) {}
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
