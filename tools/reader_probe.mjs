// reader_probe.mjs —— 用无头 Chrome 真跑一遍「阅读位置 + 书签」。
//
// 为什么要有这个脚本：源码里有某个函数名，**证明不了功能是通的**。
// 用户的原话是"一旦我返回就找不到了"，所以这里就照着那个动作演一遍：
// 滚到中间 → 离开 → 回来 → 看有没有回到原位。
//
// 用法：
//   1) 起静态服务器：  python -m http.server 8099 --directory public
//   2) 起 Chrome：      chrome --headless=new --remote-debugging-port=9333 \
//                        --user-data-dir=<临时目录> --no-first-run about:blank
//   3) node tools/reader_probe.mjs [CDP地址] [站点地址]
//
// 依赖：Node >= 21（用到内置的全局 WebSocket），不需要任何 npm 包。
//
// ⚠️ 教训（本脚本第一版就踩了）：拼 `Runtime.evaluate` 的表达式时，
//    `JSON.parse(x)["a"]||{}` 后面再接 `.pos` 会被 `||` 的优先级吃成
//    `JSON.parse(x)["a"] || ({}).pos` —— 于是断言**失败但产品其实是对的**。
//    所以下面所有取值都走自包含的 IIFE，返回基本类型，不靠外部拼接。

const CDP = process.argv[2] || "http://127.0.0.1:9333";
const BASE = process.argv[3] || "http://127.0.0.1:8099";
const BOOK = "ai-infra";
const CHAP = "ch02";
const KEY = "radar.reading.v1";

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

async function waitReady() {
  for (let i = 0; i < 60; i++) {
    try {
      if ((await evalJs("document.readyState")) === "complete") return true;
    } catch (e) { /* 正在换执行上下文 */ }
    await sleep(150);
  }
  return false;
}

async function goto(path) {
  await send("Page.navigate", { url: path.startsWith("http") ? path : BASE + path });
  await waitReady();
  await sleep(600);        // 让页面 JS 跑完（restore 在 load 之后还会补一次）
}

/* ---------------- 页面内取值：一律自包含，返回基本类型 ---------------- */
const pctNow = `(() => { const max = document.documentElement.scrollHeight - window.innerHeight;
  return max > 0 ? (window.pageYOffset / max) * 100 : -1; })()`;

const chapterList = `document.querySelectorAll(".toc-item[data-chapter]").length`;

const readPosP = (book, chap) => `(() => {
  const s = JSON.parse(localStorage.getItem(${JSON.stringify(KEY)}) || "{}");
  const b = s[${JSON.stringify(book)}] || {};
  const p = (b.pos || {})[${JSON.stringify(chap)}];
  return p ? p.p : null; })()`;

const readMarkCount = (book) => `(() => {
  const s = JSON.parse(localStorage.getItem(${JSON.stringify(KEY)}) || "{}");
  const b = s[${JSON.stringify(book)}] || {};
  return (b.marks || []).length; })()`;

(async () => {
  await connect();
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 430, height: 900, deviceScaleFactor: 2, mobile: true,
  });

  // ---------------------------------------------------------------- 1
  console.log("== 1. 从干净状态开始 ==");
  await goto(`/reader/${BOOK}/${CHAP}.html`);
  await evalJs(`localStorage.removeItem(${JSON.stringify(KEY)})`);
  await goto(`/reader/${BOOK}/${CHAP}.html`);
  // 注意：**"没有 pos 记录"不是正确期望** —— 进任何一章都会如实记下"你在开头"。
  // 这里要确认的是"没有从上一轮残留下来的有效位置"，否则后面的 50% 断言就没了意义。
  const p0 = await evalJs(readPosP(BOOK, CHAP));
  check("初始没有残留的有效位置（null 或 ≈0%）", p0 === null || Math.abs(p0) < 1, p0);
  check("初始没有书签", (await evalJs(readMarkCount(BOOK))) === 0);
  check("章节页有可滚动的内容", (await evalJs(
    `document.documentElement.scrollHeight - window.innerHeight > 400`)) === true);

  // ---------------------------------------------------------------- 2
  console.log("\n== 2. 滚到 50%，位置应当被记下来 ==");
  await evalJs(`(() => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    window.scrollTo(0, Math.round(max * 0.5));
    window.dispatchEvent(new Event("scroll"));
  })()`);
  await sleep(1600);                     // 节流窗口 700ms，等它落盘
  const saved = await evalJs(readPosP(BOOK, CHAP));
  check("滚动后保存了位置（≈50%）", Math.abs(saved - 50) < 4, saved);
  const label = await evalJs(`(() => {
    const s = JSON.parse(localStorage.getItem(${JSON.stringify(KEY)}) || "{}");
    const p = ((s[${JSON.stringify(BOOK)}] || {}).pos || {})[${JSON.stringify(CHAP)}];
    return p ? p.label : null; })()`);
  check("位置附带小节名（给用户认得出来）", !!(label && label.length > 0), label);

  // ---------------------------------------------------------------- 3
  console.log("\n== 3. 加书签 ==");
  await evalJs(`document.getElementById("add-bookmark").click()`);
  await sleep(400);
  check("书签已写入", (await evalJs(readMarkCount(BOOK))) === 1);
  check("章节页书签列表渲染出 1 条",
    (await evalJs(`document.querySelectorAll("#bm-list .bm-item").length`)) === 1);
  check("书签带上了小节名",
    !!(await evalJs(`(() => { const e = document.querySelector("#bm-list .bm-t"); return e ? e.textContent : null; })()`)));
  check("加完书签把面板露出来",
    (await evalJs(`document.getElementById("bm-panel").hidden`)) === false);
  check("按钮进入「已加书签」态",
    (await evalJs(`document.getElementById("add-bookmark").classList.contains("is-on")`)) === true);
  check("「书签」按钮显示条数",
    (await evalJs(`document.getElementById("bm-count-inline").textContent`)) === "1");

  // ---------------------------------------------------------------- 4
  console.log("\n== 4. ⭐ 核心：离开再回来，要回到原处 ==");
  await goto(`/reader/${BOOK}/`);
  await goto(`/reader/${BOOK}/${CHAP}.html`);
  await sleep(1200);
  const back = await evalJs(pctNow);
  check("返回后回到原位（±6%）", Math.abs(back - 50) < 6, back.toFixed(1) + "%");
  const after = await evalJs(readPosP(BOOK, CHAP));
  // ⭐⭐ 这条防的是"落定闸"失效：进页面就保存的话 curPct() 还是 0，
  //     会把好位置覆盖成 0 —— 症状同样是"一返回就找不到了"，原因却完全不同。
  check("⭐ 返回后位置没被 0 覆盖", after > 45, after);

  // ---------------------------------------------------------------- 5
  console.log("\n== 5. 目录页（第 2 层）==");
  await goto(`/reader/${BOOK}/`);
  check("目录页书签面板显示出来",
    (await evalJs(`document.getElementById("bm-panel").hidden`)) === false);
  check("目录页书签列表有 1 条",
    (await evalJs(`document.querySelectorAll("#bm-list .bm-item").length`)) === 1);
  check("目录页标出「这章有书签」",
    (await evalJs(`document.querySelectorAll(".toc-item.has-mark").length`)) === 1);
  const ctaHref = await evalJs(`document.getElementById("book-cta-text").textContent`);
  check("主按钮变成「继续阅读」", ctaHref === "继续阅读", ctaHref);

  // ---------------------------------------------------------------- 6
  console.log("\n== 6. 书房（第 1 层）==");
  await goto(`/reader/`);
  const resume = await evalJs(`(() => {
    const r = document.getElementById("resume");
    const c = document.getElementById("resume-chapter");
    const p = document.getElementById("resume-pos");
    return r ? { shown: !r.hidden, chap: c ? c.textContent : null,
                 pos: p ? (p.hidden ? null : p.textContent) : null } : null; })()`);
  check("书房显示「继续阅读」", !!(resume && resume.shown), resume);
  check("续读卡片精确到百分比",
    !!(resume && resume.pos && /读到\s*\d+%/.test(resume.pos)), resume && resume.pos);

  // ---------------------------------------------------------------- 7
  console.log("\n== 7. 书签跳转与删除 ==");
  await goto(`/reader/${BOOK}/`);
  const href = await evalJs(
    `(() => { const a = document.querySelector("#bm-list .bm-link"); return a ? a.getAttribute("href") : null; })()`);
  check("书签链接指向章节 + 精确位置", !!(href && href.indexOf("#p=") !== -1), href);
  if (href) {
    await goto(href);
    await sleep(1200);
    const jumped = await evalJs(pctNow);
    check("按书签跳过去落在正确位置（±6%）", Math.abs(jumped - 50) < 6, jumped.toFixed(1) + "%");
  }

  await goto(`/reader/${BOOK}/`);
  await evalJs(`document.querySelector("#bm-list .bm-del").click()`);
  await sleep(400);
  check("删除书签后存储里没了", (await evalJs(readMarkCount(BOOK))) === 0);
  check("删空后面板自动收起",
    (await evalJs(`document.getElementById("bm-panel").hidden`)) === true);

  // ---------------------------------------------------------------- 8
  console.log("\n== 8. 页面无 JS 异常 ==");
  check("全程没有未捕获异常", pageErrors.length === 0,
    pageErrors.length ? pageErrors.slice(0, 3) : undefined);

  const bad = results.filter((r) => !r.ok);
  console.log("\n" + "=".repeat(52));
  console.log(`共 ${results.length} 项，失败 ${bad.length} 项`);
  if (bad.length) {
    bad.forEach((b) => console.log("  失败：" + b.name));
    process.exit(1);
  }
  console.log("阅读位置 / 书签：全部通过");
  process.exit(0);
})().catch((e) => {
  console.error("探针异常：" + (e && e.stack ? e.stack : e));
  process.exit(2);
});
