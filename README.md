# 个人情报台（daily-radar）

手机上打开就能看的每日情报站。**加一个模块 = 加一个目录**，不用动核心一行代码。

当前模块：

| 模块 | 内容 | 频率 |
|---|---|---|
| `github_trending` | GitHub 日榜 Top 10，每个仓库"是做什么的" | 每日 |
| `market_flow` | 中国 + 全球指数、板块/个股资金流、两融，附规则化分析报告 | 每日 |
| （预留） | 后续模块按同样方式加入 | — |

---

## 一分钟上手

```bash
python3 -m radar list          # 看有哪些模块
python3 -m radar run all       # 采集 + 生成页面
python3 -m radar serve         # 起服务，浏览器打开 http://<IP>:18090
```

服务器上部署成常驻服务：

```bash
bash deploy/install.sh
```

**依赖只有 `flask` + `jinja2`，不需要任何 key 就能跑。** 大模型（中文介绍、
更好的翻译）是可选增强 —— 想开就把 key 放进 `.env` 的 `RADAR_LLM_KEY`，
不配也完全能用，页面自动退回「翻译 + 规则」两层。
详见 [让读者看懂：两级中文化](#让读者看懂两级中文化)。

---

## 目录结构

```
daily-radar/
├── config.json                 # 唯一配置入口
├── radar/
│   ├── __main__.py             # CLI（run / build / serve / status / list / new / clean）
│   ├── web.py                  # Flask：发静态文件 + /api/status + /api/refresh
│   ├── core/                   # 与模块无关的骨架
│   │   ├── config.py           #   配置合并与路径解析
│   │   ├── http.py             #   带限速/退避/编码探测的 HTTP 客户端（纯标准库）
│   │   ├── llm.py              #   大模型客户端（OpenAI 兼容 + 缓存 + 失败降级）
│   │   ├── module.py           #   模块基类 + Context + load_sibling()
│   │   ├── registry.py         #   扫描 modules/*/module.py 自动发现
│   │   ├── store.py            #   落盘、索引、指纹、清理
│   │   ├── translate.py        #   英文简介 → 中文（大模型优先，免 key 兜底）
│   │   ├── md.py               #   极简 Markdown → HTML
│   │   ├── render.py           #   Jinja2 渲染静态站
│   │   └── runner.py           #   四阶段流水线编排
│   ├── modules/                # ← 扩展点：一个目录一个模块
│   │   ├── github_trending/
│   │   │   ├── module.py       #   collect / analyze / report + 大模型总结提示词
│   │   │   ├── sources.py      #   抓取与解析（绕 IP 的 HTTPS、README/目录、东财备用域…）
│   │   │   └── template.html   #   页面正文（可选，不写就用通用表格）
│   │   └── market_flow/
│   ├── templates/              # base.html / index.html / module_generic.html
│   └── static/                 # style.css / app.js / sw.js（SW 源码）+ icons/（PWA 图标）
├── tools/
│   ├── make_icons.py           # 生成 PWA 图标（Pillow，跑一次即可，图标跟仓库走）
│   └── llm_probe.py            # 打一次真实请求，确认 key/端点/模型能不能用
├── deploy/                     # systemd 单元 + install.sh + setup_https.sh + healthcheck.sh
└── data/  public/              # 生成物，已 gitignore
```

> `static/sw.js` 是**源码位置**；构建时会额外拷一份到 `public/sw.js`（站点根目录），
> 否则 Service Worker 作用域只有 `/static/**`，管不到首页。

---

## 架构：为什么是"静态站 + 极小服务"

每天的数据是**按天分片的小 JSON**，所以不需要数据库 —— 文件系统最合适，
还能直接 `diff`、`grep`、手工改，排查成本最低。

页面在流水线里一次性渲染成静态 HTML，线上服务只做三件事：

1. 发文件
2. 回答"有没有新数据"（`GET /api/status`）
3. 接受"现在拉一次"（`POST /api/refresh`）

换来的是：**后端进程挂了，历史报告照样能打开**；页面秒开；新增模块不需要改服务。

### 四阶段流水线

每个模块都按同一套四步走，**每步的产物都落盘，可以单独重跑**：

| 阶段 | 干什么 | 产物 |
|---|---|---|
| `collect` | 拉原始数据（必须实现） | `data/<mod>/raw/<date>.json` |
| `analyze` | 加工成结构化结果（默认透传） | `data/<mod>/<date>.json` |
| `report` | 写出人读的结论（默认不产出） | `data/<mod>/<date>.md` |
| `render` | 渲染页面 | `public/<mod>/<date>.html` |

关键约定：**任一模块失败不影响其他模块**（每个模块单独 try/except，状态分别记录）。
定时任务里一个数据源挂了，不该让整站停摆。

### 扩展方式（加模块 = 加目录）

```bash
python3 -m radar new my_module --title "我的模块" --order 30
```

生成骨架后，只改 `collect()` 就能跑起来。硬规则只有两条：
目录名必须等于类属性 `name`；一个 `module.py` 只允许一个 `Module` 子类。

---

## 手机端的"更新口子"

用户需求原话是"留一个口子，手机上监测到更新并自行拉取更新"。实现方式是**版本指纹**：

1. 构建站点时，把各模块的最新日期算成 sha1，烘进 HTML：
   `<body data-fingerprint="2026-09-13-a1b2c3d4e5f6">`
2. `app.js` 每 60 秒轮询 `/api/status`，拿服务端当前指纹
3. **两者不一致 ⇔ 数据比页面新** → 自动 `location.reload()`

指纹是 `<最新日期>-<内容摘要>`。内容摘要除了各模块的日期，还**带上了最新数据文件的
字节数与纳秒级 mtime** —— 这一点很关键：定时任务每天 08:00 和 16:30 各跑一次，
如果只看日期，收盘后的第二次采集就不会被手机察觉，页面会一直停在早上的数据。
带上文件时间戳之后，"数据变了"和"指纹变了"才真正等价；而纯粹重复构建（数据没动）
指纹不变，不会误报"有更新"。

回到前台（`visibilitychange`）会立刻查一次，所以切回 App 就能看到最新；
右上角 `↻` 触发服务端重跑，跑完自动刷新。页面响应带 `Cache-Control: no-cache`，
避免手机（尤其"添加到主屏幕"的独立窗口）一直用旧页面。

---

## 装成手机 App（安卓 APK）

> 主入口是 **真 APK**（`app/android/`），PWA 只是退路 —— 见下一节。
> 小米/华为等**国行 ROM 自带浏览器不支持 PWA 安装**（只能加书签），也没有 Play/Chrome，
> 所以"要能装"就**必须出 APK**。

```bash
cd app/android
./build.sh --token <口令> --code 3 --name 1.2 --deploy
```

产物 `dist/radar-1.2.apk` + `dist/radar-1.2.json`（版本元数据，`/api/app` 读它）。

### 端口

| 端口 | 用途 |
|---|---|
| **443** | **正式入口**（HTTPS，URL 里不用写端口）。**下载/访问都走它** |
| 80 | 仅供 ACME 挑战 + 明文反代兜底。**带口令的链接别走 80** —— 明文会泄露口令 |

壳里 `usesCleartextTraffic="false"`，所以 App 自身也只会走 443。

### 两层更新，别混

| 层 | 谁负责 | 怎么触发 |
|---|---|---|
| **内容**（榜单、报告） | 站点 | `app.js` 轮询 `/api/status`，指纹一变就 reload。**壳零代码** |
| **壳自身**（APK） | App | 启动时问 `/api/app`，`versionCode` 更大就下载并调起安装 |

壳自身的更新有三种状态，由 **`/settings/` 里的「自动更新」开关**决定：

| 场景 | 触发方式 | 行为 |
|---|---|---|
| 关（默认） | 冷启动 / 页脚版本按钮 | 弹窗问一句 → 点「立即更新」才下 |
| 开 | 冷启动（**1 小时**闸） | 不弹窗、不弹进度框，静默下载 → 调起安装 |
| 任意 | 设置页「下载并安装」 | 跳过所有闸门，直接下 |

其它约束：手动模式 **6 小时最多查一次** / **可以"跳过这个版本"**（记住 `skipped`，只在会弹窗的路径上生效）/
**查更新失败静默**（绝不打扰用户）。

⚠️ **诚实边界**：即便开了自动更新，**系统仍会弹一次安装确认**。这是安卓对所有
"非应用商店来源"的硬性要求 —— 真正做到零点击只能靠 root 或 Device Owner（设备管理），
普通 sideload 应用做不到。所以设置页上把这句话写出来了，不假装"完全不用管"。

下载与安装都在 App 内完成（`MainActivity.startDownload` → `ApkProvider` → 系统安装器）：

* 需要 `REQUEST_INSTALL_PACKAGES` 权限；没授权时**先引导去授权**，回来由 `onResume` 接着装
* APK 通过自建的 `ApkProvider`（`content://com.ypeak.radar.apk/...`）交给安装器 ——
  从 API 24 起跨应用传 `file://` 会被系统拒绝，而工程不引 AndroidX，所以没用 `FileProvider`
* 下载目录用**固定文件名** `radar-update.apk`，每次覆盖，不留历史残包
* 下完会验长度：链路中断时 `read()` 可能正常返回 EOF 而不抛异常，
  不验就会把截断的包丢给安装器，报出"解析包时出现问题"这种莫名其妙的错

> `/api/app` 的版本元数据来自构建时落下的 sidecar JSON —— `versionCode` **不在文件名里**，
> 光看 `radar-1.7.apk` 只能拿到 versionName。sidecar 缺失时 `versionCode` 退化为 **0**，
> 方向是安全的（App 侧判据是"服务器 > 本地才提示"，0 只会导致不提示，不会误报）。
>
> `url` 字段给的是**具体那个文件**（`/dl/radar-1.7.apk`），不是 `/app` 别名。
> 别名每次请求都重新挑"最新的包"，而 App 是"先问版本、再下载"两次请求 ——
> 中间刚好传了新包，App 就会拿 v1.7 的版本号去下 v1.8 的包，
> 表现为"更新完还提示有新版"，自相矛盾且极难复现。

### 下载入口

| 路径 | 说明 |
|---|---|
| `/app` | **短链**（推荐，手机上好敲）：`https://118.196.100.121/app?t=<口令>` |
| `/dl/latest.apk` | 同上，`latest` 挑的是**版本号最高**的那个（不是 mtime 最新的） |
| `/dl/radar-1.7.apk` | 指定版本 —— `/api/app` 的 `url` 字段给的就是这种确定路径 |
| `/settings/` | App 设置页：自动更新开关、安装权限、手动下载安装、规划提醒（测试 / 权限入口） |

都在统一鉴权之后（`APK` 里内嵌了口令，公开可下等于把口令挂网上）。
`<root>/apk/` **故意放在 `public/` 之外** —— `build_site` 每轮先 `rmtree(public)`，放里面会被删。

扫码版：`python tools/make_download_qr.py --token <口令>`（默认就是 `/app`）。
⚠️ 二维码里编码了口令，**等于一把钥匙，别公开**；生成后**必须反解码校验**
（`cv2.imread` 读不了中文路径时**静默返回 None**，要用 `np.fromfile`+`imdecode`）。

---

## 个人规划（App 内，只存手机本地）

顶栏的清单图标 → 弹出「我的规划」面板：写一句要做什么（可选目标日期和备注），
勾掉完成的，删错了 6 秒内可以撤销。

**数据只存在这台手机上**（安卓壳的 `SharedPreferences`，key `plans_json`），
**不上服务器** —— 服务器上不留任何痕迹，也不占任何接口。
代价是换手机 / 清应用数据就没了；这是刻意的取舍，不是遗漏。

### 怎么做到"只存本地"却不用给页面加接口

| 层 | 干什么 |
|---|---|
| `MainActivity.NativeBridge` | `getPlans()` / `setPlans(json)` / `plansBytes()`，落 `SharedPreferences` |
| `templates/base.html` | 弹窗骨架。**纯空壳** —— 里面一条规划数据都没有 |
| `static/app.js` → `setupPlans()` | 打开时从桥读回来渲染；每次改动立刻写回 |

和 `/settings/` 是同一个范式：**页面不注入数据，值全部运行时从桥里读**。
好处是同一个页面在浏览器和 App 里表现不同，而服务端完全不需要知道客户端状态。

### 几条不肯让步的设计

- **落盘失败必须看得见。** `setPlans` 返回"到底存住了没有"
  （用 `commit()` 而不是 `apply()` —— 后者是异步的，拿不到结果，只能永远报成功），
  写失败时面板顶部出一条警告。最怕的不是丢数据，是**用户以为记下了**。
- **不吃"改了内存就算数"。** 增 / 改 / 删都立刻落盘。
  探针里"关掉面板再打开内容还在"是硬断言 —— 那才是真的存住了。
- **删除不弹确认框。** ⚠️ WebView 里 `window.confirm` **默认根本不弹、直接返回 false**
  （`WebChromeClient` 不处理 `onJsConfirm` 就是这行为），拿它做确认会变成
  "点了删除没反应"。改成**立刻删 + 6 秒撤销** —— 后悔药本来也比确认框顺手。
- **有上限，且超限明确失败。** 200 条 / 64KB（按 UTF-8 字节算，中文一个字 3 字节）。
  `SharedPreferences` 在 App 启动时被**整体读进内存**，塞大了会拖慢每次冷启动；
  超限**返回 false** 让界面提示，**绝不静默截断**
  （截断的 JSON 解析不出来，等于把用户写的东西悄悄弄丢）。
- **浏览器里不放假输入框。** 没有原生桥时，面板只显示一张"这个面板只在安卓 App 里可用"
  的说明卡 —— 那个输入框存不进任何地方，比没有更让人困惑（和 `/settings/` 同一个判断）。
  入口图标在浏览器里**仍然显示**，好让人知道 App 里有这个功能。
- **用户输入走 `textContent` 而不是 `innerHTML`。** "这是我自己写的内容"不是安全理由；
  探针里专有一条塞 `<img onerror=…>` 验它不会被执行。
- **逾期用琥珀色（`--warn`），不用红色。** 本项目的红色是"涨"，拿它当警示会和行情语义打架。

### 自测

```bash
bash tools/run_ui_probe.sh tools/plans_probe.mjs     # 58 项，真跑无头浏览器
```

探针注入一个**会真的存住**的假桥（`getPlans` 读的就是 `setPlans` 上一次写进去的串），
所以"关掉再打开内容还在"才是有意义的断言 —— 否则只证明了内存里那个数组没被清掉。
另有 `window.__failNextSave()` 专门把保存改成失败，用来验"存失败时页面上看得见"。

---

## 规划到期提醒（系统通知，只存手机本地）

规划里填了目标日期，**到期当天早上 9:00** 会弹一条系统通知 ——
顶上滑出来、有声有震动、留在通知栏、点一下直接进 App 并掀开规划面板。
（用户要的就是"跟微信来消息一样"，所以走的是系统通知，不是页面里的浮层。）

仍然**不经过服务器**：排程和通知全在手机上，服务器那边一个字都不知道。

### 为什么排程在原生、不在网页

⭐ **因为手机重启之后必须能重新排上。** 闹钟（`AlarmManager`）**不跨重启存活**，
而重启时 WebView 根本没加载、网页一行 JS 都没跑过 —— 排程信息要是只活在网页里，
重启一次提醒就永久失效，而且全程不报任何错。
所以原生自己读 `plans_json` 推导时刻（`PlanReminder`），
并在开机 / 覆盖安装 / 改系统时间 / 换时区时重排（`PlanAlarmReceiver`）。

| 文件 | 干什么 |
|---|---|
| `PlanReminder.java` | 从 `plans_json` 推出"下一次该什么时候响"，交给 `AlarmManager` |
| `Notifier.java` | 通知渠道（`IMPORTANCE_HIGH` = 顶上弹）+ 发通知 |
| `PlanAlarmReceiver.java` | 到点发通知；开机 / 重装 / 改时间时重排 |
| `MainActivity` | 通知权限、设置页跳转入口、点通知后掀开面板（`onNewIntent`） |
| `static/app.js` | 设置页那块状态显示 + `window.RadarPlansOpen` 钩子 |

### 几条不肯让步的设计

- **同一时刻只有一个闹钟。** 要提醒的是"某一天"而不是"某一条"，
  所以每次只排最近那一天，响完再排下一天 —— 天然免疫系统对精确闹钟的数量限制，
  也不会因为清了规划而留下一堆孤儿闹钟。
- **过了点不补，唯一的例外是开机。** 到期当天已经过了 9:00 才排到就不再补 ——
  否则用户每次打开 App 都可能被一条迟到的提醒打脸。
  但**关机期间错过的那次要补**（开路勾着 `catchUpMissed`，只在 `BOOT_COMPLETED` 时为 true）：
  闹钟本来排好了，是重启把它吃掉的，不补就成了"开着手机反而没提醒"。
- **一天只提醒一次，且只有真发出去了才记数。** 没权限时**不**记 ——
  等用户把权限开回来，当天还有机会补上。
- **拿不到「闹钟与提醒」权限就降级，但要看得见。** Android 14 起新装应用默认
  拿不到那个特殊权限，`setExact*` 会直接抛 `SecurityException`。
  所以先 `canScheduleExactAlarms()` 问一句，拿不到就退化成不精确闹钟
  （一定会响、但可能晚一会儿），并在设置页**如实写出来** + 给一个"去开启"的入口。
  绝不假装自己是准点的。
- **日期严格校验。** `isDay()` 卡死 `YYYY-MM-DD`，不合法就当这条没有日期 ——
  宁可漏一条，不可错一条（宽容解析把日期理解错，表现是"某天莫名响了"，最难查）。
- **"今天"按本地时区算**，且格式化固定 `Locale.US`
  （UTC 在东八区凌晨会差一天；某些区域设置下 `%d` 会输出非 ASCII 数字）。
- **国产 ROM 的"自启动"单独引导。** 小米/华为这类系统默认禁止后台自启，
  被禁之后闹钟**根本不响**且不报错。所以检测到厂商就在设置页摆一个
  "去设置自启动"（打不开厂商页面就退回应用详情，并明说一句，不摆点了没反应的按钮）。
- **通知权限弹框一辈子只有一次机会**，所以只在用户**存下第一条带日期的规划**时问
  （来意不言自明），并记下"问过了"；之后再点就改成把人送到系统设置页。
- **`exported="true"` 而不是 `false`。** 官方文档说 `false` 也能收到系统广播，
  但社区有大量 `targetSdk 31+` 上收不到 `BOOT_COMPLETED` 的实例，而收不到的后果是
  **静默的**（重启后永久不提醒）。两边代价完全不对称，所以选"一定能收到"那边；
  代价只是别人也能往这个接收器发广播 —— 而它是**幂等**的，
  伪造成功最多让人看到一条自己本来就该收到的提醒。

### 怎么验证它真的会响

```bash
# 设置页 →「规划提醒」：
#   下一次  —— 原生排到的具体时刻，和面板上的日期对得上就说明排程是活的
#   通知权限 / 准点提醒 —— 没开的话按钮会直接摆出来
#   「发一条测试提醒」—— 一按就弹，别等到期那天才知道通没通
```

---

## 我的自选股（`/watchlist/`，名单存服务器）

自己盯的那几只，每天一行：涨跌、成交额、换手、市盈率/市净率、振幅。
页面底部「管理自选股」里填代码或名字，保存即生效（会**立刻重跑这一支并重建站点**，
不用等到明天）。支持 A 股 / 港股 / 美股，混着写也行：

```
600519          sh600519       1.600519       600519.SS
00700           hk00700        116.00700      00700.HK
AAPL            usAAPL         105.AAPL
茅台            腾讯控股                         ← 直接写名字也可以
```

### 名单为什么存服务器，而规划只存手机

```
规划    只有你自己看，服务端不需要知道       → 存手机，服务端零痕迹
自选股  每日简报 / 以后按标的筛新闻都要用它  → 存服务器
        而这些消费方**全在服务端**
```

名单落在 `data/watchlist.json`（`data/` 不入库）。它**不是机密**，
但也别往里塞敏感东西 —— 服务器上的东西，口径就是"能被别人看到也没关系"。

### 几条不肯让步的设计

* **`resolve` 不猜。** 名字/代码**完全相等**才自动选中；搜出多个候选又没有一个完全相等
  → 交给用户选，**不由我们替他挑**（挑错了比让他再敲一次烦得多）。
* ⭐ **表单往返幂等。** 页面回填的文本再存一遍，一只都不许少。
  这条当初是踩出来的：东财搜 `00700` 会同时给出 `00700`(腾讯控股) /
  `000700`(模塑科技) / `300700` / `600700`，只看"有多个候选"就判歧义的话，
  用户**什么都没改、点一下保存就少一只股票**。所以"代码完全相等"和"名字完全相等"
  一样算强信号 —— 命中的那条候选，代码就是用户敲的那串，这不是猜。
* **空名单是合法状态**，不是错误：页面照出，带一个「加两只」的表单。
  空名单要是没有页面，用户就没有入口去添加 —— 死循环。
* ⭐ **一条行情都没拉到 → 抛错，今天不写快照。** 页面继续显示昨天那份好的，
  而不是被一份空数据顶掉（和 `market_flow` 同一口径）。
* `secid` 是要**拼进请求 URL** 的，所以写盘前过白名单正则 —— 用户输入不可信。
* 行情来自东财 `push2delay` 域，**约 15 分钟延迟**，用于每日复盘，不适合盘中交易。

### 自测

```bash
python3 smoke_test.py          # 第 18 段：名单落盘 / 解析 / 往返幂等 / 简报 / 接口
```

解析逻辑用**桩**替掉搜索接口，所以这段不联网也能跑；接口那段用 Flask 测试客户端
真打一遍鉴权和参数校验，但**故意不发 POST**（POST 会真的重跑采集并重建站点，
冒烟测试不该有副作用）。

---

## 每日大盘简报（`/api/brief`）

三个时段给三种话：早间（隔夜外盘 + 昨日收盘）、尾盘 14:30（此刻）、收盘（复盘 + 明日留意）。
手机到点时来取一次，弹成系统通知。

```bash
curl -H "X-Radar-Token: $RADAR_TOKEN" "http://127.0.0.1:18090/api/brief?slot=close"
```

### 结构：数字是规则拼的（永远有），判断是大模型的（可以没有）

通知是唯一触达用户的东西，**宁可只给数字，也不要弹一条"简报生成失败"**。
所以 `build_brief()` 里大模型那一块失败就安静降级，免责声明和数字永远在。

成本纪律沿袭 `core/llm.py`：同一个（简报属于哪天, 时段）只调一次，只缓存**成功**的结果；
**尾盘那次不调大模型** —— 数字每小时在变，缓存必然对不上，实时生成又白花钱。

### ⭐ 两件差点写错的事

* **「主力净流入」不是「领涨板块」。** `industry.inflow` 是**按主力净流入金额**排的，
  里面完全可能出现"资金在进、股价却在跌"的板块（首份快照里 `通信` 就是
  +44.32 亿 / -0.04%）。把它叫"领涨板块"就是拿一组数据当另一组用。
  现在按口径命名，并同时给出净流出那侧。
* ⭐ **一句话里不许出现两个时间基准。** 尾盘那次指数是此刻的（实时拉），
  涨跌家数 / 资金流却只能来自上一份快照（现拉要翻 56 页，一次通知等不起）。
  直接拼起来会读成「指数涨 0.94%，605 涨 / 4567 跌」—— 两个数字来自不同的日子。

  更隐蔽的是：**文件属于哪天 ≠ 数字是哪天的**。采集器 08:00 也会跑一次，
  写出的 `2026-09-13.json`（周日的采集）里装的是 **09-11 周五 16:11 的收盘数据**。
  所以日期以**行情行自带的 `ts`** 为准（东财每个报价都带 `fetched_at` 之外的行级时间戳），
  凡不是今天的数字，一律在句子里带上 `昨日` / `09-11` 这样的前缀：

  ```
  14:30 上证指数 3,911.87 +0.94% ｜ 09-11 605 涨 / 4567 跌 ｜ 自选 5 只 1涨4跌（均 -0.79%）
  ```

* 另有一种陈旧：文件不是今天的（采集压根没跑），这时整段前面会多一行
  `⚠️ 今天还没采到数据，下面用的是 … 那份快照`。
  这种错**看着一点都不像错**，最危险。

---

## 退路：装成 PWA（原生/国际版 ROM 可用）

这不是"网页加个书签"，是**装到桌面、独立窗口打开、断网也能看历史**的 app。
要满足三条浏览器不会告诉你的硬条件，缺一条就**静默不给安装入口**（页面看着一切正常，
就是装不上 —— 这次踩的就是这个坑）：

| 硬条件 | 做法 | 踩坑点 |
|---|---|---|
| manifest 里有 **192×192 和 512×512 图标** | `tools/make_icons.py` 生成 | `"icons": []` → 永远不给安装入口 |
| 注册了 **Service Worker** | `/sw.js` + `app.js` 里 `register("/sw.js")` | 放 `/static/sw.js` 作用域只有 `/static/**`，管不到首页 → 判定不合格 |
| **HTTPS 或 localhost** | `deploy/setup_https.sh` | 裸 `http://IP` 在安卓上不满足安全上下文 |

### 怎么装

**安卓 Chrome / Edge**：右上角菜单 →「安装应用」/「添加到主屏幕」。
装了之后顶栏会出现 `⤓` 按钮（`beforeinstallprompt` 触发后才显示），点一下直接弹安装框。

**iPhone Safari**：底部分享 → 「添加到主屏幕」。iOS 不读 manifest 里的 icons，
认的是 `apple-touch-icon`，已经放进 `<head>` 了；也没有安装 API，所以首次访问会提示一次。

### HTTPS 是怎么解决的：Let's Encrypt **IP 地址证书**

原本卡在"裸 IP 不是安全上下文"。评估过几条路：

| 方案 | 结论 |
|---|---|
| Cloudflare quick tunnel | 能用，但**域名每次重启都变** → 已装的 app 图标失效 |
| serveo 固定子域名 | 要注册 SSH key（需人工登录），且匿名额度已满 |
| ngrok | agent 端点被墙 |
| 自己的域名 + 证书 | 要买域名；国内还要备案 |
| **LE IP 证书（采用）** | IP 固定 → **URL 永远不变**、不用域名、不用备案、**手机不用装任何东西** |

LE 自 **2026-01-15** 起 GA 支持 IP 证书。约束与代价：

- 标识是 IP 本身（SAN 里是 `IP Address:118.196.100.121`），不是域名
- **强制 `shortlived` profile：有效期 160 小时（约 6.7 天）** → 必须自动续期
- 校验只能用 **http-01 / tls-alpn-01**，不能用 DNS-01（IP 没有 DNS 记录可证明归属）
- 需要 **certbot ≥ 5.4**（`--ip-address` 是 5.3 引入的；Ubuntu 自带的太旧）

一键配置：`bash deploy/setup_https.sh`。它做的事：

1. nginx 占 80/443 做 TLS 终结，**radar 退到 `127.0.0.1:18090`**（只监听本机，
   否则能绕过 TLS 直接打 http）
2. 80 端口提供 ACME 挑战目录 `/var/www/certbot`（这条路径**不能加口令**，
   否则 LE 校验失败；radar 的口令拦截在它后面，不冲突）
3. `certbot certonly --preferred-profile shortlived --webroot --ip-address 118.196.100.121`
4. 写 443 配置 + `radar-cert-renew.timer` 每天 03:17/15:17 检查续期（剩 1/3 时自动换）

> ⚠️ **故意不开 HSTS**：证书 6.7 天到期，万一续期挂了，有 HSTS 的话浏览器
> 连"继续访问"的逃生口都不给。宁可少一层保护也要留退路。
>
> ⚠️ **服务器 pip 默认走火山内网镜像 `mirrors.ivolces.com`，有滞后**
> （上面 certbot 只到 5.2.2，没有 `--ip-address`）→ 装新版必须显式
> `pip install -i https://pypi.org/simple/ "certbot>=5.4"`。

**本地实验**：`localhost` 天然算安全上下文，`python -m radar serve` 后即可完整安装，不需要证书。

---

## 让读者看懂：两级中文化

GitHub 上的一切都是英文，而"看懂一个仓库"其实分两层，代码里也是分开的两层：

| 层级 | 干什么 | 代码 | 需要 key 吗 |
|---|---|---|---|
| 第 1 级 | 把作者那句英文简介**翻成中文** | `core/translate.py` | 不需要（配了就升级） |
| 第 2 级 | **读 README + 根目录**，写一段中文介绍 | `sources.fetch_repo_ctx` + `core/llm.py` | 需要 |

为什么非要分两层：英文 description 常常只有半句话（`A fast ORM for Python`），
机翻出来还是半句话，读者仍然不知道"这玩意解决什么问题、我该不该点进去"。
要写出有用的话，得先给它**更多原料**（README 正文、根目录结构、语言构成）。

所以第 2 级不是"把翻译做得更花哨"，而是**换了个信息源**：从"作者的一句话"
换成"仓库自己的文档"。

---

### 第 1 级：翻译（`radar/core/translate.py`）

设计目标只有一个：**能无人值守跑**。定时任务自己跑，没人看着，所以：

| 约束 | 做法 |
|---|---|
| 不能要人工介入 | **配了大模型 key 就优先用大模型**，否则走免 key 公开接口（有道 → MyMemory 兜底） |
| 不能把整轮采集搞崩 | **任何异常都吞掉**，翻不出来就退回英文原文，报告照出 |
| 不能天天重复翻同一句 | 按内容落盘缓存到 `data/.translate_cache.json` |
| 不能把品牌名翻坏 | 见下 |

走大模型还有一个顺带的好处：**省往返**。十句简介攒成**一次请求**返回
（`_LLM.translate_many`），而不是发十次。长度对不上就整块放弃、退回免 key 接口 ——
宁可翻得糙一点，也不能把错位的译文安到别的仓库头上。

⚠️ **缓存 key 里带 provider 签名**（`Translator.sig`）。不这么做的话，从免 key
升级到大模型之后还会一直吃老渠道的低质量缓存，"升级"等于没升。代价是换渠道要
重翻一遍，值得。

### 踩过的两个真 bug（都已写进测试）

1. **`\b` 在中文旁边不算边界。** Python 的 `\b` 按 `\w` 判定，而中文也算 `\w`，
   于是 `面向llm的` 里 `向|l` 之间没有边界，`\bllm\b` 匹配不上 → 缩写大写不了。
   改用环视 `(?<![A-Za-z])…(?![A-Za-z])`，既能处理中文紧邻，又不会误伤
   `Airtable`（含 `ai`）、`clipper`（含 `cli`）这类词。

2. **`re.sub` 不重叠扫描会漏掉残留空格。** 用 `(汉字)\s+(汉字)` 这种捕获组写法，
   替换掉前一个汉字后指针已越过它，紧跟的空格再也匹配不上，
   `免费的 工具` 清不干净。改成环视不消费字符即可。

### 品牌名：一个花了功夫的坑

机翻会把"本身是普通英文词"的产品名按字面翻：

```
Intercom → 对讲机     Notion → 概念     Gemini → 双子座     Flash → 闪光
```

**实测过所有"保护"写法 —— `<b>` 标记、方括号 `[[ ]]`、全大写 —— 只要该词本身
是个普通英文词，有道照样翻**（`Intercom` 在任何编码下都变成"对讲机"）。
所以只能事后修，见 `_repair_brands()`。

防误伤的唯一手段是：**只在该词确实出现在英文原文里时才替换**。
原文有 `Intercom` 且译文出现"对讲机" → 换回 `Intercom`；
原文没有 `Intercom` 时，"对讲机"照旧不动（它可能真的是对讲机）。

**换到大模型之后这个坑基本消失**（提示词里明确要求保留产品名原样），
但 `_repair_brands` 留着——它是免费接口那条路的保险。

### 缓存里存的是"原始输出"而不是成品

这样以后改进缩写 / 排版 / 品牌名规则时，**已有的缓存立刻跟着受益**，
不用清缓存重翻（省额度，也避免新旧数据风格不一致）。

---

### 第 2 级：读懂仓库（`radar/core/llm.py`）

每个仓库多花 **2 次** `api.github.com` 请求，拿到：

- **README 正文**（`/repos/{full}/readme`，`Accept: application/vnd.github.raw`，
  截断 6000 字符 —— 约 2000 汉字，够抓重点）
- **根目录条目**（`/repos/{full}/contents`，最多 40 项）

连同上一步已有的简介 / 语言 / topics / star / 体积 / 许可证，拼成一张"资料卡"，
让模型输出三个字段：

```json
{"tagline": "一句话说清它是什么（20 字以内）",
 "detail":  "2~3 句话：解决什么问题、关键做法或亮点",
 "usage":   "一句话：典型用法。资料里没写到就留空，不要编"}
```

三条硬约束（和 translate.py 同源，因为同样是无人值守）：

1. **永不抛异常**。超时、key 失效、余额不足、限流 —— 全部吞掉返回 `""`，
   调用方降级。绝不能让"锦上添花"的东西搞崩整轮采集。
2. **必须落盘缓存**。同一天要跑两轮（08:00 / 16:30），语义没变就不该重复花钱。
   缓存 key = 模型名 + 提示词全文，所以换模型会自然失效。
3. **key 不进仓库**。`config.json` 是要推到公开仓库的，所以 key 只从环境变量
   `RADAR_LLM_KEY` 读（服务器上是 `.env`，systemd 注入）。

**提示词里两条最要紧的纪律**，都是为了压住大模型的通病：

- 「不要以仓库名开头」—— 不这么说，tagline 十有八九写成"XX 是一个……"
- 「资料里没提到就填空字符串，不要编」—— 不这么说，它会给每个仓库
  编一段 `pip install` 和性能数字

配套的两个细节：

- **资料太少就不写**（连简介、topics、README 全都没有）→ 直接返回空，
  让模型瞎猜不如不写
- **README 不落盘**。`raw/<date>.json` 是每天一份的历史快照，整篇 README
  塞进去会让 `data/` 迅速膨胀。落盘时只留前 800 字 `readme_excerpt` 供排查，
  根目录结构（只有一行）照原样留着，页面和报告里都要显示。

### 省 API 额度：`RepoContextCache`

README + 根目录都吃 `api.github.com` 的额度，而**未认证只有 60 次/小时**：

| 认证方式 | 限额 | 每轮消耗 |
|---|---|---|
| 不带 token | 60 次/小时 | 10（详情）+ 20（README+目录）= **30** |
| 带 token（`github_token`） | 5000 次/小时 | 想加多少加多少 |

一天两轮正好卡在 60 的边缘，所以加了磁盘缓存 `data/github_trending/.repo_ctx.json`，
**key = 仓库名 + `pushed_at`**：

- 代码没变（绝大多数情况）→ 第二次采集零额度消耗
- 代码变了 → 自动重抓，总结跟着更新

不按日期缓存的原因就在这：那样仓库当天更新了，总结却是旧的。

实测效果（同一份数据连跑两轮）：

```
第 1 轮：拿到 10/10 上下文，11 次模型请求，23640 tokens，采集 86s
第 2 轮：翻译 10/10 命中缓存，模型 9/10 命中缓存（只 1 次新请求 2245 tokens），采集 18s
```

> 顺带一提：`config.json` 的 `github_token` 和 SSH 密钥是两回事。
> SSH（`git@github.com`）只解决 clone / push；`api.github.com` 是 HTTPS 接口，
> **只认 `Authorization: Bearer <token>`，不认 SSH 密钥**。所以要用到 API 的功能
> 想提额度，只能配一个只读 PAT。

### ⚠️ 额度是「每小时」算的，别在一小时内连着重跑

| 认证 | 限额 | 每轮消耗 |
|---|---|---|
| 匿名 | 60 次/**小时** | 30 |
| 带 token | 5000 次/小时 | — |

一天两轮、间隔 8.5 小时 → 每轮各自在一个新的小时窗口里，30 次很宽裕。
**真正会把额度打光的只有调试**：一小时里手动跑五轮 = 150 次，必然撞限流。

撞上之后的日志长这样（`enriched: 0` + `拿到 0/10 个仓库的代码上下文`）：

```
补全 10 个仓库的详情…
拿到 0/10 个仓库的代码上下文
生成 10/10 段中文介绍          ← 还能出，但质量差一档（没有 README/目录）
```

查余额（`remaining` 到 0 就看 `reset` 是几点）：

```bash
.venv/bin/python -c "
import json,time,urllib.request
d=json.load(urllib.request.urlopen(urllib.request.Request(
  'https://api.github.com/rate_limit',headers={'User-Agent':'radar'})))
c=d['resources']['core']
print('剩余',c['remaining'],'/',c['limit'],'重置',time.strftime('%H:%M',time.localtime(c['reset'])))"
```

### ⭐ API 拿不到时：不许用更差的数据覆盖上一份好数据

数据文件是**按天覆盖写**的。所以撞限额那一轮如果什么都不管，
就会把上一份「有 topics、有根目录、有 README 摘要」的数据**盖成残缺版**，
而且读者完全看不出发生过什么 —— 这正是实测踩到的：

```
22:04 那轮：enriched=10、上下文 10/10、25344 tokens   ← 好数据
22:06 那轮：enriched=0、上下文 0/10、9373 tokens      ← 把好数据盖掉了
```

修法是 `_load_prev_items()` + `_inherit()`：API 拿不到时，
从**今天更早那一轮的落盘**里继承 `topics / license / size_kb / pushed_at / stars …`
—— **只填空白，绝不覆盖新拿到的值**。

其中 **`pushed_at` 是最关键的那个**：上下文缓存的 key 就是它。
拿不到 `pushed_at` → 缓存全部失配 → 连本来能免费命中的 README 都要重新抓，
而"没有额度"恰恰就是这一轮的问题。所以修好 `pushed_at` 等于顺带修好整条链。

页面底部的「数据源」栏会显示 **⚠️ 有 N 个仓库的详情沿用了今天上一轮的数据**，
这样读者知道是"这次没抓到"，而不是"这些仓库本来就没有 topics"。

### 怎么开（服务器上三步）

```bash
cd /root/daily-radar
# 1. 写 key（不入库；.env 已在 .gitignore 里）
echo 'RADAR_LLM_KEY=sk-你的key' >> .env
chmod 600 .env

# 2. 确认采集任务也加载了 .env —— 漏了不会报错，只会安静降级
grep EnvironmentFile /etc/systemd/system/radar-collect.service

# 3. 先探一下能不能用，再实跑
set -a; . ./.env; set +a
.venv/bin/python tools/llm_probe.py            # 打一次真实请求，看内容和用量
.venv/bin/python -m radar run github_trending
```

`llm_probe.py` 是专门为"配好了 key 但不确定能不能用"写的：它不打日志、不写文件，
就是打一次真实对话请求，把 HTTP 状态、耗时、返回内容、`finish_reason`、**思考 token**、
token 用量、**真实模型名**全打出来。两个判断点最有价值：

- **正文为空 + `finish_reason=length` + 思考 token 一大把** → 预算被"思考"吃光了，调大 `--max-tokens`
- **真实模型名 ≠ 请求的模型名** → 这个客户端名是个别名，被服务端路由到了别的后端

换供应商只要改 `config.json` 里的 `provider` / `base_url` / `model`，都是 OpenAI 兼容协议：

| provider | 说明 |
|---|---|
| `deepseek` | 默认。便宜、快、中文好 |
| `openai` / `moonshot` / `zhipu` / `siliconflow` | 改 provider 即可 |
| `local` | 指向自己起的 vLLM：`http://127.0.0.1:8000/v1` |

### DeepSeek 的模型：两个名字，都带「思考」

实测这个 key 的 `/models` 只返回两个：

| 模型 id | 特点 |
|---|---|
| **`deepseek-flash`** | 默认。快，1~2s；实测一句话请求思考 30~60 tokens |
| `deepseek-v4-pro` | 更强也更慢（6s+，思考 100+ tokens），要质量就换它 |

**关键坑：这两个模型都带思考，而 `reasoning_tokens` 和正文是共用 `max_tokens` 预算的。**
预算给少了不会报错，而是 `finish_reason=length` + **正文一个字都没有** —— 典型的"安静失效"。

```
实测（同一句提问，JSON 模式）：
  max_tokens=200   → 正文「空」，思考 200   ← 预算全被思考吃了
  max_tokens=500   → 正文正常
  max_tokens=2000  → 正文正常
```

所以代码里有三道保险：

1. `max_tokens` 默认给到 **2000**（不是"够写 500 字"那种算法，得给思考留位置）
2. `LLM.chat()` 里检测到「正文为空 + `finish_reason=length` + 有思考 token」
   → **自动把预算放大 4 倍重试一次**（上限 `max_retry_tokens`，默认 8000，防止一路翻倍烧额度）
   → 重试仍为空才返回 `""`，并且**空正文绝不写进缓存**（否则以后永远吃这条空缓存）
3. `LLM.chat_json()` 里检测到「JSON 解析失败或缺必需键」→ **同样加预算重来一次**，
   并在重试时补一句"上次被截断/套了外层，请直接输出、精简并闭合结尾"。

第 3 道是补第 2 道的漏：**第 2 道只在"正文为空"时才触发**，而截断时正文是
**非空但不合法**的（写到一半被 `length` 切断，`json.loads` 直接抛）。实测就有一个仓库
（根目录结构最长，666 字符）因为这样整段介绍没生成 —— 页面上少一个 `.ai-box`，
日志里只有一行 `生成 9/10`，不查不知道是谁。

重试成功后，结果会**同时写回"原始提示词"的缓存位**：否则每轮都是
"先失败一次 → 再带提示重试一次"，等于每天两轮白多花两次请求。

### 还有一种失败：模型把答案套了一层信封

同一轮里抓到过更刁的一个（**10 个仓库里出现 1 个**）：

```json
{"type":"json_object","content":{"tagline":"…","detail":"…","usage":"…"}}
```

字段一个不少，**只是被包进了 `type` / `content` 这一层**。原因是
`response_format={"type":"json_object"}` 被模型**理解成了"要输出的内容格式"**，
于是它老老实实把信封也写了出来。

这类错误**不能靠重试解决**（重试很可能再套一层），只能在解析这层兼容：
`_unwrap_envelope()` 会在「顶层凑不齐必需字段」时尝试剥一层，
但**判据很保守 —— 必须"剥开之后恰好能凑齐"才剥**，正常回复不会被误伤。

### 排查工具：`tools/diag_summary.py`

"这一句为什么没生成"不该靠猜。这个脚本**直接读采集时用的同一套缓存**
（上下文读 `.repo_ctx.json`、模型回复读 `.llm_cache.json`），
所以默认**零网络、零成本**，而且看到的就是**当时实际拿到的回复** ——
比重新发一次请求更有价值（重发可能就正常了，反而看不出问题）。

```bash
.venv/bin/python tools/diag_summary.py                # 列出最新一轮全部仓库的 AI 状态
.venv/bin/python tools/diag_summary.py DeskcommCRM    # 只看匹配的仓库，打印原始回复
.venv/bin/python tools/diag_summary.py DeskcommCRM --live   # 真发请求验证能否救回来（花钱）
```

它会把人话结论直接给你：**被截断** / **正文为空（思考吃光预算）** / **套了信封** /
**没按格式输出**。上面那个信封 bug 就是它抓出来的。

另外 `deepseek-chat` / `deepseek-reasoner` 这类老名字虽然也能调通，
但都会被路由到 `deepseek-flash`，**别把它们当独立模型** —— 配置里写清楚真实模型名。

**成本**：一轮约 2.6 万 tokens（10 个仓库，含思考），一天两轮、加上缓存命中，
按 DeepSeek 的价格是**每天几分钱**级别。真要省钱，把
`github_trending.summarize.max_items` 调到 5 就砍一半。

**如果不想花钱**：把 `config.json` 的 `llm.enabled` 改成 `false`（或者什么都不配），
页面自动退回"翻译 + 规则"两层，功能照常、只是没有那段中文介绍。

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/status` | 指纹、最新日期、上次运行结果、是否正在采集、各模块概况（**内容**更新口子） |
| `POST` | `/api/refresh` | 触发一轮采集（异步，202 立即返回；已在跑则 409） |
| `GET` | `/api/modules` | 模块清单 |
| `GET` | `/api/app` | 安卓壳的最新版本：`versionCode` / `versionName` / `size` / `sha256` / `url`（**壳自身**更新口子；`url` 是具体文件名，不是 `/app` 别名） |

### 静态/下载

| 路径 | 说明 |
|---|---|
| `/<path>` | 预渲染的静态页面（`public/`） |
| `/settings/` | App 设置页 —— 核心页，由 `render.py` 的 `core_pages` 生成，同样套 `base.html` 外壳 |
| `/static/<rel>` | ⚠️ **优先取 `public/static/` 的预构建副本**，没有再回落 `radar/static/`。改了 `radar/static/` 必须 `python -m radar build` 才生效 |
| `/app` · `/dl/<name>` | 安卓 APK 下载（`<root>/apk/`，在鉴权之后） |

### 访问口令（可选，但公网部署建议开）

端口是对公网开的，开启校验后，除口令持有者外谁也打不开（也打不了 `/api/refresh`）。

**手机上能用是关键**：只用 `?t=` 的话，点导航跳到 `/market_flow/` 口令就丢了。
所以首次带口令访问时会种一个 Cookie，之后同一浏览器一直有效：

```
https://<IP>/            # 已有 Cookie 的浏览器直接开
https://<IP>/?t=<口令>   # 首次访问带口令，之后浏览器记住
```

也支持请求头 `X-Radar-Token`（给脚本用）。

**口令放在哪里**：`config.json` 是要推到公开仓库的，所以真实口令不能写在里面。
用环境变量 `RADAR_TOKEN`，服务器上由 systemd 的 `EnvironmentFile` 从 `.env` 读：

```bash
# 服务器上 /root/daily-radar/.env（不入库）
RADAR_PORT=18090          # 走 HTTPS 后由 nginx 反代，radar 只监听本机
RADAR_HOST=127.0.0.1      # 关键：不许外网直连，必须过 TLS
RADAR_IP=118.196.100.121  # healthcheck 校验证书用
RADAR_TOKEN=<一串随机字符>
RADAR_LLM_KEY=sk-...      # 大模型的 key，见「让读者看懂：两级中文化」
```

⚠️ **`.env` 同时被 `radar.service`（Web）和 `radar-collect.service`（定时采集）
加载**。两个单元都必须有 `EnvironmentFile=-/root/daily-radar/.env` 这一行 ——
**漏了不会报错，只会安静降级**：采集任务读不到 `RADAR_LLM_KEY`，翻译退回免 key
接口、中文介绍整段不生成，日志里只有一句"没读到 key"。

`config.json` 的 `server.token` 是备用入口（适合本地临时测试），
优先级：环境变量 > config.json。想完全关掉校验，把 `.env` 里的 `RADAR_TOKEN` 删掉再
`systemctl restart radar` 即可。

### 端口

`RADAR_PORT` 环境变量 > `--port` 参数 > `config.json`（默认 18090）。
生产上 **80/443 归 nginx**（443 做 TLS、80 做 ACME 挑战 + 转代理），
radar 自己退到 `127.0.0.1:18090` 只监听本机。
用 80/443 是因为**安全组只放行了 22/80/443/3000/18080**，这两个现成可用。

---

## 运维

```bash
bash deploy/healthcheck.sh              # 服务/页面/指纹/数据 一次看全
bash deploy/healthcheck.sh --refresh    # 额外触发一次采集，验证"更新口子"整条链路
```

`healthcheck.sh` 会专门核对一条不变量：**页面里烘的指纹 == `/api/status` 报的指纹**。
两者一致，"指纹变了 ⇔ 数据更新了"才成立 —— 这是手机端自动刷新的全部依据。
它还会盯着 **IP 证书剩余天数**（只剩不到 2 天就报警），因为证书只有 6.7 天寿命。

常用命令：

```bash
systemctl status radar --no-pager       # 服务状态
journalctl -u radar -f                  # 日志（服务与采集同一个流）
journalctl -u radar-collect -n 50       # 只看定时采集
systemctl list-timers radar.timer       # 下次什么时候跑
systemctl start radar-collect           # 手动跑一次采集

# HTTPS / 证书
systemctl status nginx --no-pager
systemctl list-timers radar-cert-renew.timer          # 下次续期检查
/opt/certbot/bin/certbot certificates                 # 证书剩余有效期
/opt/certbot/bin/certbot renew --deploy-hook 'systemctl reload nginx'   # 手动续期
openssl x509 -enddate -noout -in /etc/letsencrypt/live/118.196.100.121/fullchain.pem
```

---

## 数据源（都是实测结论，别踩同样的坑）

### GitHub
- `github.com` 在当前网络下解析到 `20.205.243.x`，**该段被阻断**；老段 `140.82.11x.x` 可用。
  标准库没有 `curl --resolve` 那种能力，所以 `sources.py` 里手动 `socket → wrap_socket(server_hostname=...)` 保留 SNI。
- TLS 故障是**抖动型**（TCP 连得上但握手超时，同一 IP 时通时不通）
  → 必须多 IP × 多轮重试（8 个 IP / 3 轮 / 单次 15s / 总限 120s）。
- 请求必须带 `Connection: close`，否则页面下完连接不关，白等十几秒。
- 仓库详情走 `api.github.com`（这个域名没被墙）。**注意它的额度是按接口次数算的**：

  | 认证 | 限额 | 现在每轮要花 |
  |---|---|---|
  | 不带 token | **60 次/小时** | 10（详情）+ 20（README + 根目录）= 30 |
  | 带 `github_token` | 5000 次/小时 | 随便加 |

  一天两轮（08:00 / 16:30）刚好卡在 60 的边缘，所以 README / 根目录用
  `pushed_at` 做 key 落盘缓存，第二轮基本零消耗。想彻底不看额度就配个只读 PAT。
- ⚠️ **SSH 密钥和 token 是两回事**：SSH（`git@github.com`）只解决 clone / push；
  `api.github.com` 是 HTTPS 接口，**只认 `Authorization: Bearer <token>`**，
  配好的 SSH 密钥对它一点用都没有。这两个通道互不影响。

### 行情 / 资金流
- **`push2.eastmoney.com` 会直接断连**（`RemoteDisconnected`），不是限流、重试也救不回来。
  **必须用 `push2delay.eastmoney.com`** —— 同一套接口和字段，只是行情延迟约 15 分钟。
  对每天看一次的日报完全无影响。
- `datacenter-web.eastmoney.com` 取两融：报表名 `RPTA_RZRQ_LSHJ`
  （注意不是 `RPT_RZRQ_LSHJ`，后者报"报表配置不存在"）。
- 腾讯 `qt.gtimg.cn` / 新浪 `hq.sinajs.cn` 都是 **GBK**，且新浪必须带 `Referer`，
  作为指数兜底源。`http.py` 的 `sniff_encoding()` 会自动识别。
- **涨跌家数要翻 56 页**：东财 clist 的 `pz` 被硬性截断到 100
  （实测 pz=100/500/1000/5000/6000 一律只回 100 条），全市场约 5500 只要翻 56 页。
  早期版本只取一页，按涨幅排序时第 1 页全是涨停股，数出来的"上涨 100 家"是**完全错误的信号**。
  现在老老实实限速翻完，每天一次、多花约 45 秒，换一个准确的市场广度。
  试过的捷径都不行：`stock/get` 的 f104/f105/f106（涨/跌/平家数）恒为 0/0/100，字段已废弃；
  二分查找 f3 过零点只要 ~6 次请求，但停牌股（f3="-"）排序位置不定，会有几条误差。
- **北向资金已停发**：2024-08-19 起沪深港通不再逐日公布实时资金流，
  金额字段返回 null。所以报告用「板块主力净流入 + 两融余额」替代外资维度，并在报告里注明。

### 口径
- 主力净流入 = 超大单 + 大单（东财按单笔成交额分档：超大单 >100 万、大单 20~100 万）。
- 报告里的每条判断都能对应到 `module.py` 里 `_signals()` 的一个数字条件，改口径只改那一处。

---

## CLI

```bash
python3 -m radar run all              # 全量
python3 -m radar run market_flow      # 只跑一个模块
python3 -m radar run --dry-run        # 只跑不落盘
python3 -m radar run --date 2026-09-13
python3 -m radar build                # 只重建页面（不抓数据）
python3 -m radar serve --port 18090
python3 -m radar status               # 数据状况表
python3 -m radar list                 # 模块清单
python3 -m radar new <name>           # 生成模块骨架
python3 -m radar clean --keep 90      # 清理过期数据
```

依赖只有 `flask` 和 `jinja2`（HTTP 与 Markdown 都是标准库自己写的，少一个依赖少一个坑）。

---

## 本地自测

```bash
python3 smoke_test.py     # 18 段 / 784 项断言：语法编译 + 纯函数 + 整站渲染 + Web API
                          # + 翻译 + 流水线接线 + 大模型客户端 + 代码上下文 + 总结接线
                          # + 安卓壳更新链路 + 读书模块 + 设置页 + 个人规划面板
                          # + 规划到期提醒（系统通知）
```

静态断言查不出"页面上那个按钮点了到底有没有反应"。那类问题靠**无头 Chrome 真跑**
（不装 Playwright / Puppeteer，用 Node 内置的 WebSocket 直连 CDP）：

```bash
bash tools/run_ui_probe.sh tools/reader_probe.mjs     # 阅读位置 / 书签   24 项
bash tools/run_ui_probe.sh tools/settings_probe.mjs   # 设置页 / 提醒      48 项
bash tools/run_ui_probe.sh tools/plans_probe.mjs      # 个人规划 / 提醒    81 项
```

⚠️ 包装脚本里 `--user-data-dir` 必须用 `cygpath -m` 转成 Windows 路径：
`mktemp -d` 给的是 POSIX 路径，Windows 版 `chrome.exe` **不认、静默退出**
（零输出、不监听端口），只会表现成"探针连不上 CDP"，把排查方向引到网络上。
包装脚本现在会**单独判定 Chrome 有没有起来**并打印它的日志，不让它伪装成别的错。

不联网。**没装 jinja2/flask 也能跑**，只是会跳过「整站渲染」和「Web API」两段
（而 PWA 那批断言正好在「整站渲染」里，所以想验 PWA 就得装依赖）：

```bash
python3 -m venv .venv && .venv/bin/pip install jinja2 flask   # Windows: .venv\Scripts\
python3 -m radar build    # Web API 那段要读 public/，先重建一次，否则 /sw.js 还没落盘
python3 smoke_test.py
```

结果写在 `_smoke.txt`（UTF-8），Windows 控制台编码不可靠时看文件。

其中一批断言专盯 **PWA 可安装性**，因为这类问题浏览器既不报错也不提示：
`manifest.icons` 非空、`192×192`/`512×512`/`maskable` 齐备、**图标文件真实像素与声明一致**
（用 192 的图冒充 512 同样会被拒）、`sw.js` 落在**站点根目录**、`apple-touch-icon` 存在。

第 10～12 段专盯**大模型这条新链路**，全部用假的 `_post` / 假 LLM —— **不联网、不花钱**。
要验证的是"出错时会不会炸"，不是"模型答得好不好"（后者只能靠真跑）：

- key 读不到时 `available` 为假、`chat` 直接返回空串**不发请求**；
  测试里必须把 `api_key_env` 指向一个不存在的变量名，否则在配了 key 的机器
  （比如服务器）上会真的读到真 key 然后发真请求 —— 测试就得花钱了
- 超时 / 非 JSON / 缺字段 / `choices` 为空 / 模型不认 `response_format`
  （400 时自动摘掉参数重试一次）—— 每条都要求"返回空串，不抛异常"
- 磁盘缓存换进程仍命中（零请求），确认"同一天跑两轮不重复花钱"
- `pushed_at` 变化必须重取上下文；缓存条数会被修剪
- **回归断言：`report()` 必须能吃下没有 `ai_*` 字段的旧数据。**
  这条是在服务器上真踩出来的：`report()` 里写成 `it["ai_tagline"]`，
  在「对今天之前生成的历史快照重出报告」时直接 `KeyError`。
  `data/` 里躺着一堆老快照，**老数据不该让新代码报错**
