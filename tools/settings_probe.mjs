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

/* ---------------- 给 STUB **补上**提醒能力 ----------------
   刻意做成"补丁"而不是另写一份完整桥：设置页探针的前半段就是要验
   **没有提醒能力的旧壳**（用户手机上先看到新页面的那个状态），
   所以两种壳必须在同一次运行里都走一遍。

   状态从 window.__remind 读，可在页面里用 window.__setRemind() 改 ——
   于是"原生喊一声 → 页面重画"这条链是真的被验证过，而不是只看代码。 */
const STUB_REMIND = `(() => {
  if (!window.RadarNative) return;
  window.__remind = { notify: false, exact: false, pending: true,
                      when: "", count: 0, titles: "" };
  window.__setRemind = (o) => { Object.assign(window.__remind, o); };
  Object.assign(window.RadarNative, {
    reminderInfo: () => JSON.stringify(window.__remind),
    requestNotify: () => window.__calls.push("requestNotify"),
    openExactAlarmSettings: () => window.__calls.push("openExactAlarmSettings"),
    autoStartVendor: () => "小米",
    openAutoStartSettings: () => window.__calls.push("openAutoStartSettings"),
    testNotify: () => { window.__calls.push("testNotify"); return true; }
  });
})();`;

/* ---------------- 给 STUB **补上**每日简报能力 ----------------
   和提醒那份同一个套路：做成补丁，因为前半段要验**没有简报能力的旧壳**
   —— 页面先更新、App 后更新，这个状态在用户手机上必然会出现。

   三个时段的初值刻意**不都一样**：早间开着且已排程、尾盘关着、收盘开着但
   "还没排到"（when 为空）。这样三种显示状态（「下一次 …」/「已关闭」/
   「已开启，稍后重排」）能在同一次运行里都被看见 —— 尤其是最后那种：
   原生说"还没排"的时候，网页**绝不能自己算一个时刻填上去**，
   算了就必然和闹钟对不上，而对不上的表现是"设置页写着 8:00、实际没响"。 */
const STUB_BRIEF = `(() => {
  if (!window.RadarNative) return;
  const calls = window.__calls;
  window.__brief = {
    notify: false, exact: false, anyOn: true,
    slots: [
      { key: "morning",  label: "早间", on: true,  h: 8,  m: 0,  when: "9月20日 08:00" },
      { key: "intraday", label: "尾盘", on: false, h: 14, m: 30, when: "" },
      { key: "close",    label: "收盘", on: true,  h: 16, m: 40, when: "" }
    ]
  };
  window.__testCode = "ok";
  window.__briefCalls = [];
  Object.assign(window.RadarNative, {
    briefInfo: () => JSON.stringify(window.__brief),
    /* ⭐ 桩必须**真的把写入吞进去**，不能只记一笔就完事。
       真实原生存住之后会重排，下一次读到的就是新时刻；桩要是只记事不生效，
       页面里"存住之后从原生重读"这条纪律就验不出来 —— 而那正是这块的核心：
       页面上显示的每一个值都得能从原生再读出来一遍。 */
    setBrief: (json) => {
      calls.push("setBrief");
      window.__briefCalls.push(json);
      try {
        const o = JSON.parse(json);
        Object.keys(o).forEach((k) => {
          const s = (window.__brief.slots || []).filter((x) => x.key === k)[0];
          if (s) { s.on = !!o[k].on; s.h = o[k].h; s.m = o[k].m; }
        });
      } catch (e) {}
      return true;
    },
    testBrief: (slot) => {
      calls.push("testBrief:" + slot);
      /* ⭐ 在**调用当中**抓两个事实：按钮有没有禁用、状态行说的是不是
         "正在取…"。取简报要连服务器十几秒，这期间没反馈就等于点了没反应。
         同步桩里这两件事发生完就没了，只能在里面抓。 */
      const b = document.getElementById("brief-test");
      window.__busy = { disabled: !!(b && b.disabled),
                        msg: (document.getElementById("set-msg") || {}).textContent || "" };
      return window.__testCode;
    }
  });
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

  // =============================================================== C. 规划提醒
  //
  // 两种壳都要验：
  //   · **旧壳**（桥上没有提醒那几个方法）—— 就是用户手机上还会装的 v1.6。
  //     页面先更新、App 后更新，这个状态**必然会出现**，而且最容易被写成
  //     "拿不到信息 → 当成没权限"，把一个跟问题无关的引导摆到用户面前。
  //   · 新壳 —— 状态行、按钮、以及原生喊一声就重画（RadarReminderRefresh）。
  console.log("\n== C. 规划提醒 ==");

  const snapRemind = `(() => {
    const t = (id) => { const e = document.getElementById(id); return e ? e.textContent : null; };
    const el = (id) => document.getElementById(id);
    const vis = (id) => !!el(id) && !el(id).hidden;
    return {
      next: t("rem-next"), notify: t("rem-notify"), exact: t("rem-exact"),
      msg: t("rem-msg"), setMsg: t("set-msg"),
      permShown: vis("rem-perm"), exactShown: vis("rem-exact-btn"),
      bootShown: vis("rem-boot"), testShown: vis("rem-test"),
      calls: (window.__calls || []).slice()
    };
  })()`;

  // ---- C1. 旧壳：拿不到提醒信息
  await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB });
  await goto("/settings/?pass=c1");
  let r = await evalJs(snapRemind);
  check("旧壳：明说 App 是旧版本，而不是按「没权限」误报",
    typeof r.msg === "string" && r.msg.indexOf("旧版本") !== -1, r.msg);
  check("旧壳：说清了更新之后就有", r.msg.indexOf("更新") !== -1, r.msg);
  check("旧壳：不摆点了没用的按钮",
    !r.permShown && !r.exactShown && !r.bootShown && !r.testShown);
  check("旧壳：三个状态位显示为未知（—），不编数字",
    r.next === "—" && r.notify === "—" && r.exact === "—", [r.next, r.notify, r.exact]);

  // ---- C2. 有提醒能力的壳
  const inj3 = await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB_REMIND });
  await goto("/settings/?pass=c2");
  r = await evalJs(snapRemind);
  check("下一次：没排到时说「暂时没有安排」（而不是留个空白）",
    r.next === "暂时没有安排", r.next);
  check("通知权限如实显示未开启", r.notify === "未开启", r.notify);
  check("准点提醒如实显示未开启", r.exact === "未开启", r.exact);
  check("没通知权限 → 摆出开启按钮", r.permShown);
  check("没准点权限 → 摆出准点按钮", r.exactShown);
  check("小米这类系统 → 摆出自启动引导", r.bootShown);
  check("说明里点出「通知权限没开」", r.msg.indexOf("通知权限") !== -1, r.msg);

  await evalJs(`document.getElementById("rem-perm").click()`);
  await sleep(200);
  r = await evalJs(snapRemind);
  check("点「开启通知权限」→ 调了 requestNotify",
    r.calls.indexOf("requestNotify") !== -1, r.calls);

  await evalJs(`document.getElementById("rem-exact-btn").click()`);
  await sleep(150);
  await evalJs(`document.getElementById("rem-boot").click()`);
  await sleep(150);
  r = await evalJs(snapRemind);
  check("点「开启准点提醒」→ 调了 openExactAlarmSettings",
    r.calls.indexOf("openExactAlarmSettings") !== -1, r.calls);
  check("点「去设置自启动」→ 调了 openAutoStartSettings",
    r.calls.indexOf("openAutoStartSettings") !== -1, r.calls);

  // ---- 测试提醒：一按就要有明确结果（不能只靠"弹没弹"判断）
  await evalJs(`document.getElementById("rem-test").click()`);
  await sleep(250);
  r = await evalJs(snapRemind);
  check("点「发一条测试提醒」→ 调了 testNotify",
    r.calls.indexOf("testNotify") !== -1, r.calls);
  check("并且给了一句明确的反馈",
    typeof r.setMsg === "string" && r.setMsg.indexOf("测试提醒") !== -1, r.setMsg);

  // ---- ⭐ 原生从系统设置页回来时会喊的那一声：window.RadarReminderRefresh()
  //      设置页必须**重新去读**原生，而不是把旧状态留在屏幕上。
  await evalJs(`
    window.__setRemind({ notify: true, exact: true, pending: true,
                         when: "10月1日 09:00", count: 2, titles: "A、B" });
    window.RadarReminderRefresh();
  `);
  await sleep(200);
  r = await evalJs(snapRemind);
  check("⭐ 原生喊一声后，状态行重画（下一次 + 条数）",
    r.next === "10月1日 09:00（2 条）", r.next);
  check("权限齐备后两个按钮都收起", !r.permShown && !r.exactShown);
  check("准点权限也有了 → 说明里不再提「可能晚一会儿」",
    r.msg.indexOf("晚一会儿") === -1, r.msg);
  check("仍然如实提示自启动这件事（那是系统层面的限制）",
    r.msg.indexOf("自启动") !== -1, r.msg);

  await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inj3.identifier });

  // =============================================================== C3. 每日简报
  //
  // 同样两种壳都要验。这块比规划提醒多一层风险：时刻是原生存的，
  // 网页只许**显示和回写** —— 一旦网页自己算一遍，就必然和闹钟对不上，
  // 而对不上的表现是"设置页写着 8:00、实际没响"，属于最难查的那类。
  console.log("\n== C3. 每日简报（三个时段）==");

  const snapBrief = `(() => {
    const el = (id) => document.getElementById(id);
    const t = (id) => { const e = el(id); return e ? e.textContent : null; };
    const vis = (id) => !!el(id) && !el(id).hidden;
    const rows = [].slice.call(document.querySelectorAll("#brief-rows .brief-row"));
    const out = { slots: [], at: {}, on: {}, when: {}, off: {}, label: {} };
    rows.forEach((r) => {
      const k = r.getAttribute("data-slot");
      out.slots.push(k);
      const a = el("brief-at-" + k), c = el("brief-on-" + k);
      out.at[k] = a ? a.value : null;
      out.on[k] = c ? c.checked : null;
      out.when[k] = t("brief-when-" + k);
      out.label[k] = (r.querySelector(".set-name") || {}).textContent || null;
      out.off[k] = r.classList.contains("is-off");
    });
    const all = [].slice.call(document.querySelectorAll("#brief-rows .brief-row"));
    return {
      slots: out.slots, at: out.at, on: out.on, when: out.when,
      off: out.off, label: out.label, rowCount: all.length,
      msg: t("brief-msg"), setMsg: t("set-msg"),
      setMsgClass: el("set-msg") ? el("set-msg").className : "",
      testShown: vis("brief-test"),
      permShown: vis("brief-perm"), exactShown: vis("brief-exact"),
      busy: window.__busy || null,
      calls: (window.__calls || []).slice(),
      briefCalls: (window.__briefCalls || []).slice()
    };
  })()`;

  // ---- C3a. 旧壳：桥上没有 briefInfo（就是用户手机上还会装的 v1.7）
  const inj4 = await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB });
  await goto("/settings/?pass=c3a");
  let b = await evalJs(snapBrief);
  check("简报旧壳：明说是 App 旧版本，而不是说「没权限」",
    typeof b.msg === "string" && b.msg.indexOf("旧版本") !== -1, b.msg);
  check("简报旧壳：说清了更新之后就有", b.msg.indexOf("更新") !== -1, b.msg);
  check("简报旧壳：一行都不画（不摆点了没用的开关）",
    b.rowCount === 0, b.rowCount);
  check("简报旧壳：测试/权限/准点三个按钮全收起",
    !b.testShown && !b.permShown && !b.exactShown,
    [b.testShown, b.permShown, b.exactShown]);

  // ---- C3b. 有简报能力的壳
  const inj5 = await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB_REMIND });
  const inj6 = await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB_BRIEF });
  await goto("/settings/?pass=c3b");
  b = await evalJs(snapBrief);
  check("三行都画出来了，顺序就是早/尾/收",
    b.slots.join(",") === "morning,intraday,close", b.slots);
  check("行数正好 3（没有多画一遍）", b.rowCount === 3, b.rowCount);
  check("时段名来自原生",
    b.label.morning === "早间简报" && b.label.intraday === "尾盘简报"
      && b.label.close === "收盘简报", b.label);
  check("⭐ 时刻来自原生，网页不许自己算（08:00 / 14:30 / 16:40）",
    b.at.morning === "08:00" && b.at.intraday === "14:30" && b.at.close === "16:40",
    b.at);
  check("开关状态来自原生",
    b.on.morning === true && b.on.intraday === false && b.on.close === true, b.on);
  check("关掉的那一档整行压暗", b.off.intraday === true && b.off.morning === false,
    b.off);
  check("关掉的那一档写「已关闭」", b.when.intraday === "已关闭", b.when.intraday);
  check("已排程的那一档写「下一次 …」",
    b.when.morning === "下一次 9月20日 08:00", b.when.morning);
  check("⭐ 原生说「还没排到」时不许自己编一个时刻",
    b.when.close === "已开启，稍后重排", b.when.close);
  check("没通知权限 → 摆出开启按钮", b.permShown);
  check("说明里点出「通知权限没开」", b.msg.indexOf("通知权限") !== -1, b.msg);
  check("有自启动限制就一并说（那是系统层面的限制）",
    b.msg.indexOf("自启动") !== -1, b.msg);

  // ---- ⭐ 三行只画一次：重画不许重建节点（重建会把正在输入的时刻打断）
  await evalJs(`window.__row0 = document.querySelector("#brief-rows .brief-row")`);
  await evalJs(`window.RadarReminderRefresh()`);
  await sleep(120);
  const sameRow = await evalJs(
    `document.querySelector("#brief-rows .brief-row") === window.__row0`);
  b = await evalJs(snapBrief);
  check("⭐ 重画复用同一批节点（正在输入的时刻不会被打断）", sameRow === true, sameRow);
  check("重画也不会多出一行", b.rowCount === 3, b.rowCount);

  // ---- 改时刻：必须落回原生，而且带的是新时刻
  await evalJs(`(() => {
    const a = document.getElementById("brief-at-close");
    a.value = "17:05";
    a.dispatchEvent(new Event("change", { bubbles: true }));
  })()`);
  await sleep(150);
  b = await evalJs(snapBrief);
  let last = b.briefCalls.length ? JSON.parse(b.briefCalls[b.briefCalls.length - 1]) : {};
  check("改时刻 → 调了 setBrief", b.briefCalls.length >= 1, b.briefCalls.length);
  check("⭐ 回写的是完整三档（只有一条写入路径，没有改一半的状态）",
    b.briefCalls.length >= 1
      && ["morning", "intraday", "close"].every((k) => last[k]), last);
  check("回写的时刻就是刚改的那个（收盘 17:05）",
    last.close && last.close.h === 17 && last.close.m === 5, last.close);
  check("改完给了「已保存」反馈",
    typeof b.setMsg === "string" && b.setMsg.indexOf("已保存") !== -1
      && b.setMsgClass.indexOf("is-ok") !== -1, [b.setMsg, b.setMsgClass]);

  // ---- ⭐ 清空时刻框：绝不能当成 0:00 存进去（那会变成半夜弹一条）
  const before = b.briefCalls.length;
  await evalJs(`(() => {
    const a = document.getElementById("brief-at-close");
    a.value = "";
    a.dispatchEvent(new Event("change", { bubbles: true }));
  })()`);
  await sleep(150);
  b = await evalJs(snapBrief);
  check("⭐ 时刻框清空 → 不写进原生（否则会变成 0:00 半夜弹一条）",
    b.briefCalls.length === before, [before, b.briefCalls.length]);
  check("⭐ 清空时如实说「时刻填得不对」，不假装存住了",
    typeof b.setMsg === "string" && b.setMsg.indexOf("时刻") !== -1
      && b.setMsgClass.indexOf("is-warn") !== -1, [b.setMsg, b.setMsgClass]);
  check("⭐ 清空后从原生重读，输入框回到原来的 17:05（不留在空框上）",
    b.at.close === "17:05", b.at.close);

  // ---- 关掉一档
  await evalJs(`document.getElementById("brief-on-morning").click()`);
  await sleep(150);
  b = await evalJs(snapBrief);
  last = JSON.parse(b.briefCalls[b.briefCalls.length - 1]);
  check("关掉早间 → 回写里早间 on=false 且其他档不变",
    last.morning && last.morning.on === false
      && last.intraday && last.intraday.on === false
      && last.close && last.close.on === true, last);
  check("关掉后那一行立刻压暗", b.off.morning === true, b.off);
  check("关掉后那一行写「已关闭」", b.when.morning === "已关闭", b.when.morning);

  await evalJs(`document.getElementById("brief-on-close").click()`);
  await sleep(150);
  b = await evalJs(snapBrief);
  check("三档全关时说明改口（不再说「到点会收到一条」）",
    typeof b.msg === "string" && b.msg.indexOf("三档都关着") !== -1, b.msg);

  // ---- 「立刻取一条看看」
  await evalJs(`document.getElementById("brief-on-morning").click()`);
  await sleep(150);
  b = await evalJs(snapBrief);
  check("重新打开早间后又是开着的", b.on.morning === true);
  await evalJs(`document.getElementById("brief-test").click()`);
  await sleep(200);
  b = await evalJs(snapBrief);
  check("点测试 → 取的是**第一个开着**的时段（早间，不是收盘）",
    b.calls.indexOf("testBrief:morning") !== -1, b.calls);
  check("⭐ 取的过程中按钮禁用（十几秒没反馈 = 用户以为点了没反应）",
    !!b.busy && b.busy.disabled === true, b.busy);
  check("⭐ 取的过程中状态行说「正在取…」",
    !!b.busy && b.busy.msg.indexOf("正在取") !== -1, b.busy && b.busy.msg);
  check("成功后告诉用户去通知栏看",
    typeof b.setMsg === "string" && b.setMsg.indexOf("通知栏") !== -1
      && b.setMsgClass.indexOf("is-ok") !== -1, [b.setMsg, b.setMsgClass]);
  check("取完按钮恢复可用", b.testShown === true);

  // ---- 结果码要逐个说人话（四种结果不能都显示成同一句）
  const codes = [
    ["nonotify", "没开", "is-warn"],
    ["nofetch", "没取到", "is-warn"],
    ["dup", "已经发过", "is-ok"],
  ];
  for (const [code, word, cls] of codes) {
    await evalJs(`window.__testCode = ${JSON.stringify(code)}`);
    await evalJs(`document.getElementById("brief-test").click()`);
    await sleep(200);
    b = await evalJs(snapBrief);
    check(`结果码 ${code} → 说「${word}」`,
      typeof b.setMsg === "string" && b.setMsg.indexOf(word) !== -1
        && b.setMsgClass.indexOf(cls) !== -1, [b.setMsg, b.setMsgClass]);
  }

  // ---- 权限齐备：两个引导按钮自己收起，说明也不再提「可能晚一会儿」
  await evalJs(`
    window.__testCode = "ok";
    Object.assign(window.__brief, { notify: true, exact: true });
    window.RadarReminderRefresh();
  `);
  await sleep(150);
  b = await evalJs(snapBrief);
  check("通知/准点权限齐备 → 两个引导按钮都收起",
    !b.permShown && !b.exactShown, [b.permShown, b.exactShown]);
  check("说明改成「到点会去服务器取一次」",
    typeof b.msg === "string" && b.msg.indexOf("服务器") !== -1, b.msg);
  check("权限齐备后不再提「通知权限没开」",
    b.msg.indexOf("通知权限没开") === -1, b.msg);

  await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inj4.identifier });
  await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inj5.identifier });
  await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: inj6.identifier });

  // =============================================================== D. 无异常
  console.log("\n== D. 页面无 JS 异常 ==");
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
