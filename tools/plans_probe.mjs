// plans_probe.mjs —— 用无头 Chrome 真跑一遍「个人规划」弹窗。
//
// 为什么必须有这个脚本：面板是**纯运行时**的 —— HTML 里一个规划条目都没有，
// 全部由 app.js 从原生存储读回来再画。所以"源码里有 setupPlans 这个函数名"
// 证明不了任何事。真正要验的是：
//   ① 打开后列表真的画出来了、排序对不对、计数对不对
//   ② 每次改动**真的落盘**了（看假桥收到了什么），而不是只改了内存
//   ③ 关掉再打开，内容**还在**（这是"真的存住了"唯一的证据）
//   ④ 存**失败**时页面上看得见 —— 这是本项目最在意的一条
//   ⑤ 删除的撤销真的能把东西放回去
//   ⑥ 浏览器里（没有桥）**不放假输入框**，只给说明
//   ⑦ 用户输入不会被当成 HTML 执行（textContent 而不是 innerHTML）
//
// 用法：bash tools/run_ui_probe.sh tools/plans_probe.mjs
//
// ⚠️ 两个刻意的设计：
//   1. 假桥用 Page.addScriptToEvaluateOnNewDocument 注入 —— 必须在页面脚本
//      之前就存在，否则 setupPlans 跑的时候 window.RadarNative 还是 undefined，
//      页面会静静走进"浏览器分支"，于是所有 App 断言全错。
//   2. 假桥里的 setPlans **真的把串存起来**，getPlans 再读回来。
//      只有这样，"关掉再打开内容还在"才是有意义的断言 ——
//      否则只是证明了内存里那个数组没被清掉。

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
  await sleep(600);
}

/* ---------------- 假的安卓桥 ----------------
   刻意做到"能真的存住"：
     getPlans 读的是 setPlans 上一次写进去的串 —— 于是"关掉再打开内容还在"
     才是真断言。另外记下每一次调用，好断"改动到底有没有落盘"。
   留了两个开关给测试用：
     window.__failNextSave()  让下一次保存返回 false（模拟存储写不进去）
     window.__callsOf(name)   取出某个方法的调用记录 */
const STUB = `(() => {
  const calls = [];
  const st = { plans: "", bytes: 0, failNext: false };
  /* 提醒相关的状态全部可调 —— 好在一次运行里把"四种状态"都走一遍。
     默认给一个"什么都齐备、有一条 10 月 1 日到期的规划"的场面。 */
  st.remind = { notify: true, exact: true, pending: true,
                when: "10月1日 09:00", count: 1, titles: "读完 vLLM Scheduler 源码" };
  st.vendor = "小米";
  st.testOk = true;
  window.__calls = calls;
  window.__stub = st;
  window.__failNextSave = () => { st.failNext = true; };
  window.__callsOf = (n) => calls.filter(c => c[0] === n);
  window.__setRemind = (o) => { Object.assign(st.remind, o); };
  window.__setVendor = (v) => { st.vendor = v; };
  window.RadarNative = {
    versionName: () => "9.9",
    versionCode: () => 99,
    checkForUpdate: () => calls.push(["checkForUpdate"]),
    getAutoUpdate: () => false,
    setAutoUpdate: () => {},
    canInstall: () => true,
    openInstallSettings: () => {},
    downloadUpdate: () => {},
    getPlans: () => { calls.push(["getPlans"]); return st.plans; },
    setPlans: (json) => {
      calls.push(["setPlans", json]);
      if (st.failNext) { st.failNext = false; return false; }
      st.plans = json;
      st.bytes = json.length;
      return true;
    },
    plansBytes: () => st.bytes,
    reminderInfo: () => { calls.push(["reminderInfo"]); return JSON.stringify(st.remind); },
    canNotify: () => !!st.remind.notify,
    requestNotify: () => calls.push(["requestNotify"]),
    openNotifySettings: () => calls.push(["openNotifySettings"]),
    exactAlarmAllowed: () => !!st.remind.exact,
    openExactAlarmSettings: () => calls.push(["openExactAlarmSettings"]),
    autoStartVendor: () => st.vendor,
    openAutoStartSettings: () => calls.push(["openAutoStartSettings"]),
    testNotify: () => { calls.push(["testNotify"]); return st.testOk; },
  };
})();`;

/* ⭐ 原生侧真正会去执行的那串握手表达式（MainActivity.JS_OPEN_PLANS）。
   这里**逐字照抄**，不图省事改写成别的形式：
   它验的就是"原生那一侧调进来会发生什么"，改一个字符验的就不是同一件事了。
   返回 "ok" 才算掀面板成功；返回 "no" 时原生会先回首页再试。 */
const HOOK_JS = "(function(){"
  + "if(typeof window.RadarPlansOpen==='function'){window.RadarPlansOpen();return 'ok';}"
  + "return 'no';})()";

const NO_BRIDGE = `(() => { delete window.RadarNative; })();`;

/* 旧壳：桥在，但**没有提醒那几个方法** —— 就是用户手机上还会装着的 v1.6。
   这个状态一定会出现（页面先更新、App 后更新），所以必须单独验一遍：
   拿不到提醒信息时，页面**不能**按"没权限"报，也不能画一个点了没反应的按钮。 */
const LEGACY_BRIDGE = `(() => {
  if (!window.RadarNative) return;
  delete window.RadarNative.reminderInfo;
  delete window.RadarNative.canNotify;
  delete window.RadarNative.requestNotify;
  delete window.RadarNative.autoStartVendor;
  delete window.RadarNative.testNotify;
})();`;

await connect();
await send("Runtime.enable");
await send("Page.enable");

/* ==========================================================================
   一、App 分支：有原生桥
   ========================================================================== */
console.log("\n—— 一、App 内（注入假桥）——");
await send("Page.addScriptToEvaluateOnNewDocument", { source: STUB });
await goto("/");

check("入口图标出现（App 内）", await evalJs(`!document.getElementById('plans-btn').hidden`));
check("面板初始是隐藏的", await evalJs(`document.getElementById('plans-modal').hidden === true`));
check("看板存在（确认是 App 分支）", await evalJs(`typeof window.RadarNative === 'object'`));

await evalJs(`document.getElementById('plans-btn').click()`);
await sleep(400);
check("点击后弹出面板", await evalJs(`document.getElementById('plans-modal').hidden === false`));
check("面板带入场动画类", await evalJs(`document.getElementById('plans-modal').classList.contains('is-in')`));
check("打开时锁住背景滚动", await evalJs(`document.body.classList.contains('plans-open')`));
check("显示 App 分支", await evalJs(`document.getElementById('plans-app').hidden === false`));
check("不显示浏览器说明卡", await evalJs(`document.getElementById('plans-web').hidden === true`));
check("首次打开走空态文案", await evalJs(
  `document.getElementById('plan-list').children.length === 0
   && !document.getElementById('plan-empty').hidden
   && document.getElementById('plan-empty').textContent.indexOf('还没有规划') >= 0`));
check("读取调用真的发生了", await evalJs(`window.__callsOf('getPlans').length >= 1`));

/* ---- 新增 ---- */
console.log("\n  · 新增");
const urlBefore = await evalJs(`location.href`);
await evalJs(`
  document.getElementById('plan-title').value = '读完 vLLM Scheduler 源码';
  document.getElementById('plan-due').value = '2026-10-01';
  document.getElementById('plan-memo').value = 'M1-W1';
  document.getElementById('plan-add').click();
`);
await sleep(300);
check("提交表单没有让页面跳走（preventDefault 生效）", (await evalJs(`location.href`)) === urlBefore);
check("列表出现 1 条", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 1`));
check("标题正确", await evalJs(`document.querySelector('#plan-list .plan-t').textContent === '读完 vLLM Scheduler 源码'`));
check("日期渲染成 MM-DD", await evalJs(`document.querySelector('#plan-list .plan-date').textContent === '10-01'`));
check("备注渲染出来了", await evalJs(`document.querySelector('#plan-list .plan-memo').textContent === 'M1-W1'`));
check("空态收起了", await evalJs(`document.getElementById('plan-empty').hidden === true`));
check("计数正确", await evalJs(`document.getElementById('plan-count').textContent === '未完成 1 · 已完成 0'`));
check("输入框被清空（可以接着记下一条）", await evalJs(
  `document.getElementById('plan-title').value === '' && document.getElementById('plan-memo').value === ''`));
check("改动**真的落盘**了（setPlans 收到含该条的 JSON）", await evalJs(`
  (() => {
    const cs = window.__callsOf('setPlans');
    if (!cs.length) return false;
    try {
      const o = JSON.parse(cs[cs.length - 1][1]);
      return o.items.length === 1 && o.items[0].t === '读完 vLLM Scheduler 源码'
             && o.items[0].due === '2026-10-01' && o.items[0].done === false;
    } catch (e) { return false; }
  })()`));

await evalJs(`
  document.getElementById('plan-title').value = '把 5070 装上跑通 vLLM';
  document.getElementById('plan-add').click();
`);
await sleep(250);
check("能连着加第二条", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 2`));

/* ---- 完成 / 排序 ---- */
console.log("\n  · 完成与排序");
await evalJs(`document.querySelectorAll('#plan-list .plan-check')[0].click()`);
await sleep(250);
check("勾选后进入完成态", await evalJs(`document.querySelectorAll('#plan-list .plan-item.is-done').length === 1`));
check("完成后计数更新", await evalJs(`document.getElementById('plan-count').textContent === '未完成 1 · 已完成 1'`));
check("已完成项**沉到列表底部**（未完成在前）", await evalJs(`
  document.querySelectorAll('#plan-list .plan-item')[0].classList.contains('is-done') === false
  && document.querySelectorAll('#plan-list .plan-item')[1].classList.contains('is-done') === true`));
check("已完成项**仍然显示**（不隐藏 —— 划掉本身是正反馈）", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 2`));
check("出现「隐藏已完成」按钮", await evalJs(`document.getElementById('plan-hide-done').hidden === false`));
check("完成状态也落盘了", await evalJs(`
  (() => {
    const cs = window.__callsOf('setPlans');
    const o = JSON.parse(cs[cs.length - 1][1]);
    return o.items.filter(x => x.done).length === 1;
  })()`));

await evalJs(`document.getElementById('plan-hide-done').click()`);
await sleep(200);
check("隐藏已完成后只剩 1 条", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 1`));
check("按钮文案变成「显示已完成」", await evalJs(`document.getElementById('plan-hide-done').textContent === '显示已完成'`));
await evalJs(`document.getElementById('plan-hide-done').click()`);
await sleep(200);
check("再点回来显示 2 条", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 2`));

/* ---- 删除 + 撤销 ---- */
console.log("\n  · 删除与撤销");
await evalJs(`document.querySelectorAll('#plan-list .plan-del')[0].click()`);
await sleep(250);
check("删除立刻生效", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 1`));
check("出现撤销条", await evalJs(`document.getElementById('plan-undo').hidden === false`));
check("撤销条写明了删的是哪条", await evalJs(
  `document.getElementById('plan-undo-txt').textContent.indexOf('已删除') === 0`));
check("删除也落盘了", await evalJs(`
  (() => {
    const cs = window.__callsOf('setPlans');
    return JSON.parse(cs[cs.length - 1][1]).items.length === 1;
  })()`));

await evalJs(`document.getElementById('plan-undo-btn').click()`);
await sleep(250);
check("撤销把条目放回来了", await evalJs(`document.querySelectorAll('#plan-list .plan-item').length === 2`));
check("撤销后撤销条收起", await evalJs(`document.getElementById('plan-undo').hidden === true`));

/* ---- 关掉再打开：数据还在吗（真正证明"存住了"）---- */
console.log("\n  · 关掉再打开");
await evalJs(`document.querySelector('#plans-modal .plans-x').click()`);
await sleep(400);
check("点 × 能关闭", await evalJs(`document.getElementById('plans-modal').hidden === true`));
check("关闭后解除背景滚动锁", await evalJs(`document.body.classList.contains('plans-open') === false`));

await evalJs(`document.getElementById('plans-btn').click()`);
await sleep(400);
check("重开后内容还在（真的从存储读回来的）", await evalJs(`
  document.querySelectorAll('#plan-list .plan-item').length === 2
  && (() => {
    const ts = [].map.call(document.querySelectorAll('#plan-list .plan-t'), e => e.textContent);
    return ts.indexOf('读完 vLLM Scheduler 源码') >= 0;
  })()`));
check("重开后完成态也还在", await evalJs(`document.querySelectorAll('#plan-list .plan-item.is-done').length === 1`));

/* ---- 保存失败必须看得见 ---- */
console.log("\n  · 保存失败");
await evalJs(`window.__failNextSave()`);
await evalJs(`
  document.getElementById('plan-title').value = '这条会存不进去';
  document.getElementById('plan-add').click();
`);
await sleep(300);
check("存失败时状态条露出来", await evalJs(`document.getElementById('plans-note').hidden === false`));
check("状态条是警告样式", await evalJs(`document.getElementById('plans-note').classList.contains('is-warn')`));
check("状态条明确说没存进去", await evalJs(
  `document.getElementById('plans-note').textContent.indexOf('没有存进手机') >= 0`));
check("失败提示不影响继续使用（面板还开着）", await evalJs(`document.getElementById('plans-modal').hidden === false`));

/* ---- 关闭方式 ---- */
console.log("\n  · 关闭方式");
await evalJs(`document.querySelector('#plans-modal .plans-veil').click()`);
await sleep(400);
check("点遮罩能关闭", await evalJs(`document.getElementById('plans-modal').hidden === true`));
await evalJs(`document.getElementById('plans-btn').click()`);
await sleep(350);
await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);
await sleep(400);
check("按 Esc 能关闭", await evalJs(`document.getElementById('plans-modal').hidden === true`));
check("再点入口是打开（不是又一次关闭）", await evalJs(`
  document.getElementById('plans-btn').click();
  document.getElementById('plans-modal').hidden === false`));

/* ---- 用户输入不许当 HTML 执行 ---- */
console.log("\n  · 输入安全");
await evalJs(`
  document.getElementById('plan-title').value = '<img src=x onerror="window.__xss=1">';
  document.getElementById('plan-add').click();
`);
await sleep(300);
check("标题里的 HTML 被当纯文本（没有生成 img 元素）", await evalJs(
  `document.querySelectorAll('#plan-list img').length === 0`));
check("注入的 onerror 没有被执行", await evalJs(`window.__xss === undefined`));
check("原文照原样显示出来", await evalJs(
  `[].some.call(document.querySelectorAll('#plan-list .plan-t'),
      e => e.textContent.indexOf('<img') === 0)`));

await evalJs(`document.querySelector('#plans-modal .plans-x').click()`);
await sleep(350);

check("App 分支：页面 JS 无异常", pageErrors.length === 0, pageErrors.length ? pageErrors : undefined);

/* ==========================================================================
   三、到期提醒（到期当天 9:00 的系统通知）
   提醒本身是**系统通知**，无头浏览器里弹不出来 —— 所以这里能验的、也最该验的
   是三件事：
     · 把那串"握手表达式"原样跑一遍：点通知进来时原生调的就是它，
       返回 "ok" 才算掀开了面板（返回 "no" 原生会先回首页再试）
     · 四种状态（权限齐备 / 没通知权限 / 没准点权限 / 还没定日期）下
       面板那一行说的是不是人话、按钮出没出来
     · 按钮点了真的会去调桥 —— 而不是摆个装饰
   ========================================================================== */
console.log("\n—— 二、到期提醒 ——");
pageErrors.length = 0;
await goto("/");

/* ---- 原生那条握手路径 ---- */
console.log("\n  · 点通知进来（原生握手）");
await evalJs(`
  if (!document.getElementById('plans-modal').hidden) {
    document.querySelector('#plans-modal .plans-x').click();
  }`);
await sleep(350);
check("握手前面板是关着的", await evalJs(`document.getElementById('plans-modal').hidden === true`));
check("⭐ 原生的握手表达式返回 ok", (await evalJs(HOOK_JS)) === "ok");
await sleep(400);
check("⭐ 面板真的被掀开了（这才是“点通知能进面板”的证据）",
  await evalJs(`document.getElementById('plans-modal').hidden === false`));
check("掀开的是 App 分支（不是浏览器说明卡）",
  await evalJs(`document.getElementById('plans-app').hidden === false`));
await evalJs(`document.querySelector('#plans-modal .plans-x').click()`);
await sleep(350);
check("已经开着时再握一次手不会把它关掉", await evalJs(`
  (() => {
    const r = ${HOOK_JS};
    return r === 'ok' && document.getElementById('plans-modal').hidden === false;
  })()`));
await evalJs(`document.querySelector('#plans-modal .plans-x').click()`);
await sleep(350);
/* 钩子不在的页面（理论上不会，但真出事时原生得能分辨出来）→ 必须是 "no" */
check("⭐ 钩子不在时报 no（原生据此先回首页再试）", await evalJs(`
  (() => {
    const saved = window.RadarPlansOpen;
    delete window.RadarPlansOpen;
    const r = ${HOOK_JS};
    window.RadarPlansOpen = saved;
    return r === 'no';
  })()`));

/* ---- 面板里那一行状态 ---- */
console.log("\n  · 面板里的提醒状态");
async function openPanel() {
  await evalJs(`
    if (document.getElementById('plans-modal').hidden) {
      document.getElementById('plans-btn').click();
    }`);
  await sleep(400);
}
async function closePanel() {
  await evalJs(`
    if (!document.getElementById('plans-modal').hidden) {
      document.querySelector('#plans-modal .plans-x').click();
    }`);
  await sleep(350);
}

await openPanel();
check("提醒状态行露出来了", await evalJs(`document.getElementById('plan-remind').hidden === false`));
check("状态行读的是原生给的事实", await evalJs(`window.__callsOf('reminderInfo').length >= 1`));
check("权限齐备时说清 9:00 提醒", await evalJs(
  `document.getElementById('plan-remind-txt').textContent.indexOf('9:00') >= 0`));
check("不需要操作时不摆按钮", await evalJs(`document.getElementById('plan-remind-btn').hidden === true`));

await closePanel();
await evalJs(`window.__setRemind({ notify: false })`);
await openPanel();
check("⭐ 没通知权限时明确说发不出来", await evalJs(
  `document.getElementById('plan-remind-txt').textContent.indexOf('通知权限没开') >= 0`));
check("并给一个按钮", await evalJs(
  `document.getElementById('plan-remind-btn').hidden === false
   && document.getElementById('plan-remind-btn').textContent === '开启通知'`));
const beforeReq = await evalJs(`window.__callsOf('requestNotify').length`);
await evalJs(`document.getElementById('plan-remind-btn').click()`);
await sleep(200);
check("⭐ 按钮真的去调 bridge 要权限（不是装饰）", await evalJs(
  `window.__callsOf('requestNotify').length === ${beforeReq} + 1`));

await closePanel();
await evalJs(`window.__setRemind({ notify: true, exact: false })`);
await openPanel();
check("没准点权限时如实说可能晚一会儿", await evalJs(
  `document.getElementById('plan-remind-txt').textContent.indexOf('晚一会儿') >= 0`));
check("这种情况不需要用户动手，不给按钮", await evalJs(
  `document.getElementById('plan-remind-btn').hidden === true`));

await closePanel();
await evalJs(`window.__setRemind({ exact: true, pending: false, when: "", count: 0 })`);
await openPanel();
check("还没有定日期的规划时引导去填日期", await evalJs(
  `document.getElementById('plan-remind-txt').textContent.indexOf('目标日期') >= 0`));

check("提醒状态行不依赖页面自己算的时刻", await evalJs(
  `window.__callsOf('reminderInfo').length >= 4`));
check("到期提醒：页面 JS 无异常", pageErrors.length === 0, pageErrors.length ? pageErrors : undefined);

await closePanel();

/* ==========================================================================
   三、旧壳：桥在、但没有提醒那几个方法
   —— 就是用户手机上还会装的 v1.6。页面先更新、App 后更新，这个状态必然出现。
   ========================================================================== */
console.log("\n—— 三、旧壳（桥没有提醒方法）——");
pageErrors.length = 0;
await send("Page.addScriptToEvaluateOnNewDocument", { source: LEGACY_BRIDGE });
await goto("/");
check("旧壳下看板还在（只是少了提醒的方法）",
  await evalJs(`typeof window.RadarNative === 'object'
                && typeof window.RadarNative.reminderInfo === 'undefined'`));
await openPanel();
check("面板照常能开", await evalJs(`document.getElementById('plans-modal').hidden === false`));
check("⭐ 拿不到提醒信息时**不显示**状态行（不猜、不误报）",
  await evalJs(`document.getElementById('plan-remind').hidden === true`));
check("旧壳下页面 JS 无异常（少了方法也不能崩）",
  pageErrors.length === 0, pageErrors.length ? pageErrors : undefined);
await evalJs(`document.querySelector('#plans-modal .plans-x').click()`);
await sleep(350);

/* ==========================================================================
   四、浏览器分支：没有原生桥
   ========================================================================== */
console.log("\n—— 四、浏览器（无桥）——");
pageErrors.length = 0;
await send("Page.addScriptToEvaluateOnNewDocument", { source: NO_BRIDGE });
await goto("/");

check("浏览器里入口**仍然显示**（好让人知道 App 里有这个功能）", await evalJs(`!document.getElementById('plans-btn').hidden`));
await evalJs(`document.getElementById('plans-btn').click()`);
await sleep(400);
check("打开后显示浏览器说明卡", await evalJs(`document.getElementById('plans-web').hidden === false`));
check("说明卡文案明确说只在 App 内可用", await evalJs(
  `document.getElementById('plans-web').textContent.indexOf('只在安卓 App 里可用') >= 0`));
check("**不显示** App 分支（没有假输入框）", await evalJs(`document.getElementById('plans-app').hidden === true`));
check("提醒状态行在浏览器里也不出现（网页端没有提醒这回事）",
  await evalJs(`document.getElementById('plan-remind').hidden === true`));
check("页面上没有可输入的规划框", await evalJs(`
  (() => {
    const el = document.getElementById('plan-title');
    if (!el) return true;
    return el.offsetParent === null;   // 不可见 —— 在 hidden 的容器里
  })()`));
/* 注意这里的口径：注入的两段脚本**都会执行**（STUB 先跑、NO_BRIDGE 再删掉
   RadarNative），所以 window.__callsOf 还在 —— 但那正是我们要的探针：
   它能回答"网页端有没有偷偷去读写本地存储"。真浏览器里连这些全局都没有。 */
check("网页端一次都没有试图读写本地存储", await evalJs(
  `window.__callsOf('getPlans').length === 0 && window.__callsOf('setPlans').length === 0`));
check("也没有冒出「保存失败」这类误导提示", await evalJs(
  `document.getElementById('plans-note').hidden === true`));
check("浏览器分支：页面 JS 无异常", pageErrors.length === 0, pageErrors.length ? pageErrors : undefined);

/* ==========================================================================
   汇总
   ========================================================================== */
const bad = results.filter((r) => !r.ok);
console.log(`\n${"—".repeat(56)}`);
console.log(`规划面板探针：${results.length - bad.length}/${results.length} 通过`);
if (pageErrors.length) {
  console.log("页面异常：");
  pageErrors.forEach((e) => console.log("  " + String(e).split("\n")[0]));
}
if (bad.length) {
  console.log("失败项：");
  bad.forEach((r) => console.log("  ✗ " + r.name));
  process.exit(1);
}
process.exit(0);
