/* ============================================================================
   个人情报台 · 前端交互
   ----------------------------------------------------------------------------
   设计原则：
     1. 内容层（轮询 / 刷新 / PWA）—— 让数据"活着"
     2. 交互层（手势 / 长按菜单 / count-up / 时间问候 / swipe）—— 让页面"像 App"
     3. 触感反馈优先；尊重 prefers-reduced-motion
   ============================================================================ */

(function () {
  "use strict";

  /* ============================================================ 常量 */
  var PAGE_FP      = document.body.dataset.fingerprint || "";
  var POLL_IDLE    = 60 * 1000;
  var POLL_ACTIVE  = 3 * 1000;
  var LAST_SEEN_KEY = "radar.lastSeenFp";
  var COLLAPSED_KEY = "radar.collapsed";
  var INSTALL_DISMISS_KEY = "radar.installDismissed";
  var MUTED_KEY     = "radar.muted";
  var PINNED_KEY    = "radar.pinned";

  var fresh       = document.getElementById("freshness");
  var toastEl     = document.getElementById("toast");
  var btn         = document.getElementById("refresh-btn");
  var installBtn  = document.getElementById("install-btn");
  var pullEl      = document.getElementById("pull-indicator");
  var fab         = document.getElementById("fab");

  var timer = null;
  var polling = false;
  var refreshRequested = false;

  /* ============================================================ 工具 */

  function toast(msg, ms) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.hidden = true; }, ms || 2200);
  }

  function relTime(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60)    return s + " 秒前";
    if (s < 3600)  return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }

  function vibrate(ms) {
    try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) {}
  }

  function copyText(text, msg) {
    msg = msg || "已复制";
    var onOk = function () { toast(msg); vibrate(10); };
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(onOk, function () { fallbackCopy(text, onOk); });
    } else {
      fallbackCopy(text, onOk);
    }
  }
  function fallbackCopy(text, cb) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try { document.execCommand("copy"); cb(); }
    catch (e) { toast("复制失败"); }
    document.body.removeChild(ta);
  }

  /* ============================================================ 时间相关 */

  function timeOfDayGreeting() {
    /* 一日三种问候 + emoji —— emoji 用 inline svg 是过度设计 */
    var h = new Date().getHours();
    if (h < 5)  return "夜深了";
    if (h < 11) return "早上好";
    if (h < 14) return "中午好";
    if (h < 18) return "下午好";
    if (h < 22) return "晚上好";
    return "夜深了";
  }

  function formatToday(date) {
    var d = date || new Date();
    var weekday = ["日","一","二","三","四","五","六"][d.getDay()];
    return (d.getMonth() + 1) + " 月 " + d.getDate() + " 日 · 星期" + weekday;
  }

  function updateGreeting() {
    var el = document.getElementById("greeting-text");
    var sub = document.getElementById("greeting-sub");
    if (el) el.textContent = timeOfDayGreeting();
    if (sub) sub.innerHTML = formatToday() + ' · <strong id="freshness-2">—</strong>';
    /* 把数据新鲜度同步到这里 —— 顶栏 + 问候两处都显示 */
    var cellInGreet = document.getElementById("freshness-2");
    var cellInTop   = document.getElementById("freshness");
    if (cellInTop && cellInGreet) {
      cellInGreet.textContent = cellInTop.textContent;
    }
  }

  /* ============================================================ 状态查询 */

  function applyStatus(st) {
    if (!st) return;
    if (fresh && st.latest) {
      var parts = [];
      if (st.latest) parts.push("数据 " + st.latest);
      if (st.last_run && st.last_run.finished_at) {
        var rel = relTime(st.last_run.finished_at);
        if (rel) parts.push(rel);
      }
      var text = parts.join(" · ");
      fresh.textContent = text;
      var cell = document.getElementById("freshness-2");
      if (cell) cell.textContent = text;
      /* brand.dot 缩放给反馈 */
      var dot = document.querySelector(".brand");
      if (dot && st.last_run && st.last_run.failed) {
        dot.classList.add("fresh");
      }
    }
    /* 把"今日已更新/待更新"主题色反映回首页卡 */
    var updatedToday = st.latest && new Date(st.latest).toDateString() === new Date().toDateString();
    document.querySelectorAll(".mod-card[data-fresh]").forEach(function (el) {
      var d = el.dataset.fresh;
      if (d === new Date().toISOString().slice(0,10)) el.classList.add("fresh");
    });
  }

  function fetchStatus() {
    return fetch("/api/status", { headers: { "Accept": "application/json" },
                                  cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function tick() {
    if (document.hidden) { schedule(POLL_IDLE); return; }
    fetchStatus().then(function (st) {
      applyStatus(st);
      if (st && st.fingerprint && st.fingerprint !== PAGE_FP) {
        if (st.busy && !refreshRequested) {
          schedule(POLL_ACTIVE); return;
        }
        toast("检测到新数据，正在刷新…", 1800);
        setTimeout(function () { location.reload(); }, 900);
        return;
      }
      if (polling) {
        if (st && st.busy) { schedule(POLL_ACTIVE); return; }
        polling = false;
        refreshing(false);
        document.dispatchEvent(new CustomEvent("radar:refreshEnd"));
        toast(st && st.last_run && st.last_run.failed ? "跑完了，但部分模块失败" : "已是最新");
        schedule(POLL_IDLE);
        return;
      }
      schedule(POLL_IDLE);
    }).catch(function () {
      if (fresh) fresh.textContent = "离线";
      document.dispatchEvent(new CustomEvent("radar:refreshEnd"));
      schedule(POLL_IDLE * 2);
    });
  }

  /* ============================================================ 主动刷新 */

  function refreshing(on) {
    if (btn) { btn.classList.toggle("busy", !!on); btn.disabled = !!on; }
    if (fab) { fab.classList.toggle("busy", !!on); fab.disabled = !!on; }
  }

  function hidePullIndicator() {
    if (pullEl) pullEl.classList.remove("pull-show", "pull-ready", "pull-spin");
  }

  function requestRefresh() {
    if (polling) return;
    polling = true;
    refreshRequested = true;
    refreshing(true);
    if (pullEl) {
      pullEl.classList.add("pull-show", "pull-spin");
      pullEl.classList.remove("pull-ready");
    }
    toast("正在拉取最新数据…", 1800);

    fetch("/api/refresh", { method: "POST",
                            headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error((res.j && res.j.error) || ("HTTP " + res.ok));
        schedule(POLL_ACTIVE);
        vibrate(15);
      })
      .catch(function (e) {
        polling = false;
        refreshRequested = false;
        refreshing(false);
        hidePullIndicator();
        toast("触发失败：" + (e.message || e), 3200);
        schedule(POLL_IDLE);
      });
  }

  /* ============================================================ 下拉刷新 v2
     关键改善（v1 → v2）：
       · 阈值上调到 96px（手感更稳）
       · 拖拽时给容器加 transform —— 视觉同步
       · 手指抖动容差提到 12px（v1 的 10 在快速滑动时容易误判） */

  function setupPullToRefresh() {
    if (!pullEl) return;
    var startY = 0, startX = 0, pulling = false, armed = false;
    var THRESHOLD = 96, MOVE_TOL = 12;
    document.addEventListener("touchstart", function (e) {
      if (window.scrollY > 4 || document.hidden || polling) return;
      /* 不要在横向滑动容器里触发 */
      if (e.target.closest(".table-wrap, .tabs, .chips")) return;
      startY = e.touches[0].clientY;
      startX = e.touches[0].clientX;
      pulling = true; armed = false;
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
      if (!pulling) return;
      var t = e.touches[0];
      var dx = t.clientX - startX;
      var dy = t.clientY - startY;
      /* 横向位移超阈 → 切到横向滚动模式，放弃下拉 */
      if (Math.abs(dx) > MOVE_TOL * 2 && Math.abs(dx) > Math.abs(dy)) {
        pulling = false; return;
      }
      if (dy < 0 || window.scrollY > 4) { pulling = false; return; }
      var dist = Math.min(dy, 160);
      if (dist > 8) {
        armed = true;
        pullEl.classList.add("pull-show");
        if (dist > THRESHOLD) {
          pullEl.classList.add("pull-ready");
          pullEl.style.transform = "translateY(0)";
        } else {
          pullEl.classList.remove("pull-ready");
          pullEl.style.transform = "translateY(" + (dist - THRESHOLD) + "px)";
        }
      }
    }, { passive: true });

    document.addEventListener("touchend", function () {
      pullEl.style.transform = "";
      if (!armed) { pulling = false; return; }
      if (pullEl.classList.contains("pull-ready")) requestRefresh();
      else pullEl.classList.remove("pull-show");
      pulling = false; armed = false;
    });
    document.addEventListener("radar:refreshEnd", hidePullIndicator);
  }

  /* ============================================================ 长按菜单
     按住模块卡超过 600ms → 菜单从下方浮现（不是从系统菜单）
     选项：固定 / 静音 / 分享 / 复制链接 */

  function openActionMenu(card, x, y) {
    closeActionMenu();
    var menu = document.createElement("div");
    menu.className = "action-menu open";
    menu.style.top  = Math.min(y, window.innerHeight - 230) + "px";
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - 200)) + "px";

    var name = card.querySelector(".title").textContent.trim();
    var href = card.getAttribute("href") || "/";
    var url = location.origin + href;

    menu.innerHTML =
      '<button data-act="pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l2 7h7l-5.5 4.5L17 22l-5-4-5 4 1.5-8.5L3 9h7z"/></svg>固定到顶部</button>' +
      '<button data-act="mute"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5zM23 9l-6 6M17 9l6 6"/></svg>静音一天</button>' +
      '<button data-act="share"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>分享</button>' +
      '<button data-act="copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>复制链接</button>';

    document.body.appendChild(menu);
    var onAct = function (e) {
      var act = e.currentTarget.dataset.act;
      closeActionMenu();
      document.removeEventListener("click", dismiss, true);
      if (act === "share") {
        if (navigator.share) navigator.share({ title: name + " · 个人情报台", url: url }).catch(function () {});
        else copyText(url, "已复制链接");
      } else if (act === "copy") {
        copyText(url, "已复制链接");
      } else if (act === "mute") {
        card.style.transition = "opacity .3s, transform .3s";
        card.style.opacity = "0.4";
        card.style.transform = "scale(0.95)";
        setTimeout(function () { card.style.display = "none"; }, 320);
        try {
          var muted = JSON.parse(localStorage.getItem(MUTED_KEY) || "{}");
          muted[name] = Date.now() + 86400000;
          localStorage.setItem(MUTED_KEY, JSON.stringify(muted));
        } catch (e) {}
        toast("已将 " + name + " 静音 24 小时");
        vibrate(8);
      } else if (act === "pin") {
        try {
          var pins = JSON.parse(localStorage.getItem(PINNED_KEY) || "[]");
          if (pins.indexOf(name) === -1) pins.unshift(name);
          localStorage.setItem(PINNED_KEY, JSON.stringify(pins));
        } catch (e) {}
        toast("已固定到顶部（重新打开生效）");
        vibrate(8);
      }
    };
    Array.prototype.forEach.call(menu.querySelectorAll("button"), function (b) {
      b.addEventListener("click", onAct);
    });
    var dismiss = function () { closeActionMenu(); document.removeEventListener("click", dismiss, true); };
    setTimeout(function () { document.addEventListener("click", dismiss, true); }, 50);
  }
  function closeActionMenu() {
    document.querySelectorAll(".action-menu").forEach(function (m) { m.remove(); });
  }

  function setupLongPressMenu() {
    var HOLD = 600, MOVE = 12;
    document.querySelectorAll(".mod-card").forEach(function (card) {
      var st = null, x = 0, y = 0, sx = 0, sy = 0;
      card.addEventListener("touchstart", function (e) {
        if (e.touches.length !== 1) return;
        sx = e.touches[0].clientX; sy = e.touches[0].clientY;
        st = setTimeout(function () {
          /* 涟漪 */
          var r = card.getBoundingClientRect();
          var ripple = document.createElement("span");
          ripple.className = "press-ripple";
          ripple.style.width = ripple.style.height = "120px";
          ripple.style.left = (sx - r.left) + "px";
          ripple.style.top  = (sy - r.top)  + "px";
          card.appendChild(ripple);
          setTimeout(function () { ripple.remove(); }, 400);
          /* 菜单 */
          x = Math.min(Math.max(sx - 80, 8), window.innerWidth - 200);
          y = Math.max(sy - 60, 80);
          openActionMenu(card, x, y);
          vibrate(15);
          st = null;
        }, HOLD);
      }, { passive: true });
      card.addEventListener("touchmove", function (e) {
        var t = e.touches[0];
        if (Math.abs(t.clientX - sx) > MOVE || Math.abs(t.clientY - sy) > MOVE) {
          if (st) { clearTimeout(st); st = null; }
        }
      }, { passive: true });
      card.addEventListener("touchend", function () { if (st) { clearTimeout(st); st = null; } });
    });
  }

  /* ============================================================ 长按链接复制
     (单行文本/code 专用) —— 旧功能保留 */

  var suppressNextClick = false;
  function setupLongPressCopy() {
    var SEL = ".rank-title a, code, [data-copy]";
    var HOLD_MS = 520, MOVE_TOL = 10;
    document.addEventListener("touchstart", function (e) {
      var el = e.target.closest && e.target.closest(SEL);
      if (!el) return;
      var x0 = e.touches[0].clientX, y0 = e.touches[0].clientY;
      var tid = setTimeout(function () {
        var text = (el.dataset && el.dataset.copy) || el.textContent.trim();
        if (text) {
          var preview = text.length > 18 ? text.slice(0, 18) + "…" : text;
          copyText(text, "已复制 " + preview);
          suppressNextClick = true;
          vibrate(15);
        }
      }, HOLD_MS);
      var cleanup = function () {
        clearTimeout(tid);
        el.removeEventListener("touchmove", onMove);
        el.removeEventListener("touchend", onEnd);
        el.removeEventListener("touchcancel", onEnd);
      };
      var onMove = function (ev) {
        var t = ev.touches[0];
        if (Math.abs(t.clientX - x0) > MOVE_TOL ||
            Math.abs(t.clientY - y0) > MOVE_TOL) cleanup();
      };
      var onEnd = function () { cleanup(); };
      el.addEventListener("touchmove", onMove, { passive: true });
      el.addEventListener("touchend", onEnd);
      el.addEventListener("touchcancel", onEnd);
    }, { passive: true });
    document.addEventListener("click", function (e) {
      if (suppressNextClick) {
        suppressNextClick = false;
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    }, true);
  }

  /* ============================================================ 数字 count-up
     Hero num: 进入视口时从 0 滚动到目标 —— 让数据"活"。
     受 prefers-reduced-motion 守护。*/

  function setupCountUp() {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var els = document.querySelectorAll("[data-countup]");
    if (!els.length) return;
    if (!("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.textContent = el.dataset.countup; });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target;
        io.unobserve(el);
        var target = parseFloat(el.dataset.countup);
        var suffix = el.dataset.countupSuffix || "";
        var prefix = el.dataset.countupPrefix || "";
        if (isNaN(target)) return;
        var dur = 800;
        var start = performance.now();
        function step(t) {
          var k = Math.min((t - start) / dur, 1);
          k = 1 - Math.pow(1 - k, 3);   /* ease-out-cubic */
          var v = prefix + formatNumber(target * k, target) + suffix;
          el.textContent = v;
          if (k < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
      });
    }, { threshold: 0.4 });
    els.forEach(function (el) { io.observe(el); });
  }
  function formatNumber(v, target) {
    /* target 决定小数位：整数目标 → 不显示小数 */
    if (Math.abs(target) >= 100) return Math.round(v).toLocaleString();
    if (Math.abs(target) >= 10)  return v.toFixed(1);
    return v.toFixed(2);
  }

  /* ============================================================ Swipe 切模块
     模块页（market_flow / github_trending）左右滑切到上下一个模块。
     阈值：水平位移 ≥ 80 且 < 水平位移 < 竖直位移 * 1.5 */

  function setupSwipeBetweenModules() {
    var deck = document.querySelector(".swipe-deck");
    if (!deck) return;
    /* 从底栏推算：当前页是哪个模块？上一个 / 下一个是谁？
       不在 Python 端塞 data 进来 —— 这样模板层零改动，render.py 不动。 */
    var items = Array.prototype.slice.call(document.querySelectorAll(".bottom-nav .bn-item"));
    var activeIdx = -1;
    items.forEach(function (el, i) { if (el.classList.contains("active")) activeIdx = i; });
    if (activeIdx <= 0) return; /* 首页 / 第一个模块 → 没得上一个模块可跳 */
    var prev = items[activeIdx - 1];
    var next = items[activeIdx + 1];
    var prevUrl = prev ? prev.href : null;
    var nextUrl = next ? next.href : null;
    if (!prevUrl && !nextUrl) return;

    var sx = 0, sy = 0, t0 = 0, tracking = false;
    var THRESHOLD = 80, RATIO = 1.5;

    document.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) return;
      if (e.target.closest(".table-wrap, .tabs, .chips, .panel.is-collapsed, .fold")) return;
      sx = e.touches[0].clientX; sy = e.touches[0].clientY;
      t0 = Date.now(); tracking = true;
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
      if (!tracking || e.touches.length !== 1) return;
      var dx = e.touches[0].clientX - sx;
      var dy = e.touches[0].clientY - sy;
      if (Math.abs(dy) > Math.abs(dx)) return;
      if (dx > 18 && prevUrl) deck.dataset.swipeState = "peek-left";
      else if (dx < -18 && nextUrl) deck.dataset.swipeState = "peek-right";
      else deck.removeAttribute("data-swipe-state");
    }, { passive: true });

    document.addEventListener("touchend", function (e) {
      if (!tracking) return;
      var dx = e.changedTouches[0].clientX - sx;
      var dy = e.changedTouches[0].clientY - sy;
      tracking = false;
      deck.removeAttribute("data-swipe-state");
      var dur = Date.now() - t0;
      if (Math.abs(dx) < THRESHOLD) return;
      if (Math.abs(dy) > Math.abs(dx) * RATIO) return;
      if (dur > 700) return;
      if (dx > 0 && prevUrl) { toast("← " + prev.textContent.trim()); setTimeout(function(){ location.href = prevUrl; }, 130); }
      else if (dx < 0 && nextUrl) { toast(next.textContent.trim() + " →"); setTimeout(function(){ location.href = nextUrl; }, 130); }
    });
  }

  /* ============================================================ 面板可折叠 */
  function setupCollapsible() {
    var stored = {};
    try { stored = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "{}"); }
    catch (e) { stored = {}; }
    document.querySelectorAll(".panel").forEach(function (p) {
      var h = p.querySelector("h2");
      if (!h) return;
      /* 设置页的卡片不参与折叠：那里每个标题下面就是控件本身，
         点一下标题把开关收起来，看起来像"设置项消失了"。 */
      if (p.hasAttribute("data-no-collapse")) return;
      var key = h.textContent.trim().slice(0, 12);
      if (stored[key]) p.classList.add("is-collapsed");
      h.addEventListener("click", function () {
        p.classList.toggle("is-collapsed");
        var s = {};
        try { s = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "{}"); }
        catch (e) { s = {}; }
        s[key] = p.classList.contains("is-collapsed");
        try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(s)); }
        catch (e) {}
      });
    });
  }

  /* ============================================================ 分享 + 安装横幅 */
  function setupShare() {
    /* 顶栏 share 按钮 */
    var shareBtn = document.getElementById("share-btn");
    if (shareBtn) {
      shareBtn.hidden = false;
      shareBtn.addEventListener("click", function () {
        var url = location.origin + location.pathname;
        var title = document.title;
        if (navigator.share) {
          navigator.share({ title: title, url: url }).then(function () { vibrate(10); }).catch(function () {});
        } else {
          copyText(url, "已复制链接");
        }
      });
    }
    /* FAB 刷新 */
    if (fab) {
      fab.addEventListener("click", requestRefresh);
    }
  }

  function setupInstallBanner() {
    var el = document.getElementById("install-banner");
    if (!el) return;
    try { if (localStorage.getItem(INSTALL_DISMISS_KEY)) return; } catch (e) {}
    try { if (location.search.indexOf("t=") !== -1) return; } catch (e) {}
    if (!window.matchMedia || !window.matchMedia("(max-width: 560px)").matches) return;
    el.hidden = false;
    var x = el.querySelector(".ib-x");
    if (x) x.addEventListener("click", function () {
      el.hidden = true;
      try { localStorage.setItem(INSTALL_DISMISS_KEY, "1"); } catch (e) {}
    });
  }

  /* ============================================================ App 版本入口
     只在安卓壳内出现（浏览器里 window.RadarNative 是 undefined）。
     做两件事：
       1. 显示"我装的是哪一版" —— 用户第一次能看见自己的版本
       2. 点一下 = 让原生跳过 6 小时闸、立刻查一次（并且必须有反馈）
     另外静默对一次 /api/app：有新版本就把这行点亮成 "可更新到 vX"，
     用户不点也能知道。 */

  function setupAppVersion() {
    var native = window.RadarNative;
    if (!native) return;                       // 浏览器：不显示
    var box = document.getElementById("app-ver");
    var nameEl = document.getElementById("app-ver-name");
    var actEl = document.getElementById("app-ver-act");
    if (!box || !nameEl) return;

    var curName = "", curCode = 0;
    try {
      curName = native.versionName() || "";
      curCode = native.versionCode() || 0;
    } catch (e) { /* 桥异常：当作信息拿不到，但入口照常显示 */ }
    nameEl.textContent = curName || String(curCode || "?");

    box.addEventListener("click", function () {
      try { native.checkForUpdate(); vibrate(10); } catch (e) {}
    });
    box.hidden = false;

    // 静默对比一次：走 WebView 自己的 Cookie 鉴权，不必再带 ?t=
    fetch("/api/app", { headers: { "Accept": "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (o) {
        if (!o) return;
        var latestCode = o.versionCode || 0;
        var latestName = o.versionName || String(latestCode);
        if (latestCode > curCode) {
          box.classList.add("has-new");
          if (actEl) actEl.textContent = "可更新到 v" + latestName;
          box.title = "点一下立即更新";
        }
      })
      .catch(function () { /* 拿不到就当没新版，别打扰 */ });
  }

  /* ============================================================ 设置页（/settings/）
     这一页上的每个动作最后都要经过 JS 桥落到原生，所以：
       · 有桥 → 解开设置区，值从原生读（绝不写死在 HTML 里）
       · 没桥 → **明说"只在 App 内生效"**，而不是把开关画出来让人点。
         一个点了没有反应的开关，比没有开关更让人困惑。

     页面结构在 templates/settings.html，样式在 style.css 第 32 节。 */
  function setupSettings() {
    var envEl = document.getElementById("set-env");
    if (!envEl) return;                        // 不是设置页，直接退
    var native = window.RadarNative;
    var nativeBox = document.getElementById("set-native");
    var webBox = document.getElementById("set-web-only");

    if (!native) {
      envEl.textContent = "当前是在浏览器里打开的。";
      envEl.classList.add("is-warn");
      if (webBox) webBox.hidden = false;
      return;
    }

    envEl.textContent = "已连接到安卓 App。";
    envEl.classList.remove("is-warn");
    if (nativeBox) nativeBox.hidden = false;

    var sw = document.getElementById("auto-update");
    var curEl = document.getElementById("set-cur");
    var latestEl = document.getElementById("set-latest");
    var msgEl = document.getElementById("set-msg");
    var checkBtn = document.getElementById("set-check");
    var dlBtn = document.getElementById("set-dl");
    var permPanel = document.getElementById("set-perm-panel");
    var permBtn = document.getElementById("set-perm");

    var curName = "", curCode = 0;
    try {
      curName = native.versionName() || "";
      curCode = native.versionCode() || 0;
    } catch (e) { /* 桥异常：当作读不到，页面照常可用 */ }
    if (curEl) curEl.textContent = curName ? ("v" + curName) : "—";

    function say(text, cls) {
      if (!msgEl) return;
      msgEl.textContent = text || "";
      msgEl.className = "set-note" + (cls ? " " + cls : "");
    }

    /* ---------------- 自动更新开关 ---------------- */
    if (sw) {
      var on = false;
      try { on = !!native.getAutoUpdate(); } catch (e) {}
      sw.checked = on;
      sw.addEventListener("change", function () {
        try { native.setAutoUpdate(sw.checked); } catch (e) {}
        vibrate(sw.checked ? 12 : 6);
        say(sw.checked
          ? "已开启：以后发现新版本会自动下载并调起安装。"
          : "已关闭：发现新版本时会先问你。",
          sw.checked ? "is-ok" : "");
      });
    }

    /* ---------------- 安装权限 ----------------
       只在"还没授权"时才把这块露出来 —— 已经能装了还摆一个授权按钮，
       会让人以为哪里没配好。从系统设置页返回时补查一次。 */
    function paintPerm() {
      var ok = true;
      try { ok = native.canInstall(); } catch (e) {}
      if (permPanel) permPanel.hidden = !!ok;
      return ok;
    }
    if (permBtn) {
      permBtn.addEventListener("click", function () {
        try { native.openInstallSettings(); } catch (e) {}
      });
    }
    paintPerm();
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) paintPerm();
    });

    /* ---------------- 版本信息 ---------------- */
    function paintLatest(o) {
      var code = o.versionCode || 0;
      var name = o.versionName || String(code);
      var newer = code > curCode;
      if (latestEl) {
        latestEl.textContent = "";
        latestEl.appendChild(document.createTextNode("v" + name));
        if (newer) {
          var b = document.createElement("span");
          b.className = "set-badge";
          b.textContent = "有新版本";
          latestEl.appendChild(b);
        }
      }
      if (dlBtn) dlBtn.hidden = !newer;
      return newer;
    }

    function refresh() {
      say("正在检查…", "is-busy");
      /* 走 WebView 自己的 Cookie 鉴权，不用再带 ?t= */
      fetch("/api/app", { headers: { "Accept": "application/json" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (o) {
          if (!o) { say("检查失败，请检查网络后重试", "is-warn"); return; }
          var newer = paintLatest(o);
          say(newer
            ? ("有新版本 v" + (o.versionName || o.versionCode) + "，可以下载安装")
            : "已是最新版本",
            newer ? "is-busy" : "is-ok");
        })
        .catch(function () { say("检查失败，请检查网络后重试", "is-warn"); });
    }
    refresh();

    if (checkBtn) {
      checkBtn.addEventListener("click", function () {
        vibrate(10);
        /* 让原生跳闸查一次（它同时会更新"上次检查时间"并给反馈） */
        try { native.checkForUpdate(); } catch (e) {}
        refresh();
      });
    }

    if (dlBtn) {
      dlBtn.addEventListener("click", function () {
        vibrate(12);
        if (!paintPerm()) {
          say("请先允许本应用「安装未知应用」", "is-warn");
          try { native.openInstallSettings(); } catch (e) {}
          return;
        }
        say("已开始下载，请稍候…", "is-busy");
        try { native.downloadUpdate(); } catch (e) { say("下载没能启动", "is-warn"); }
      });
    }
  }

  /* ============================================================ 把静音恢复 */
  function restoreMuted() {
    try {
      var muted = JSON.parse(localStorage.getItem(MUTED_KEY) || "{}");
      var now = Date.now();
      var anyMuted = false;
      document.querySelectorAll(".mod-card").forEach(function (card) {
        var name = (card.querySelector(".title") || {}).textContent;
        if (muted[name] && muted[name] > now) {
          card.style.transition = "opacity .3s";
          card.style.opacity = "0.4";
          anyMuted = true;
        }
      });
      if (anyMuted) toast("上次有模块被静音了，可长按恢复", 4000);
    } catch (e) {}
  }

  /* ============================================================ 启动 */

  if (btn) btn.addEventListener("click", requestRefresh);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { clearTimeout(timer); tick(); updateGreeting(); }
  });
  window.addEventListener("online", function () { clearTimeout(timer); tick(); });

  try {
    var seen = localStorage.getItem(LAST_SEEN_KEY);
    if (seen && seen !== PAGE_FP) toast("上次访问后有新数据", 3200);
    localStorage.setItem(LAST_SEEN_KEY, PAGE_FP);
  } catch (e) {}

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  var installPrompt = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    installPrompt = e;
    if (installBtn) installBtn.hidden = false;
  });
  if (installBtn) {
    installBtn.addEventListener("click", function () {
      if (!installPrompt) return;
      installPrompt.prompt();
      installPrompt.userChoice.then(function () {
        installPrompt = null;
        installBtn.hidden = true;
      });
    });
  }

  try {
    var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var standalone = window.navigator.standalone === true ||
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
    if (isIOS && !standalone && !localStorage.getItem("radar.iosHinted")) {
      localStorage.setItem("radar.iosHinted", "1");
      setTimeout(function () { toast("点 分享 → 添加到主屏幕，就能当 app 用", 6000); }, 2500);
    }
  } catch (e) {}

  /* ============================================================ 读书 · 进度与续读
     静态站没有后端状态，"读到哪"只能存在浏览器里。
     一份 localStorage 数据同时供三处使用：
       1. 书房每本书的进度条
       2. 单本书目录里每一章的"已读"
       3. 书房的"继续阅读"卡片
     把它们放在同一个 key 下，是为了避免"三处各存一份、彼此对不上"。

     存储形状（带 v1 后缀，将来改结构可以整体换 key 而不误读旧数据）：
       { "<书>": { title, last, lastTitle, at, chapters: { "<章>": 1 } } } */

  var READ_KEY = "radar.reading.v1";

  function readStore() {
    try {
      var raw = localStorage.getItem(READ_KEY);
      var o = raw ? JSON.parse(raw) : null;
      return (o && typeof o === "object") ? o : {};
    } catch (e) { return {}; }
  }

  function readSave(state) {
    try { localStorage.setItem(READ_KEY, JSON.stringify(state)); } catch (e) {}
  }

  function readBook(state, book, bookTitle) {
    var b = state[book];
    if (!b || typeof b !== "object") { b = state[book] = {}; }
    if (!b.chapters || typeof b.chapters !== "object") b.chapters = {};
    if (bookTitle && !b.title) b.title = bookTitle;
    return b;
  }

  function readCount(b) {
    var n = 0, k;
    for (k in b.chapters) if (b.chapters[k]) n++;
    return n;
  }

  function readSet(book, bookTitle, chapter, chapterTitle, done) {
    var s = readStore();
    var b = readBook(s, book, bookTitle);
    if (done) {
      b.chapters[chapter] = 1;
      b.last = chapter;
      b.lastTitle = chapterTitle || "";
      b.at = Date.now();
      if (bookTitle) b.title = bookTitle;
    } else {
      delete b.chapters[chapter];
      /* 取消已读时，别让"继续阅读"还指着这一章 */
      if (b.last === chapter) { b.last = ""; b.lastTitle = ""; }
    }
    readSave(s);
    return b;
  }

  function readTouch(book, bookTitle, chapter, chapterTitle) {
    /* 只更新"最近在读"，**不动已读标记** —— 重进一章不该把它变回未读 */
    var s = readStore();
    var b = readBook(s, book, bookTitle);
    b.last = chapter;
    b.lastTitle = chapterTitle || "";
    b.at = Date.now();
    if (bookTitle) b.title = bookTitle;
    readSave(s);
  }

  /* ---------------- 阅读位置：记住"这一章读到哪儿" ----------------
     为什么存**百分比**就够，而不去存"某个小节的 id"：
     书稿是按 commit 钉死的（见 books.py），同一章的排版不会变，
     所以"屏高的百分之几"在同一台设备上是稳定的。
     反而用锚点会更差 —— 当你停在某个标题下面几千字处时，
     按锚点恢复会把你**拉回到那个标题**，等于每次都在往回跳。
     小节名只用来做显示用的小标签（"1.2 张量并行"），不参与定位。 */
  function readPos(b, chap) {
    if (!b || !b.pos || typeof b.pos !== "object") return null;
    var p = b.pos[chap];
    return (p && typeof p.p === "number") ? p : null;
  }

  function readPosSet(book, bookTitle, chap, chapTitle, p, label) {
    var s = readStore();
    var b = readBook(s, book, bookTitle);
    if (!b.pos || typeof b.pos !== "object") b.pos = {};
    b.pos[chap] = {
      p: Math.round(Math.max(0, Math.min(100, p)) * 10) / 10,
      label: label || "",
      t: chapTitle || "",
      at: Date.now()
    };
    readSave(s);
    return b.pos[chap];
  }

  /* ---------------- 书签 ---------------- */
  function readMarks(b) {
    return (b && b.marks && b.marks.length) ? b.marks : [];
  }

  function markFind(b, chap, p) {
    /* 同一章里位置几乎相同就算同一个书签 ——
       否则连点几下会堆出一串几乎重合的条目，列表立刻变成噪音。 */
    var arr = readMarks(b), i, m;
    for (i = 0; i < arr.length; i++) {
      m = arr[i];
      if (m.chap === chap && Math.abs((m.p || 0) - p) < 2.5) return m;
    }
    return null;
  }

  function markAdd(book, bookTitle, chap, chapTitle, p, label) {
    var s = readStore();
    var b = readBook(s, book, bookTitle);
    if (!b.marks || !b.marks.length) b.marks = [];
    var dup = markFind(b, chap, p);
    if (dup) return null;
    b.marks.push({
      id: "m" + Date.now() + "-" + b.marks.length,
      chap: chap, t: chapTitle || "", label: label || "",
      p: Math.round(Math.max(0, Math.min(100, p)) * 10) / 10,
      at: Date.now()
    });
    readSave(s);
    return b.marks[b.marks.length - 1];
  }

  function markRemove(book, id) {
    var s = readStore();
    var b = readBook(s, book, "");
    if (!b.marks || !b.marks.length) return false;
    var n = b.marks.length;
    b.marks = b.marks.filter(function (m) { return m.id !== id; });
    if (b.marks.length === n) return false;
    readSave(s);
    return true;
  }

  /* 相对时间：书签列表里"3 天前"比一个完整时间戳更好扫 */
  function agoOf(ts) {
    var d = Date.now() - (ts || 0);
    if (d < 60000) return "刚刚";
    if (d < 3600000) return Math.floor(d / 60000) + " 分钟前";
    if (d < 86400000) return Math.floor(d / 3600000) + " 小时前";
    if (d < 2592000000) return Math.floor(d / 86400000) + " 天前";
    return new Date(ts).toLocaleDateString("zh-CN");
  }

  /* 把一本书的书签渲染进列表。章节页和目录页共用同一份实现 ——
     两处各写一遍必然会漂移（一个按时间倒序、一个忘了转义…）。 */
  function renderBookmarks(book, listEl, emptyEl, badgeEl) {
    if (!listEl) return 0;
    var b = readStore()[book] || {};
    var marks = readMarks(b).slice();
    /* 最近加的排最前 —— 刚存的书签应该一眼看到 */
    marks.sort(function (x, y) { return (y.at || 0) - (x.at || 0); });

    listEl.textContent = "";
    var i, m, li, a, del, t, meta;
    for (i = 0; i < marks.length; i++) {
      m = marks[i];
      li = document.createElement("li");
      li.className = "bm-item";

      a = document.createElement("a");
      a.className = "bm-link";
      /* 跳到**书签自己的位置**：用 #p= 而不是章节里的小节锚点，
         这样同一个标题下的多个书签也能各自落到准确的位置。 */
      a.setAttribute("href", "/reader/" + book + "/" + m.chap + ".html#p=" + (m.p || 0));

      t = document.createElement("span");
      t.className = "bm-t";
      t.textContent = m.label || m.t || m.chap;
      a.appendChild(t);

      meta = document.createElement("span");
      meta.className = "bm-meta";
      var parts = [];
      if (m.t && m.label) parts.push(m.t);
      parts.push("读到 " + (m.p || 0) + "%");
      parts.push(agoOf(m.at));
      meta.textContent = parts.join(" · ");
      a.appendChild(meta);

      del = document.createElement("button");
      del.type = "button";
      del.className = "bm-del";
      del.setAttribute("data-mark", m.id);
      del.setAttribute("aria-label", "删除书签");
      del.textContent = "×";

      li.appendChild(a);
      li.appendChild(del);
      listEl.appendChild(li);
    }

    if (badgeEl) badgeEl.textContent = marks.length;
    if (emptyEl) emptyEl.hidden = marks.length > 0;
    return marks.length;
  }

  /* 事件委托：书签是 JS 现渲染的，逐个挂监听既啰嗦又容易漏 */
  function bindMarkList(listEl, book, onChange) {
    if (!listEl || listEl.__bmBound) return;
    listEl.__bmBound = true;
    listEl.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest(".bm-del") : null;
      if (!btn) return;
      ev.preventDefault();
      var id = btn.getAttribute("data-mark");
      if (markRemove(book, id)) {
        vibrate(6);
        toast("已删除书签");
        if (onChange) onChange();
      }
    });
  }

  /* ---- 章节页：滚动进度条 + 读完自动标记 + 手动开关 ---- */
  function setupReaderChapter() {
    var root = document.querySelector(".reader");
    if (!root) return;
    var book = root.getAttribute("data-book");
    var bookTitle = root.getAttribute("data-book-title") || "";
    var chapter = root.getAttribute("data-chapter");
    var chapterTitle = root.getAttribute("data-chapter-title") || "";
    var total = parseInt(root.getAttribute("data-total"), 10) || 0;
    if (!book || !chapter) return;

    var bar = document.getElementById("read-bar-fill");
    var btn = document.getElementById("mark-read");
    var btnText = document.getElementById("mark-read-text");

    /* 进页面就先落一次"最近在读"，这样即使没读完，书架上的续读入口也是对的 */
    readTouch(book, bookTitle, chapter, chapterTitle);

    /* ---- 小节标题：只用来给位置/书签起一个"认得出来"的名字 ---- */
    var heads = [];
    (function () {
      var all, i, n, ok = { H1: 1, H2: 1, H3: 1, H4: 1 };
      try { all = document.querySelectorAll(".book-content *"); } catch (e) { return; }
      for (i = 0; i < all.length; i++) {
        n = all[i];
        if (n.id && ok[n.tagName]) heads.push(n);
      }
    })();

    function headAt() {
      /* 视口上方（留 90px 余量）的最后一个标题 = 当前所在小节。
         标题在文档里天然有序，所以一旦遇到"还没到"的就可以停。 */
      var best = null, i;
      for (i = 0; i < heads.length; i++) {
        if (heads[i].getBoundingClientRect().top <= 90) best = heads[i]; else break;
      }
      return best;
    }

    function curPct() {
      var doc = document.documentElement;
      var max = doc.scrollHeight - window.innerHeight;
      var y = window.pageYOffset || doc.scrollTop || 0;
      return max > 40 ? Math.min(100, Math.max(0, (y / max) * 100)) : 100;
    }

    function markLabel() {
      var h = headAt();
      return h ? (h.textContent || "").trim().slice(0, 60) : "";
    }

    /* ===================== 位置的保存 ===================== */
    /* ⚠️ 只靠 scroll 节流是不够的。手机上离开页面是"按返回键"（pagehide），
       如果刚滚完还没到节流窗口就卸载了，这次位置就白记了 ——
       用户看到的就是"一返回就找不到了"。所以两个补丁：
         1. 滚动时节流保存（700ms）
         2. pagehide / 切到后台时**立刻**再保存一次 */
    var saveTimer = null;
    function savePos() {
      /* ⚠️ 还没"落定初始位置"之前**一律不写**。
         否则会踩坑 1 的同款事故：进页面时 curPct() 还是 0，
         用户没滚动就返回 → 把好不容易存下的位置**覆盖成 0%**。 */
      if (!restoreSettled) return;
      readPosSet(book, bookTitle, chapter, chapterTitle, curPct(), markLabel());
      paintMarkBtn();
    }
    function saveSoon() {
      if (saveTimer) return;
      saveTimer = setTimeout(function () { saveTimer = null; savePos(); }, 700);
    }
    window.addEventListener("pagehide", savePos);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) savePos();
    });

    /* ===================== 位置的恢复 ===================== */
    var restoreSettled = false;
    var userScrolled = false;
    ["touchstart", "wheel", "keydown", "mousedown"].forEach(function (ev) {
      window.addEventListener(ev, function () { userScrolled = true; }, { passive: true });
    });

    function hashPct() {
      /* 书签跳转用 `#p=37.5`。用 hash 而不是 query：
         hash 不会参与鉴权/缓存，也不会把 `?t=` 挤掉。 */
      var m = /[#&?]p=([0-9]+(?:\.[0-9]+)?)/.exec(location.hash || "");
      return m ? parseFloat(m[1]) : null;
    }

    function restore() {
      var fromHash = hashPct();
      var p = fromHash;
      if (p === null) {
        var saved = readPos(readBook(readStore(), book, bookTitle), chapter);
        if (saved) p = saved.p;
      }
      if (p === null || !(p > 0.2)) { restoreSettled = true; return; }
      if (userScrolled) { restoreSettled = true; return; }
      var doc = document.documentElement;
      var max = doc.scrollHeight - window.innerHeight;
      if (max <= 40) return;            /* 内容还没铺开（图没加载完），等 load 再试 */
      window.scrollTo(0, Math.round(max * Math.min(100, p) / 100));
      restoreSettled = true;
      if (fromHash === null) toast("已回到上次读到的位置 · " + Math.round(p) + "%", 2200);
    }

    function isDone() {
      var st = readStore();
      return !!(st[book] && st[book].chapters && st[book].chapters[chapter]);
    }

    function paint() {
      if (!btn) return;
      var done = isDone();
      btn.classList.toggle("is-on", done);
      btn.setAttribute("aria-pressed", done ? "true" : "false");
      if (btnText) btnText.textContent = done ? "已读完" : "标记为已读";
    }

    /* ===================== 书签 ===================== */
    var bmBtn = document.getElementById("add-bookmark");
    var bmBtnText = document.getElementById("add-bookmark-text");
    var bmPanel = document.getElementById("bm-panel");
    var bmList = document.getElementById("bm-list");
    var bmEmpty = document.getElementById("bm-empty");
    var bmBadge = document.getElementById("bm-count-badge");
    var bmToggle = document.getElementById("toggle-bookmarks");
    var bmCountInline = document.getElementById("bm-count-inline");

    function paintMarkBtn() {
      if (!bmBtn) return;
      var here = markFind(readStore()[book] || {}, chapter, curPct());
      bmBtn.classList.toggle("is-on", !!here);
      bmBtn.setAttribute("aria-pressed", here ? "true" : "false");
      if (bmBtnText) bmBtnText.textContent = here ? "已加书签" : "加书签";
    }

    function paintMarks() {
      var n = renderBookmarks(book, bmList, bmEmpty, bmBadge);
      if (bmToggle) bmToggle.classList.toggle("is-on", n > 0);
      /* 按钮上带条数：不点开也能知道"这本书里有几条书签" */
      if (bmCountInline) {
        bmCountInline.textContent = String(n);
        bmCountInline.hidden = n === 0;
      }
      paintMarkBtn();
    }

    if (btn) {
      btn.addEventListener("click", function () {
        var done = !isDone();
        readSet(book, bookTitle, chapter, chapterTitle, done);
        paint();
        vibrate(done ? 12 : 6);
        if (done) {
          toast(total && readCount(readStore()[book] || {}) >= total
            ? "这本书读完了 🎉" : "已标记为读完");
        }
      });
    }

    if (bmBtn) {
      bmBtn.addEventListener("click", function () {
        var p = curPct();
        var here = markFind(readStore()[book] || {}, chapter, p);
        if (here) {
          markRemove(book, here.id);
          vibrate(6);
          toast("已删除书签");
        } else {
          var m = markAdd(book, bookTitle, chapter, chapterTitle, p, markLabel());
          vibrate(12);
          if (m) {
            toast("已加书签 · " + Math.round(p) + "%");
            /* 加完顺手把列表露出来 —— 否则用户不知道书签存到哪儿去了 */
            if (bmPanel) { bmPanel.hidden = false; bmToggle && bmToggle.setAttribute("aria-expanded", "true"); }
          } else {
            toast("这个位置已经有书签了");
          }
        }
        paintMarks();
      });
    }

    if (bmToggle && bmPanel) {
      bmToggle.addEventListener("click", function () {
        bmPanel.hidden = !bmPanel.hidden;
        bmToggle.setAttribute("aria-expanded", bmPanel.hidden ? "false" : "true");
        if (!bmPanel.hidden) {
          paintMarks();
          bmPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      });
    }

    bindMarkList(bmList, book, paintMarks);
    paint();
    paintMarks();

    var auto = false;
    function onScroll() {
      var pct = curPct();
      if (bar) bar.style.width = pct.toFixed(1) + "%";
      saveSoon();
      /* 85% 视为读完 —— 结尾常是注释/参考文献，不必真的滑到底 */
      if (!auto && pct >= 85 && !isDone()) {
        auto = true;
        readSet(book, bookTitle, chapter, chapterTitle, true);
        paint();
      }
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () { ticking = false; onScroll(); });
    }, { passive: true });
    window.addEventListener("resize", function () { onScroll(); if (restoreSettled) restore(); });

    /* 先立即试一次；图还没加载完、撑不出高度时，等 load 再补一次
       （只在用户还没自己滚过的情况下 —— 不能把人从当前位置拽走） */
    restore();
    window.addEventListener("load", function () { if (!userScrolled) restore(); });
    onScroll();
  }

  /* ---- 书房：进度条 + 继续阅读 ---- */
  function setupReaderShelf() {
    var shelf = document.querySelector(".shelf");
    var resume = document.getElementById("resume");
    if (!shelf && !resume) return;
    var state = readStore();

    var cards = document.querySelectorAll(".book-card[data-book]");
    var i, card, slug, total, b, done, pct;
    for (i = 0; i < cards.length; i++) {
      card = cards[i];
      slug = card.getAttribute("data-book");
      total = parseInt(card.getAttribute("data-chapters"), 10) || 0;
      b = state[slug];
      done = b ? readCount(b) : 0;
      pct = total ? Math.round((done / total) * 100) : 0;
      var fill = card.querySelector(".bk-progress > i");
      var label = card.querySelector(".bk-progress-t");
      if (fill) fill.style.width = pct + "%";
      if (label) {
        label.textContent = done
          ? ("已读 " + done + " / " + total + " 章 · " + pct + "%")
          : "未开始";
      }
    }

    /* 继续阅读：取所有书里 at 最大的那一章 */
    if (!resume) return;
    var best = null, k;
    for (k in state) {
      var bb = state[k];
      if (!bb || !bb.last) continue;
      if (!best || (bb.at || 0) > (best.at || 0)) {
        best = { slug: k, last: bb.last, lastTitle: bb.lastTitle, title: bb.title,
                 at: bb.at || 0, pos: readPos(bb, bb.last) };
      }
    }
    if (!best) return;
    var link = document.getElementById("resume-link");
    var elC = document.getElementById("resume-chapter");
    var elB = document.getElementById("resume-book");
    var elP = document.getElementById("resume-pos");
    /* 不带 #p= —— 章节页会自己按存下的位置恢复，
       这条链接永远指向**当下最新**的位置，写死一个百分比反而会过时。 */
    if (link) link.setAttribute("href", "/reader/" + best.slug + "/" + best.last + ".html");
    if (elC) elC.textContent = best.lastTitle || best.last;
    if (elB) elB.textContent = best.title || best.slug;
    if (elP) {
      if (best.pos && best.pos.p > 1) {
        elP.textContent = (best.pos.label ? best.pos.label + " · " : "")
          + "读到 " + Math.round(best.pos.p) + "%";
        elP.hidden = false;
      } else {
        elP.hidden = true;
      }
    }
    resume.hidden = false;
  }

  /* ---- 单本书：目录里的已读标记 + 主按钮文案 ---- */
  function setupReaderBook() {
    var hero = document.querySelector(".book-hero");
    if (!hero) return;
    var state = readStore();

    var items = document.querySelectorAll(".toc-item[data-chapter]");
    var i, slug, b, anyRead = 0;
    /* 书 slug 从主按钮上取 —— 那里已经有 data-resume-book */
    var cta = document.querySelector("[data-resume-book]");
    var book = cta ? cta.getAttribute("data-resume-book") : "";
    b = book ? state[book] : null;

    for (i = 0; i < items.length; i++) {
      slug = items[i].getAttribute("data-chapter");
      if (b && b.chapters && b.chapters[slug]) {
        items[i].classList.add("is-read");
        anyRead++;
      }
    }

    var text = document.getElementById("book-cta-text");
    if (text && b && b.last) text.textContent = "继续阅读";

    /* 有读过就把主按钮指向"上次那一章"，而不是永远指向第一章 */
    if (cta && b && b.last) cta.setAttribute("href", "/reader/" + book + "/" + b.last + ".html");

    /* 目录里如果全是已读，给个轻提示，别做额外操作 */
    var tocHead = document.querySelector(".panel-head .badge.muted");
    if (tocHead && anyRead && items.length) {
      tocHead.textContent = "已读 " + anyRead + " / " + items.length;
    }

    /* 目录里顺便标出"有书签"的章 —— 否则书签只活在一个折叠面板里，等于藏起来了 */
    if (book && b) {
      var byChap = {};
      readMarks(b).forEach(function (m) { byChap[m.chap] = (byChap[m.chap] || 0) + 1; });
      for (i = 0; i < items.length; i++) {
        slug = items[i].getAttribute("data-chapter");
        if (byChap[slug]) items[i].classList.add("has-mark");
      }
    }

    /* 书签面板：**没有书签就整个不显示** —— 一个空面板只会让人以为坏了 */
    var bmPanel = document.getElementById("bm-panel");
    if (bmPanel && book) {
      var bmList = document.getElementById("bm-list");
      var bmEmpty = document.getElementById("bm-empty");
      var bmBadge = document.getElementById("bm-count-badge");

      function repaintMarks() {
        var n = renderBookmarks(book, bmList, bmEmpty, bmBadge);
        bmPanel.hidden = n === 0;
      }
      bindMarkList(bmList, book, repaintMarks);
      repaintMarks();
    }
  }

  /* ============================================================ 启动顺序 */
  setupPullToRefresh();
  setupLongPressCopy();
  setupLongPressMenu();
  setupCollapsible();
  setupShare();
  setupInstallBanner();
  setupAppVersion();
  setupSettings();
  setupSwipeBetweenModules();
  setupCountUp();
  setupReaderChapter();
  setupReaderShelf();
  setupReaderBook();
  restoreMuted();
  updateGreeting();
  /* 一分钟后问候里的时间刷新一次（如"5 分钟前"半小时不变就过期了） */
  setInterval(updateGreeting, 60 * 1000);
  schedule(1200);
})();
