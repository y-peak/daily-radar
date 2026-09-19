// settings_probe.mjs —— 用无头 Chrome 真跑一遍设置页（/settings/）。
//
// 为什么要有这个脚本：设置页是**纯运行时**的页面 —— HTML 里一个值都没有，
// 全靠 app.js 从 window.RadarNative 里读。所以"源码里有 setupSettings 这个
// 函数名"证明不了任何事：真正要验的是
//   ① 检测到桥时把设置区解开、读到的值真的画到页面上
//   ② 没检测到桥时**不画假开关**，而是明说"只在 App 内生效"
//   ③ 每个控件的动作真的落到了对应的桥方法上
//
// 真机上装不了 App 的情况下，这是最接近真实的验证方式：
// 页面是真的（从 public/ 走 HTTP 加载），桥是假的（注入的 stub）。
//
// 用法：bash tools/run_ui_probe.sh tools/settings_probe.mjs
//
// ⚠️ 两个刻意的设计：
//   1. 桥用 `Page.addScriptToEvaluateOnNewDocument` 注入 —— 必须在页面脚本
//      之前就存在，否则 setupSettings 跑的时候 window.RadarNative 还是 undefined。
//   2. `/api/app` 的 fetch 也被拦掉（只拦这一个 URL，其余透传）。
//      因为静态服务器没有这个接口。接口本身的行为由 smoke_test 覆盖，
//      这里要验的是"页面拿到 200 + 版本号之后画成什么样"。

const CDP = process.argv[2] || "http://127.0.0.1:9333";
const BASE = process.argv[3] || "http://127.0.0.1:8099";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ws, seq = 0;
const pending = new Map();
const results = [];
const pageErrors = [];

function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}

async function evalJs(expression) {
  const r = await send("Runtime.evaluate", {
    expression, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    const d = r.exceptionDetails.exception || r.exceptionDetails;
    throw new Error("页面内异常: " + (d.description || JSON.stringify(d)));
  }
  return r.result.value;
}

function check(name, ok, extra) {
  results.push({ name, ok: !!ok });
  const tail = extra === undefined ? "" : "  → " + JSON.stringify(extra);
  console.log(`${ok ? "  [OK]" : "  [!!]"} ${name}${tail}`);
}

async function connect() {
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`${CDP}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((res, rej) => {
          ws.onopen = res;
          ws.onerror = () => rej(new Error("ws error"));
        });
        ws.onmessage = (ev) => {
          const m = JSON.parse(ev.data);
          if (m.method === "Runtime.exceptionThrown") {
            const d = (m.params && m.params.exceptionDetails) || {};
            pageErrors.push(
              (d.exception && d.exception.description) || d.text || "unknown",
            );
            return;
          }
          if (m.id && pending.has(m.id)) {
            const p = pending.get(m.id);
            pending.delete(m.id);
            m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
          }
        };
        return;
      }
    } catch (e) { /* 端口还没起来，继续等 */ }
    await sleep(400);
  }
  throw new Error("连不上 Chrome CDP：" + CDP);
}

async function goto(path) {
  await send("Page.navigate", { url: path.startsWith("http") ? path : BASE + path });
  for (let i = 0; i < 60; i++) {
    try {
      if ((await evalJs("document.readyState")) === "complete") break;
    } catch (e) { /* 正在换执行上下文 */ }
    await sleep(150);
  }
  await sleep(700);          // 让 setupSettings 里的 fetch 回调跑完
}

/* ---------------- 注入用的假桥 ----------------
   只实现页面真正会调的那几个方法，返回值刻意**可观察**：
   · getAutoUpdate 读的是一个会随 setAutoUpdate 变化的变量 —— 这样
     "点了开关 → 真的调了桥 → 页面状态跟着变"这条链才是真的被验证过。
   · canInstall 初值给 false，并留一个 window.__grant() 把权限打开。
     顺序很重要：未授权时的行为（转去授权，而不是硬下）本身就是一条产品规则，
     必须**先**验它，再验授权之后的正常下载。 */
const STUB = `(() => {
  const calls = [];
  const state = { auto: false, can: false };
  window.__calls = calls;
  window.__grant = () => { state.can = true; };
  window.__appInfo = { versionCode: 99, versionName: "9.9",
                       size: 111757, url: "/dl/radar-9.9.apk" };
  window.RadarNative = {
    versionName: () => "1.4",
    versionCode: () => 5,
    getAutoUpdate: () => state.auto,
    setAutoUpdate: (v) => { state.auto = !!v; calls.push("setAutoUpdate:" + !!v); },
    canInstall: () => state.can,
    openInstallSettings: () => calls.push("openInstallSettings"),
    checkForUpdate: () => calls.push("checkForUpdate"),
    downloadUpdate: () => calls.push("downloadUpdate")
  };
  const orig = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.indexOf("/api/app") !== -1) {
      return Promise.resolve(new Response(JSON.stringify(window.__appInfo), {
        status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return orig.apply(this, arguments);
  };
})();`;

/* ---------------- 页面内取值：一律自包含，返回基本类型 ---------------- */
const snap = `(() => {
  const t = (id) => { const e = document.getElementById(id); return e ? e.textContent : null; };
  const el = (id) => document.getElementById(id);
  return {
    env: t("set-env"),
    envWarn: !!el("set-env") && el("set-env").classList.contains("is-warn"),
    nativeShown: !!el("set-native") && !el("set-native").hidden,
    webShown: !!el("set-web-only") && !el("set-web-only").hidden,
    cur: t("set-cur"),
    latest: t("set-latest"),
    msg: t("set-msg"),
    msgClass: el("set-msg") ? el("set-msg").className : "",
    checked: !!el("auto-update") && el("auto-update").checked,
    dlShown: !!el("set-dl") && !el("set-dl").hidden,
    permShown: !!el("set-perm-panel") && !el("set-perm-panel").hidden,
    badge: (document.querySelector("#set-latest .set-badge") || {}).textContent || null,
    calls: (window.__calls || []).slice()
  };
})()`;

(async () => {
  await connect();
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 430, height: 900, deviceScaleFactor: 2, mobile: true,
  });

  // =============================================================== A. 有桥
  console.log("== A. 安卓 App 内（注入假桥）==");
  const inj = await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB });
  await goto("/settings/?pass=a");
  let s = await evalJs(snap);

  check("环境提示说已连上 App", typeof s.env === "string" && s.env.indexOf("安卓 App") !== -1, s.env);
  check("设置区解开显示", s.nativeShown);
  check("「只在 App 内生效」块隐藏", !s.webShown);
  check("当前版本来自桥（v1.4）", s.cur === "v1.4", s.cur);
  check("最新版本来自 /api/app（v9.9）", typeof s.latest === "string" && s.latest.indexOf("9.9") !== -1, s.latest);
  check("标出「有新版本」", s.badge === "有新版本", s.badge);
  check("露出「下载并安装」按钮", s.dlShown);
  check("未授权时露出安装权限卡片", s.permShown);
  check("提示文案说可以下载安装",
    typeof s.msg === "string" && s.msg.indexOf("可以下载安装") !== -1, s.msg);
  check("开关初值取自桥（默认关）", s.checked === false);

  // ---- 自动更新开关
  await evalJs(`document.getElementById("auto-update").click()`);
  await sleep(250);
  s = await evalJs(snap);
  check("点开关 → 调了 setAutoUpdate(true)",
    s.calls.indexOf("setAutoUpdate:true") !== -1, s.calls);
  check("开关变成已选", s.checked === true);
  check("开关动作给了反馈且是成功态",
    s.msgClass.indexOf("is-ok") !== -1 && s.msg.indexOf("已开启") !== -1, s.msg);

  // ---- 再点一次应当关掉（开关必须能双向）
  await evalJs(`document.getElementById("auto-update").click()`);
  await sleep(250);
  s = await evalJs(snap);
  check("再点一次 → setAutoUpdate(false)",
    s.calls.indexOf("setAutoUpdate:false") !== -1, s.calls);
  check("开关回到未选", s.checked === false);

  // ---- 检查更新
  await evalJs(`document.getElementById("set-check").click()`);
  await sleep(400);
  s = await evalJs(snap);
  check("点「检查更新」→ 调了 checkForUpdate",
    s.calls.indexOf("checkForUpdate") !== -1, s.calls);

  // ---- ⭐ 未授权时点下载，必须**转去授权**而不是硬下。
  //      这是产品规则（硬下必然在安装那一步失败，用户只会看到"更新没反应"），
  //      所以先验它，再去授权验正常路径。
  await evalJs(`document.getElementById("set-dl").click()`);
  await sleep(300);
  s = await evalJs(snap);
  check("⭐ 未授权时点下载 → 转去授权页（而不是硬下）",
    s.calls.indexOf("openInstallSettings") !== -1, s.calls);
  check("未授权时给出明确原因",
    s.msg.indexOf("安装未知应用") !== -1, s.msg);
  check("未授权时**没有**真的发起下载",
    s.calls.indexOf("downloadUpdate") === -1, s.calls);

  // ---- 权限卡片上的按钮
  await evalJs(`document.getElementById("set-perm").click()`);
  await sleep(250);
  s = await evalJs(snap);
  check("点「去授权」→ 调了 openInstallSettings",
    s.calls.filter((c) => c === "openInstallSettings").length >= 2, s.calls);

  // ---- 授权完成后（用户从系统设置页回来），权限卡片应当自己收起
  await evalJs(`(() => {
    window.__grant();
    document.dispatchEvent(new Event("visibilitychange"));
  })()`);
  await sleep(250);
  s = await evalJs(snap);
  check("授权后权限卡片自动收起", !s.permShown);

  // ---- 现在才验正常下载路径
  await evalJs(`document.getElementById("set-dl").click()`);
  await sleep(300);
  s = await evalJs(snap);
  check("授权后点「下载并安装」→ 调了 downloadUpdate",
    s.calls.indexOf("downloadUpdate") !== -1, s.calls);
  check("下载给出了进行中提示",
    s.msgClass.indexOf("is-busy") !== -1 && s.msg.indexOf("下载") !== -1, s.msg);

  // =============================================================== B. 无桥
  console.log("\n== B. 浏览器里（不给桥）==");
  await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inj.identifier });
  await goto("/settings/?pass=b");
  s = await evalJs(snap);
  check("设置区**不**显示（不画假开关）", !s.nativeShown);
  check("「只在 App 内生效」块显示", s.webShown);
  check("环境提示点明是浏览器",
    typeof s.env === "string" && s.env.indexOf("浏览器") !== -1, s.env);
  check("提示带警告态", s.envWarn);

  // =============================================================== C. 无异常
  console.log("\n== C. 页面无 JS 异常 ==");
  check("全程没有未捕获异常", pageErrors.length === 0,
    pageErrors.length ? pageErrors.slice(0, 3) : undefined);

  const bad = results.filter((r) => !r.ok);
  console.log("\n" + "=".repeat(52));
  console.log(`共 ${results.length} 项，失败 ${bad.length} 项`);
  if (bad.length) {
    bad.forEach((b) => console.log("  失败：" + b.name));
    process.exit(1);
  }
  console.log("设置页：全部通过");
  process.exit(0);
})().catch((e) => {
  console.error("探针异常：" + (e && e.stack ? e.stack : e));
  process.exit(2);
});
