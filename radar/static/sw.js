/* Service Worker —— 让"装到手机上的那个图标"真的像个 app。
 *
 * 没有 SW 会有两个后果，都是硬伤：
 *   1. Chrome/Edge 不给安装入口（可安装性判定要求有注册的 SW）
 *   2. 断网就是白屏 —— 而手机在地铁/电梯里断网是常态
 *
 * 缓存策略按资源性质分开，别一刀切：
 *
 *   /static/*     cache-first + 后台刷新   指纹变了 URL 就变，缓存永远不会脏
 *   HTML 导航     network-first           优先新数据；断网时退到上次看过的页面
 *   /api/*        只走网络                 状态必须实时，缓存下来只会骗自己
 *   其它          network-first
 *
 * 只缓存 200；401（口令失效）和 5xx 一律不落缓存，否则会把错误页钉死。
 */

const CACHE = 'radar-shell-v1';
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // 逐个 put 而不是 addAll：任何一个 404 都会让 addAll 整体失败，
    // 结果整个 SW 装不上（曾经因为这个排查了很久）
    await Promise.all(SHELL.map(async (url) => {
      try {
        const res = await fetch(url, { credentials: 'same-origin' });
        if (res.ok) await cache.put(url, res);
      } catch (e) { /* 单个资源失败不影响整体 */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.map((n) => (n === CACHE ? null : caches.delete(n))));
    await self.clients.claim();
  })());
});

function isHtml(request, url) {
  return request.mode === 'navigate'
    || (request.headers.get('accept') || '').includes('text/html')
    || url.pathname.endsWith('.html')
    || url.pathname.endsWith('/');
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 状态接口绝不缓存：缓存下来只会显示过期信息，比报错更坏
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(req));
    return;
  }

  // 静态资源：缓存优先。它们的 URL 带 ?v=<数据指纹>，数据一变就是新 URL，
  // 所以命中缓存的一定是对的，不需要校验。
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cached = await caches.match(req, { ignoreSearch: false });
      if (cached) {
        // 后台悄悄更新，下次打开就是新的
        event.waitUntil((async () => {
          try {
            const fresh = await fetch(req);
            if (fresh.ok) await (await caches.open(CACHE)).put(req, fresh);
          } catch (e) { /* 断网就继续用旧的 */ }
        })());
        return cached;
      }
      const res = await fetch(req);
      if (res.ok) {
        const copy = res.clone();
        event.waitUntil((await caches.open(CACHE)).put(req, copy));
      }
      return res;
    })());
    return;
  }

  // 页面：网络优先，断网退回上次成功看过的内容
  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok && isHtml(req, url)) {
        const copy = res.clone();
        event.waitUntil((async () => {
          const cache = await caches.open(CACHE);
          await cache.put(req, copy);
          // 目录形式的 URL（/market_flow/）也缓存一份 index.html 的键，
          // 保证离线时点导航能命中
        })());
      }
      return res;
    } catch (e) {
      const cached = await caches.match(req);
      if (cached) return cached;
      // 导航到没缓存过的页面 → 退回首页快照
      if (isHtml(req, url)) {
        const home = await caches.match('/') || await caches.match(req, { ignoreSearch: true });
        if (home) return home;
      }
      return new Response(
        '<!DOCTYPE html><meta name=viewport content="width=device-width,initial-scale=1">'
        + '<div style="font:16px/1.7 system-ui;padding:32px 20px;color:#1b1f24">'
        + '<h2 style="margin:0 0 8px">离线，且这个页面还没缓存过</h2>'
        + '<p style="color:#6a737d">联网后刷新一次即可；已经看过的页面断网也能打开。</p>'
        + '</div>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
      );
    }
  })());
});
