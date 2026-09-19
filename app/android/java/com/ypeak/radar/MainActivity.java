package com.ypeak.radar;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ComponentName;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.ValueCallback;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * 个人情报台 —— 全屏 WebView 外壳。
 *
 * 设计取向：**薄**。所有内容都由服务器渲染，这里只负责"把网页装进一个没有地址栏的窗口"。
 * 好处是改页面完全不用重新发版 —— 站点那边靠 data-fingerprint 轮询自动 reload，
 * 壳这边什么都不用做。
 *
 * ⚠️ 注意区分**两层更新**，它们解决的是不同问题：
 *   · **内容更新** —— 完全自动，且已经由站点负责（app.js 轮询 /api/status，
 *     指纹一变就 reload）。壳一行代码都不用改。
 *   · **壳自身更新** —— 就是 `checkUpdate()` 干的事。壳很少变，
 *     所以要"偶尔查一次 + 别唠叨"，而不是每次启动都弹。
 *
 * 壳自身更新有三种状态，由"自动更新"开关和 `forceDownload` 决定：
 *
 *   | 场景             | 触发方式            | 行为                       |
 *   |------------------|---------------------|----------------------------|
 *   | 关（默认）       | 冷启动 / 页脚按钮   | 弹窗问 → 用户点"立即更新"   |
 *   | 开               | 冷启动（1 小时闸）  | 不弹窗，静默下载 → 拉起安装 |
 *   | 任意             | 设置页"下载并安装"  | 跳过一切闸门，直接下        |
 *
 * ⚠️ **诚实边界**：即便开了自动更新，系统仍会弹一次安装确认。这是安卓对
 *    所有"非应用商店来源"的硬性要求 —— 想真正零点击只能靠 root 或
 *    Device Owner（设备管理），普通 sideload 应用做不到。
 *    所以设置页上把这句话写出来了，不假装"完全不用管"。
 *
 * 五个必须自己处理的地方（不做就会被用户感知为"这破 App 有问题"）：
 *   1. 站外链接（GitHub 仓库、许可证页…）要用系统浏览器打开，不能困在 WebView 里
 *   2. 断网要有可重试的提示页，不能只留一片白屏
 *   3. 返回键要能网页后退，而不是直接退出
 *   4. 顶端下拉刷新 —— 用户对 WebView 应用的默认预期
 *   5. 有新版本要能自己发现，并且**能在应用内装掉** ——
 *      否则用户要么永远停在旧版，要么每次都得跳到浏览器去绕一圈
 */
public class MainActivity extends Activity {

    private PtrWebView web;
    private ProgressBar bar;
    private LinearLayout offlineView;
    private TextView offlineMsg;

    /** 401 只自动重试一次（用带口令的地址重新种一次 Cookie），避免死循环。 */
    private boolean authRetried = false;

    /** 版本检查的最小间隔：壳很少变，启动就弹会变成骚扰。 */
    private static final long UPDATE_CHECK_INTERVAL_MS = 6L * 60 * 60 * 1000;

    /**
     * 开了"自动更新"之后的检查间隔（1 小时）。
     *
     * 为什么比手动模式短得多：用户把这个开关打开，意思就是"你看着办，别问我"。
     * 还按 6 小时算的话，他一天开两次 App 也可能整整一天收不到更新，
     * 会以为开关没生效。1 小时既够及时，也不至于每次冷启动都打一次请求。
     */
    private static final long AUTO_CHECK_INTERVAL_MS = 60L * 60 * 1000;

    /** 下载落地的文件名。
     *
     * 刻意用**固定名**而不是带上版本号：带版本号的话，每升一次版就在
     * 应用外部目录里留一个几十上百 KB 的旧包，永远不会有人来清。
     * 固定名 = 每次覆盖，天然自清理。文件名也不影响安装界面显示什么
     * （那里显示的是 app 的 label），所以没有别的代价。 */
    private static final String DL_FILE = "radar-update.apk";

    /** SharedPreferences 里的键名，集中放一处免得拼错。 */
    private static final String PREF_AUTO = "auto_update";

    /** 个人规划面板的内容，整块 JSON 存在这里。 */
    private static final String PREF_PLANS = "plans_json";

    /**
     * "通知权限已经问过用户了没有"。
     *
     * ⭐ 为什么要记这个：API 33 起那个权限弹框**一辈子只弹一次**
     * （用户拒绝之后系统不再弹，再调 requestPermissions 也是立刻回调失败）。
     * 不记的话，我们会在每次保存规划时"请求"一次而屏幕上什么都没有 ——
     * 用户只觉得偶尔卡一下，而真正该给他的引导（去系统设置里开）
     * 反而永远不出现。
     */
    private static final String PREF_NOTIFY_ASKED = "plan_notify_asked";

    /** 请求通知权限用的 requestCode。 */
    private static final int REQ_NOTIFY = 21;

    /**
     * 掀开规划面板的 JS 钩子（定义在 app.js 的 setupPlans 里）。
     *
     * ⭐ 它是**带返回值**的，不是随手调一下：页面没加载完、或者当时停在别的页面
     * （比如正在读书）时，window.RadarPlansOpen 根本不存在，调用就是一次
     * 静默的空操作 —— 用户看到的是"点了通知进来，什么都没发生"。
     * 拿到 "no" 我们才能改走"先回首页再掀"这条路。
     */
    private static final String JS_OPEN_PLANS =
            "(function(){"
            + "if(typeof window.RadarPlansOpen==='function'){window.RadarPlansOpen();return 'ok';}"
            + "return 'no';})()";

    /**
     * 规划数据的落盘上限（64KB，按 UTF-8 字节算）。
     *
     * 这是个人备忘，正常几十条也就几 KB。设上限是防"某天手滑/脚本写入巨量数据"
     * 把 SharedPreferences 撑爆 —— 它在 App 启动时会被**整体读进内存**，
     * 真塞进去几十 MB，每次冷启动都要多等那一会儿。
     *
     * 超限时**明确返回 false**，让网页提示"没存进去" ——
     * 绝不静默截断：截断后的 JSON 解析不出来，等于把用户写的东西悄悄弄丢了。
     */
    private static final int PLANS_MAX_BYTES = 64 * 1024;

    private SharedPreferences prefs;

    /** 正在显示的更新对话框。Activity 销毁时要主动关掉，否则会带着已死的窗口泄漏。 */
    private AlertDialog updateDialog;

    /** 下载进度框。和 updateDialog 分开持有 —— 两者可能先后出现，别互相踩。 */
    private AlertDialog dlDialog;
    private ProgressBar dlBar;
    private TextView dlMsg;

    /** 下载互斥。网页上的按钮和壳自己的自动检查是两个入口，不加锁会同时下两份。 */
    private volatile boolean downloading = false;

    /**
     * 已经下好、但还差"安装未知应用"权限的那个包。
     *
     * 用户被引到系统设置页授权后回到 App 时，用它把安装接着做完 ——
     * 否则用户授完权回来发现"什么都没发生"，还得自己再点一次下载。
     */
    private File pendingApk;

    /**
     * 这次进来是"点了到期提醒的通知"进来的 —— 要把规划面板掀开。
     *
     * 三种入口都得认：通知点击（onNewIntent，App 还活着）、
     * 通知点击（onCreate，进程被杀过）、以及冷启动时 Intent 里带着这个标记。
     */
    private boolean pendingOpenPlans = false;

    /** 页面加载完没有。没加载完就调 JS 钩子必然是空操作。 */
    private boolean pageLoaded = false;

    /** 已经试过"先回首页再掀面板"没有（面板只在首页上，见 dispatchPendingPanel）。 */
    private boolean panelTriedRoot = false;

    /** 掀面板的尝试次数。有上限 —— 无限重试会把一个偶发问题变成持续耗电。 */
    private int panelTries = 0;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);

        web = new PtrWebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setBuiltInZoomControls(false);
        ws.setDisplayZoomControls(false);
        ws.setSupportZoom(false);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        // 站点只有 https，明文一律不放行
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setBackgroundColor(Color.WHITE);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        // 站点靠 Cookie 鉴权（首次带 ?t= 种下，一年有效），WebView 默认就是开的，
        // 这里显式打开只是为了防止某些 ROM 的默认值不一致。
        CookieManager.getInstance().setAcceptCookie(true);
        web.setOnRefreshListener(new OnRefresh() {
            @Override
            public void onRefresh() {
                hideOffline();
                web.reload();
            }
        });

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setVisibility(View.GONE);

        buildOfflineView();

        FrameLayout root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        root.addView(offlineView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        root.addView(bar, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, dp(3), Gravity.TOP));
        setContentView(root);

        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int p) {
                bar.setProgress(p);
                bar.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
            }
        });

        // ⚠️ prefs 必须在**注册桥之前**就绪 —— 桥方法里会读它（getPlans / getAutoUpdate），
        //    而注册之后页面随时可能调用。原先它在 loadUrl 之后才赋值，只因为
        //    页面加载是异步的才一直没出问题：那是靠时序侥幸，不该留着。
        prefs = getSharedPreferences("radar", Context.MODE_PRIVATE);

        // ⭐ 把"版本 + 检查更新"暴露给网页。
        //    注册必须在 loadUrl **之前** —— 否则首次加载的页面里
        //    window.RadarNative 是 undefined，那条入口要等下次加载才出现。
        //    只暴露"读版本号"和"触发检查"两个只读动作，不暴露任何能改文件/网络的方法。
        web.addJavascriptInterface(new NativeBridge(), "RadarNative");

        if (state == null || web.restoreState(state) == null) {
            web.loadUrl(AppConfig.START_URL);
        }

        // 从通知点进来（进程被杀过的那种）：记下"要把规划面板掀开"。
        // 真正掀开要等页面加载完 —— 见 dispatchPendingPanel。
        pendingOpenPlans = wantsPlans(getIntent());

        // ⭐ 每次打开 App 都把提醒重排一次。
        //    闹钟会被系统在"应用被强行停止 / 清理后台 / 重启"之后清掉，
        //    而"用户打开了 App"就是把它重新排上的最好时机。
        //    同一时刻只存在一个闹钟（固定 requestCode 会替换掉旧的），
        //    所以重复调用是幂等的，不用先判断。
        try {
            Notifier.ensureChannel(this);
            PlanReminder.reschedule(this);
            // 简报的三档闹钟同理：系统清理后台 / 重启都会把 AlarmManager 里的东西
            // 抹掉，而"用户打开了 App"是重新排上的最好时机。
            // 三个时段各用固定 requestCode，重复调用是幂等的。
            BriefAlarm.reschedule(this);
        } catch (Exception ignored) {
            // 排程失败最坏就是"这次不提醒"；让它把开 App 弄崩才是不可接受的
        }

        checkUpdate(false);
    }

    // ============================================================ 给网页的桥
    /**
     * 网页通过 `window.RadarNative` 调到这里。
     *
     * 为什么需要它：自动检查是**每 6 小时一次且静默的** —— 用户既看不到自己装的
     * 是哪个版本，也没法主动问"有没有新版"。这类能力如果用户感知不到，
     * 就等于没做。所以补一个显式入口。
     *
     * ⚠️ 这些方法在 **JavaBridge 线程**被调用，不是主线程 ——
     *    任何碰 UI 的动作都得 runOnUiThread。
     * ⚠️ 参数/返回值只支持基本类型与 String。
     */
    public class NativeBridge {

        /** 当前壳的版本名，如 "1.4"。网页用来显示"我的版本"。 */
        @JavascriptInterface
        public String versionName() {
            return AppConfig.VERSION_NAME;
        }

        /** 当前壳的版本号，网页可拿它和 /api/app 返回的比大小。 */
        @JavascriptInterface
        public int versionCode() {
            return AppConfig.VERSION_CODE;
        }

        /**
         * 用户主动点"检查更新"。**跳过 6 小时间隔**，并且结果一定要有反馈
         * （自动检查可以静默，人主动问就不能装死）。
         */
        @JavascriptInterface
        public void checkForUpdate() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    checkUpdate(true);
                }
            });
        }

        /**
         * "自动更新"开关的当前值。设置页拿它来画开关的初始状态。
         *
         * ⚠️ 真值在原生这边（SharedPreferences），不在网页的 localStorage 里。
         * 网页那份只是显示用 —— 两边各存一份迟早会不一致，
         * 而不一致的表现是"开关看着是开的、实际没生效"，很难查。
         */
        @JavascriptInterface
        public boolean getAutoUpdate() {
            return autoUpdateEnabled();
        }

        /**
         * 开关"自动更新"。
         *
         * 打开时立刻查一次：用户刚表达完"我要自动更新"，如果还要等下一个
         * 小时窗口才动，他会觉得开关没反应。
         */
        @JavascriptInterface
        public void setAutoUpdate(final boolean on) {
            if (prefs != null) {
                prefs.edit().putBoolean(PREF_AUTO, on).apply();
            }
            if (on) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        checkUpdate(false);
                    }
                });
            }
        }

        /** 现在能不能安装应用（即"安装未知应用"是否已授权）。设置页据此决定要不要显示授权卡片。 */
        @JavascriptInterface
        public boolean canInstall() {
            return canInstallPackages();
        }

        /** 跳到"安装未知应用"的授权页。 */
        @JavascriptInterface
        public void openInstallSettings() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    gotoInstallPermission();
                }
            });
        }

        /**
         * 设置页上的"下载并安装"：跳过一切闸门，直接下最新的那个包。
         *
         * 和 {@link #checkForUpdate()} 的区别在于**不会只弹个对话框** ——
         * 用户是在设置页明确按了这个按钮，意图已经足够清楚了。
         */
        @JavascriptInterface
        public void downloadUpdate() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    checkUpdate(true, true);
                }
            });
        }

        /* ---------------- 个人规划：只存在这台手机上 ----------------
           为什么数据放在壳里、而不是网页的 localStorage：
             · 规划是"我自己的东西"，不该依赖站点缓存是否被清，
               也不该跟着 WebView 数据清理一起消失；
             · 放 SharedPreferences 里 — App 在，数据就在；
             · 而且**完全不经过服务器**（服务器上不留任何痕迹）。
           代价是换手机/清数据就没了 —— 这是用户自己选的取舍，
           真要同步得另加一套服务端存储，不是这里的默认行为。 */

        /**
         * 读回规划 JSON。
         *
         * 从未保存过时返回**空串**（而不是 "{}"）：网页据此区分
         * "第一次用"和"保存过一个空列表"，好走对应文案。
         */
        @JavascriptInterface
        public String getPlans() {
            SharedPreferences p = prefs;
            if (p == null) return "";
            String s = p.getString(PREF_PLANS, "");
            return s == null ? "" : s;
        }

        /**
         * 整块写回规划 JSON。返回**到底存住了没有**。
         *
         * 用 commit() 而不是 apply()：本方法跑在 **JavaBridge 线程**（不是主线程），
         * 这点磁盘 I/O 不会卡界面；而 apply() 是异步的、拿不到结果 ——
         * 那就只能永远返回成功，等于骗网页"已保存"。
         * 网页拿到 false 必须**显式提示**，做不到就别假装做到。
         */
        @JavascriptInterface
        public boolean setPlans(String json) {
            SharedPreferences p = prefs;
            if (p == null || json == null) return false;
            // 按 UTF-8 字节算：中文一个字 3 字节，用 length() 会严重低估
            if (json.getBytes(StandardCharsets.UTF_8).length > PLANS_MAX_BYTES) return false;
            try {
                boolean ok = p.edit().putString(PREF_PLANS, json).commit();
                if (ok) {
                    // ⭐ 存住了就顺手重排提醒。挂在这里而不是让网页显式再调一次：
                    //    写规划**只有这一条路径**，挂在这儿就不可能"忘了排"——
                    //    而忘排的表现是"设了提醒但从来没人提醒我"，完全静默。
                    PlanReminder.reschedule(MainActivity.this);
                    // 顺带把"已经不该挂着"的通知收掉（做完了/删了的那条）。
                    // ⚠️ 注意这不发生在"打开 App"那条路径上 —— 开 App 不等于看过提醒。
                    PlanReminder.dropStaleNotification(MainActivity.this);
                    maybeAskNotify();
                }
                return ok;
            } catch (Exception e) {
                return false;
            }
        }

        /** 规划数据现在占了多少字节。设置页/排查用，正常情况用不到。 */
        @JavascriptInterface
        public int plansBytes() {
            SharedPreferences p = prefs;
            if (p == null) return 0;
            String s = p.getString(PREF_PLANS, "");
            return s == null ? 0 : s.getBytes(StandardCharsets.UTF_8).length;
        }

        /* ---------------- 规划提醒（到期当天 9:00 的系统通知） ----------------

           分工：**什么时候响**全部由原生算（见 PlanReminder）——它必须在
           App 没打开、甚至没联网的时候也自洽，所以不能依赖网页。
           网页这边只做三件事：把事实显示出来、把用户送到该去的地方、
           给一个"立刻发一条"的验证入口。

           ⚠️ 全都是"读事实"或"跳转"，没有任何能改排程结果的方法 ——
              排程只跟着 plans_json 走，而写 plans_json 只有 setPlans 一条路。

           ⚠️ 刻意**只暴露这几个**：像 canNotify / exactAlarmAllowed 这种
              "同一件事实的第二条查询路径"一律不单独开方法 ——
              reminderInfo() 已经把它们一起给了。两处来源迟早会不一致，
              而不一致的表现是"设置页说开着、实际没开"。
              （smoke_test 里有一条断言盯着"暴露的桥方法都被用到了"。 */

        /**
         * 设置页要的那点事实，一次性给全（JSON 字符串，字段含义见 PlanReminder.nextInfo）。
         *
         * 为什么要 JSON 而不是原生拼好的句子：文案属于展示层。原生只回答
         * "事实是什么"，改措辞就不用重新发版装 APK。
         */
        @JavascriptInterface
        public String reminderInfo() {
            return PlanReminder.nextInfo(MainActivity.this);
        }

        /**
         * 要通知权限。
         *
         * ⚠️ 弹框只出现一次。所以原生自己判断："还没问过"就弹框，
         *    "问过了 / 是在系统设置里关掉的"就直接把人送到系统设置页 ——
         *    否则表现就是"点了按钮什么也没发生"。
         *    也正因为这条兜底，"去通知设置"不需要再单独开一个方法。
         */
        @JavascriptInterface
        public void requestNotify() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    askNotifyPermission();
                }
            });
        }

        /** 跳「闹钟与提醒」权限页（API 31+ 才有这一项）。 */
        @JavascriptInterface
        public void openExactAlarmSettings() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    gotoExactAlarmSettings();
                }
            });
        }

        /**
         * 这台机器要不要额外去开"自启动"？返回厂商名（如"小米"），不需要就是空串。
         *
         * 为什么这事归原生管：国产 ROM 默认禁自启动，被禁之后 AlarmManager
         * 排的闹钟**根本不响**且不报错 —— 正是本项目最忌讳的那类静默失效。
         * 网页拿到厂商名才好在设置页上把"去哪里打开"摆出来。
         */
        @JavascriptInterface
        public String autoStartVendor() {
            return vendorNeedingAutoStart();
        }

        /**
         * 打开厂商的自启动管理页。
         *
         * 没有返回值：桥方法跑在 JavaBridge 线程，而 startActivity 要回主线程，
         * 异步了就拿不到结果。所以"能不能打开"不靠返回值，靠**打开不了时给一句
         * 明确的话**（提示去应用详情里找），不让用户面对一个点了没反应的按钮。
         */
        @JavascriptInterface
        public void openAutoStartSettings() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (!launchAutoStartSettings()) {
                        toastUi(getString(R.string.notify_no_autostart_page));
                        gotoAppDetails();
                    }
                }
            });
        }

        /**
         * 立刻发一条测试通知。
         *
         * 这条链路（权限 → 渠道 → 通知 → 点开进面板）没有办法在无头环境里自动验证，
         * 所以必须给用户一个一按就能看见结果的按钮 —— 否则"提醒到底通没通"
         * 只能等到期那天才知道，而那天往往正是要用的时候。
         */
        @JavascriptInterface
        public boolean testNotify() {
            boolean ok = PlanReminder.testNow(MainActivity.this);
            if (!ok) {
                toastLater(getString(R.string.notify_test_failed));
            }
            return ok;
        }

        /* ---------------- 每日简报（早间 / 尾盘 / 收盘） ----------------

           分工和规划提醒**正好相反**：这里排程还是原生算（原因同前 —— 开机时
           WebView 没加载，排程信息不能只活在网页里），但**内容要去服务器取**。
           闹钟响的时候 App 可能压根没打开过，所以原生必须自己会发这个请求，
           口令是构建期嵌进 AppConfig 的。

           三个方法的分工：
             briefInfo()  读事实（开关 / 时刻 / 下一次什么时候响 / 权限）
             setBrief()   写配置（唯一能改排程结果的路径，写完自己重排）
             testBrief()  立刻取一条发出来 —— 这条链路没法在无头环境里自动验证，
                          必须给一个一按就能看见结果的入口，否则"到底通没通"
                          只能等到明天早上 8 点才知道。
        */

        /**
         * 设置页要的简报事实，一次性给全（JSON，字段含义见 BriefAlarm.nextInfo）。
         *
         * 和 reminderInfo() 一样是 JSON 而不是拼好的句子：文案归网页管，
         * 改措辞不用重新发版装 APK。
         */
        @JavascriptInterface
        public String briefInfo() {
            return BriefAlarm.nextInfo(MainActivity.this);
        }

        /**
         * 写回简报配置（三个时段的开关与时刻）。返回**到底存住了没有**。
         *
         * 用 commit() 且存住了才重排 —— 见 setPlans 里的同类说明。
         * 这里**不做** Toast 反馈：网页上已经有状态行，同一个结果说两遍是噪音。
         */
        @JavascriptInterface
        public boolean setBrief(String json) {
            boolean ok = BriefAlarm.save(MainActivity.this, json);
            if (ok) {
                maybeAskNotify();
            }
            return ok;
        }

        /**
         * 立刻取一条简报发出来，返回结果码给网页去说人话：
         *   ok        发出去了
         *   nonotify  取到了，但没有通知权限
         *   nofetch   取不到（网络/服务器/结构不对）
         *   dup       这个时段今天已经发过（手动测试时不会走到）
         *
         * ⚠️ **同步**执行，会阻塞 JavaBridge 线程最多十几秒（连接 5s + 读 7s）。
         *    这是刻意的：异步就返回不了结果，而这个按钮存在的全部意义
         *    就在于"一按就知道通没通"。JavaBridge 线程不是 UI 线程，
         *    WebView 不会被卡住，只是这一次调用要等一会儿。
         */
        @JavascriptInterface
        public String testBrief(String slot) {
            return BriefAlarm.testNow(MainActivity.this, slot);
        }
    }

    // ================================================================== 检测新版本
    /**
     * 问服务器"现在最新是哪个版本"，比自己新就提示 —— 或者，开了自动更新就直接下。
     *
     * 几条刻意的选择：
     *   · **放后台线程** —— 网络请求绝不能上主线程（会 ANR）。
     *   · **失败就静默** —— 自动查更新失败绝不能打扰用户，它只是个附赠能力。
     *   · **间隔随模式而变**：手动模式 6 小时最多一次（免得变成骚扰），
     *     自动模式 1 小时（用户已经说了"你看着办"）。
     *   · **"跳过这个版本"只在会弹窗的那条路径上生效** —— 开了自动更新时
     *     那个按钮根本看不到，再去拦就成了开关不起作用。
     *   · **主动模式跳过所有闸门** —— 用户点了"检查更新"却什么也不发生，是最糟的体验。
     *   · **下载在 App 内完成**（见 startDownload）。以前是丢给系统浏览器，
     *     用户得在两个应用之间来回跳，下完还要自己找安装包 —— 现在不用了。
     *
     * @param manual        人主动触发的（必须有反馈）
     * @param forceDownload 不管开关如何，发现新版就直接下（设置页的"下载并安装"）
     */
    private void checkUpdate(final boolean manual) {
        checkUpdate(manual, false);
    }

    private void checkUpdate(final boolean manual, final boolean forceDownload) {
        final boolean auto = autoUpdateEnabled();
        if (!manual) {
            // 只有自动触发才看间隔。主动模式跳过 —— 见上面注释。
            final long interval = auto ? AUTO_CHECK_INTERVAL_MS : UPDATE_CHECK_INTERVAL_MS;
            final long last = prefs.getLong("last_check", 0L);
            if (System.currentTimeMillis() - last < interval) {
                return;
            }
        }
        // 这一趟要不要"静默自动下"：要开过开关，且不是设置页里手动按的下载。
        final boolean autoThisRun = auto && !forceDownload;

        new Thread(new Runnable() {
            @Override
            public void run() {
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(AppConfig.API_APP).openConnection();
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(8000);
                    conn.setRequestProperty("Accept", "application/json");
                    if (conn.getResponseCode() != 200) {
                        if (manual) {
                            toastLater(getString(R.string.update_check_failed));
                        }
                        return;              // 自动模式静默：查更新失败不该打扰用户
                    }
                    StringBuilder sb = new StringBuilder();
                    BufferedReader r = new BufferedReader(
                            new InputStreamReader(conn.getInputStream(), "UTF-8"));
                    try {
                        String line;
                        while ((line = r.readLine()) != null) {
                            sb.append(line);
                        }
                    } finally {
                        r.close();
                    }
                    final JSONObject o = new JSONObject(sb.toString());
                    final int code = o.optInt("versionCode", 0);
                    final String name = o.optString("versionName", "");
                    // 服务器会给**确定那个文件**的路径（形如 /dl/radar-1.5.apk）。
                    // 老服务器没有这个字段时是空串 → 下载时回落到 /app 别名。
                    final String path = o.optString("url", "");
                    final String shown = (name == null || name.length() == 0)
                            ? String.valueOf(code) : name;

                    prefs.edit().putLong("last_check", System.currentTimeMillis()).apply();

                    // 已经是最新：自动模式安静退场，主动模式必须告诉用户"真的没新版"
                    if (code <= AppConfig.VERSION_CODE) {
                        if (manual) {
                            toastLater(getString(R.string.update_is_latest, AppConfig.VERSION_NAME));
                        }
                        return;
                    }
                    // 自动模式尊重"跳过这个版本"；用户主动来问时**不再拦截** ——
                    // 他既然主动问了，就是想知道，哪怕之前点过"跳过"。
                    if (!manual && !autoThisRun && prefs.getInt("skipped", 0) >= code) {
                        return;
                    }
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            if (autoThisRun) {
                                // 用户要的就是"不用管"：不弹窗，直接下。
                                // 进度也不弹框 —— 冷启动时糊一个不能取消的进度框
                                // 会把"打开看一眼"这件事挡住，得不偿失。
                                toastUi(getString(R.string.update_auto_started, shown));
                                startDownload(code, shown, path, true);
                            } else {
                                promptUpdate(code, shown, path);
                            }
                        }
                    });
                } catch (Exception e) {
                    // 断网、JSON 坏了、被墙…… 自动模式一律当"没更新"；
                    // 主动模式得给个交代，不能让用户对着屏幕干等。
                    if (manual) {
                        toastLater(getString(R.string.update_check_failed));
                    }
                } finally {
                    if (conn != null) {
                        conn.disconnect();
                    }
                }
            }
        }, "update-check").start();
    }

    /** 开没开"自动更新"。prefs 在某些早期调用路径上可能还没初始化。 */
    private boolean autoUpdateEnabled() {
        return prefs != null && prefs.getBoolean(PREF_AUTO, false);
    }

    /** 在主线程上弹一个 Toast；Activity 已经不在了就安静吞掉。 */
    private void toastUi(final String msg) {
        if (isFinishing() || isDestroyed()) {
            return;
        }
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    /** 从后台线程发 Toast —— Toast 必须回到主线程。 */
    private void toastLater(final String msg) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                toastUi(msg);
            }
        });
    }

    private void promptUpdate(final int code, final String shown, final String path) {
        // ⚠️ 必须同时查 isFinishing() 和 **isDestroyed()**：
        //    查更新的线程可能在 Activity 已经销毁之后才回到主线程，
        //    这时 show() 会抛 BadTokenException 直接崩掉。
        //    （本 Activity 锁了竖屏且吃掉了 configChanges，所以旋转不会重建；
        //      但"不保留活动"/内存压力仍可能销毁它 —— 这类崩溃低频但很难查。）
        if (isFinishing() || isDestroyed()) {
            return;
        }
        AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle(R.string.update_title)
                .setMessage(getString(R.string.update_msg, shown, AppConfig.VERSION_NAME))
                .setPositiveButton(R.string.update_now, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        startDownload(code, shown, path, false);
                    }
                })
                .setNegativeButton(R.string.update_later, null)
                .setNeutralButton(R.string.update_skip, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        // 记住"这个版本我跳过了"，下个版本再提醒
                        prefs.edit().putInt("skipped", code).apply();
                    }
                })
                .create();
        updateDialog = dlg;
        dlg.show();
    }

    // ================================================================== 应用内下载 + 安装
    /**
     * 在 App 内把安装包下下来，下完交给系统安装器。
     *
     * 为什么不再"丢给系统浏览器"：那条路每一步都要用户动手 ——
     * 点更新 → 跳浏览器 → 等下载 → 再点通知/文件 → 点安装。
     * 用户的原话是"每次都需要我自己点"，指的就是这个。
     *
     * @param silent 自动更新模式：不弹进度框。用户在冷启动时糊一个不能取消的
     *               进度框挡在内容前面，比"没提示"更烦。
     */
    private void startDownload(final int code, final String shown,
                               final String path, final boolean silent) {
        if (downloading) {
            return;                      // 网页按钮和自动检查是两个入口，必须互斥
        }
        File dir = getExternalFilesDir(null);
        if (dir == null) {
            dir = getCacheDir();
        }
        final File out = new File(dir, DL_FILE);
        downloading = true;
        if (!silent) {
            showDownloadDialog(shown);
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                HttpURLConnection conn = null;
                InputStream in = null;
                FileOutputStream fos = null;
                try {
                    // ⚠️ 路径来自服务器（/api/app 的 url 字段），不是我们自己拼的。
                    //    它一定是 `/dl/xxx.apk` 这种站内相对路径；但**不能盲信**：
                    //    只要不是以 / 开头，就退回官方别名，避免被拼成奇怪的外站地址。
                    String rel = (path != null && path.startsWith("/") && path.length() > 1
                            && path.indexOf("//") < 0) ? path : "/app";
                    String url = AppConfig.BASE_URL + rel + AppConfig.TOKEN_QUERY;

                    conn = (HttpURLConnection) new URL(url).openConnection();
                    conn.setConnectTimeout(10000);
                    conn.setReadTimeout(30000);       // 包有百来 KB，给宽一点
                    conn.setInstanceFollowRedirects(true);
                    if (conn.getResponseCode() != 200) {
                        failDownload();
                        return;
                    }
                    final int total = conn.getContentLength();
                    in = conn.getInputStream();
                    fos = new FileOutputStream(out);   // 固定名 → 覆盖上一次的残包
                    byte[] buf = new byte[16 * 1024];
                    long got = 0;
                    int n, lastPct = -1;
                    while ((n = in.read(buf)) > 0) {
                        fos.write(buf, 0, n);
                        got += n;
                        if (total > 0) {
                            int pct = (int) (got * 100 / total);
                            if (pct != lastPct) {
                                lastPct = pct;
                                setDownloadProgress(shown, pct);
                            }
                        }
                    }
                    fos.flush();
                    fos.close();
                    fos = null;
                    in.close();
                    in = null;

                    // ⚠️ 这里必须验"真的下到了东西"。
                    //    网络在半路断掉时，read() 可能正常返回 -1（EOF）而**不抛异常**，
                    //    于是我们手里就是一个截断的 APK。直接丢给安装器的话，
                    //    用户看到的是"解析包时出现问题"这种莫名其妙的错误。
                    if (out.length() <= 0) {
                        failDownload();
                        return;
                    }
                    downloading = false;
                    hideDownloadDialog();
                    if (silent) {
                        toastLater(getString(R.string.update_auto_ready));
                    }
                    installApk(out);
                } catch (Exception e) {
                    // 断网、磁盘满、下载中被切…… 一律按"下载失败"处理，别崩
                    failDownload();
                } finally {
                    try { if (fos != null) fos.close(); } catch (Exception ignored) { }
                    try { if (in != null) in.close(); } catch (Exception ignored) { }
                    if (conn != null) {
                        conn.disconnect();
                    }
                }
            }
        }, "update-download").start();
    }

    private void failDownload() {
        downloading = false;
        hideDownloadDialog();
        toastLater(getString(R.string.update_dl_failed));
    }

    /** 进度框。刻意**不能取消**：下到一半退出会留下一个残包，反而更难解释。 */
    private void showDownloadDialog(final String shown) {
        if (isFinishing() || isDestroyed()) {
            return;
        }
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(22);
        box.setPadding(pad, pad, pad, pad);

        dlMsg = new TextView(this);
        dlMsg.setText(getString(R.string.update_downloading, shown));

        dlBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        dlBar.setMax(100);
        // 先转圈：Content-Length 不一定拿得到（分块传输就没有），
        // 拿到了再切成确定进度。反过来做的话，拿不到长度时进度条会一直卡在 0%。
        dlBar.setIndeterminate(true);

        box.addView(dlMsg, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(14);
        box.addView(dlBar, lp);

        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle(R.string.update_dl_title)
                .setView(box)
                .setCancelable(false)
                .create();
        dlDialog = d;
        d.show();
    }

    private void setDownloadProgress(final String shown, final int pct) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (dlBar == null) {
                    return;
                }
                if (dlBar.isIndeterminate()) {
                    dlBar.setIndeterminate(false);
                    dlBar.setProgress(0);
                }
                dlBar.setProgress(pct);
                if (dlMsg != null) {
                    dlMsg.setText(getString(R.string.update_downloading_pct, shown, pct));
                }
            }
        });
    }

    private void hideDownloadDialog() {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (dlDialog != null) {
                    if (dlDialog.isShowing()) {
                        dlDialog.dismiss();
                    }
                    dlDialog = null;
                }
                dlBar = null;
                dlMsg = null;
            }
        });
    }

    // ------------------------------------------------------------ 交给系统安装器
    /**
     * 把下好的包交给系统安装器。
     *
     * ⚠️ 没授权"安装未知应用"时，**必须先引导授权，而不是硬拉安装器** ——
     * 硬拉的结果是安装界面弹出来又立刻失败，用户只看到"更新没反应"。
     * 授权走系统设置页，回来后由 onResume 接着装（用户不用再点一次）。
     */
    private void installApk(final File apk) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (isFinishing() || isDestroyed()) {
                    return;
                }
                if (!canInstallPackages()) {
                    pendingApk = apk;
                    toastUi(getString(R.string.update_need_perm));
                    gotoInstallPermission();
                    return;
                }
                pendingApk = null;
                launchInstaller(apk);
            }
        });
    }

    /** 能不能"安装未知应用"。API 26 之前没有这个开关，一律算可以。 */
    private boolean canInstallPackages() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return true;
        }
        try {
            return getPackageManager().canRequestPackageInstalls();
        } catch (Exception e) {
            return true;                 // 判断不了就别拦着，交给安装器去报错
        }
    }

    /** 跳到本应用的"安装未知应用"授权页。 */
    private void gotoInstallPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        try {
            startActivity(new Intent(
                    android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName())));
            return;
        } catch (ActivityNotFoundException ignored) {
            // 个别 ROM 没做这个页面
        }
        try {
            startActivity(new Intent(
                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException ignored) {
            // 连应用详情页都没有（极罕见）：那就只能靠用户自己找设置了
        }
    }

    // =================================================== 规划提醒：权限与跳转
    /**
     * 存下一条"定了日期"的规划时，顺口问一次通知权限。
     *
     * 为什么挑这个时机：用户此刻刚表达了"我要在某天做这件事"，弹框的来意
     * 不言自明。冷启动就弹的话，用户还不知道这应用能提醒什么，多半直接拒绝，
     * 而系统那个弹框**只有这一次机会** —— 拒了就再也不能弹了。
     *
     * 只问一次（PREF_NOTIFY_ASKED）。之后靠设置页/面板上的显式入口。
     */
    private void maybeAskNotify() {
        if (Notifier.canNotify(this)) return;
        if (wasNotifiedAsked()) return;
        if (!PlanReminder.hasDatedPlan(this)) return;
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                askNotifyPermission();
            }
        });
    }

    private boolean wasNotifiedAsked() {
        return prefs != null && prefs.getBoolean(PREF_NOTIFY_ASKED, false);
    }

    /**
     * 要通知权限，或者把用户送到能开它的地方。
     *
     * ⚠️ 必须区分"还没问过"和"问过了/被系统关掉了"：
     *    · 还没问过 → requestPermissions（弹系统框，只有这一次机会）
     *    · 问过了  → 直接跳系统设置页。再调 requestPermissions 是不出声的空转，
     *                表现就是"点了按钮什么也没发生"。
     */
    private void askNotifyPermission() {
        if (Notifier.canNotify(this)) {
            toastUi(getString(R.string.notify_already));
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && !Notifier.hasRuntimePermission(this)
                && !wasNotifiedAsked()) {
            prefs.edit().putBoolean(PREF_NOTIFY_ASKED, true).apply();
            requestPermissions(
                    new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
            return;
        }
        toastUi(getString(R.string.notify_need_perm));
        gotoNotifySettings();
    }

    /** 跳本应用的通知设置页。API 26 起有专门的页面，更早的版本退到应用详情。 */
    private void gotoNotifySettings() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            try {
                Intent it = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
                it.putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                startActivity(it);
                return;
            } catch (Exception ignored) {
                // 个别 ROM 没做这个页面
            }
        }
        gotoAppDetails();
    }

    /** 跳到「闹钟与提醒」特殊权限页。API 31 之前没有这一项，什么都不做。 */
    private void gotoExactAlarmSettings() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return;
        }
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                    Uri.parse("package:" + getPackageName())));
            return;
        } catch (Exception ignored) {
            // 有的 ROM 把这一页藏起来了，退到应用详情让用户自己找
        }
        gotoAppDetails();
    }

    private void gotoAppDetails() {
        try {
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException ignored) {
        }
    }

    /**
     * 这台机器是不是"默认禁自启动"的那类，是的话返回厂商名。
     *
     * 为什么值得单独判一次：国产 ROM（小米/华为/OPPO/vivo/三星…）默认禁止
     * 应用自启动，被禁之后 AlarmManager 排的闹钟**根本不会响**，也不会有任何
     * 报错 —— 表现就是"我明明设了提醒，却从来没被提醒过"。
     * 这类静默失效只能靠"把入口摆到用户面前"来解决。
     *
     * 判断只看厂商名，不做机型白名单：宁可多显示一个入口，
     * 也不要因为认不出来而漏掉真正需要它的那台机器。
     */
    private String vendorNeedingAutoStart() {
        String m = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER.toLowerCase(Locale.ROOT);
        String b = Build.BRAND == null ? "" : Build.BRAND.toLowerCase(Locale.ROOT);
        String mb = m + " " + b;
        if (mb.contains("xiaomi") || mb.contains("redmi") || mb.contains("poco")) return "小米";
        if (mb.contains("huawei") || mb.contains("honor")) return "华为";
        if (mb.contains("oppo") || mb.contains("realme") || mb.contains("oneplus")) return "OPPO";
        if (mb.contains("vivo") || mb.contains("iqoo")) return "vivo";
        if (mb.contains("samsung")) return "三星";
        if (mb.contains("meizu")) return "魅族";
        return "";
    }

    /**
     * 试着打开厂商的自启动管理页。
     *
     * 这些页面都不是公开 API，机型之间差别很大，所以只能**按常见清单逐个试**，
     * 试不通就返回 false 让调用方给一句明白话（去应用详情里找）。
     * 一个点了没反应的按钮比没有按钮更糟 —— 这是本项目一贯的判断。
     */
    private boolean launchAutoStartSettings() {
        String[][] pages = {
                {"com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"},
                {"com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                {"com.coloros.safecenter", "com.coloros.safecenter.startupapp.StartupAppListActivity"},
                {"com.oppo.safe", "com.oppo.safe.permission.startup.StartupAppListActivity"},
                {"com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
                {"com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity"},
                {"com.samsung.android.lool", "com.samsung.android.sm.ui.battery.BatteryActivity"},
                {"com.meizu.safe", "com.meizu.safe.security.SHOW_APPSEC"},
        };
        for (int i = 0; i < pages.length; i++) {
            try {
                Intent it = new Intent();
                it.setComponent(new ComponentName(pages[i][0], pages[i][1]));
                startActivity(it);
                return true;
            } catch (Exception ignored) {
                // 这台机器没有这一页：试下一个
            }
        }
        return false;
    }

    /**
     * 权限回调。
     *
     * 拿到权限要**立刻重排一次**：被拒的时候我们排的是不精确闹钟（甚至是没排），
     * 授完之后不重排的话，用户会发现"给了权限还是晚点才响"。
     */
    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(code, perms, granted);
        if (code != REQ_NOTIFY) {
            return;
        }
        boolean ok = granted != null && granted.length > 0
                && granted[0] == PackageManager.PERMISSION_GRANTED;
        toastUi(getString(ok ? R.string.notify_granted : R.string.notify_denied));
        if (ok) {
            try {
                PlanReminder.reschedule(this);
            } catch (Exception ignored) {
            }
        }
        refreshReminderUi();
    }

    /**
     * 真的拉起安装。
     *
     * 用 `content://` 而不是 `file://` —— 从 API 24 起后者跨应用传递会直接抛
     * FileUriExposedException。URI 由 {@link ApkProvider} 提供，
     * 并用 FLAG_GRANT_READ_URI_PERMISSION 临时授权给安装器。
     */
    private void launchInstaller(File apk) {
        Uri uri = ApkProvider.uriFor(apk.getName());
        Intent view = new Intent(Intent.ACTION_VIEW);
        view.setDataAndType(uri, "application/vnd.android.package-archive");
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(view);
            return;
        } catch (ActivityNotFoundException ignored) {
            // 少数 ROM 只认老式的 INSTALL_PACKAGE，继续往下试
        }
        Intent alt = new Intent(Intent.ACTION_INSTALL_PACKAGE);
        alt.setData(uri);
        alt.putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true);
        alt.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(alt);
        } catch (ActivityNotFoundException e) {
            toastUi(getString(R.string.update_no_installer));
        }
    }

    // ------------------------------------------------------------------ 断网提示页
    private void buildOfflineView() {
        offlineView = new LinearLayout(this);
        offlineView.setOrientation(LinearLayout.VERTICAL);
        offlineView.setGravity(Gravity.CENTER);
        offlineView.setBackgroundColor(getResources().getColor(R.color.brand_bg));
        offlineView.setVisibility(View.GONE);

        offlineMsg = new TextView(this);
        offlineMsg.setText(R.string.offline_hint);
        offlineMsg.setTextColor(getResources().getColor(R.color.brand_text));
        offlineMsg.setTextSize(15f);
        offlineMsg.setGravity(Gravity.CENTER);
        int pad = dp(28);
        offlineMsg.setPadding(pad, 0, pad, dp(18));

        Button retry = new Button(this);
        retry.setText(R.string.retry);
        retry.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                hideOffline();
                web.reload();
            }
        });

        offlineView.addView(offlineMsg, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        offlineView.addView(retry, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    private void showOffline(String why) {
        if (why != null && why.length() > 0) {
            offlineMsg.setText(getString(R.string.offline_hint) + "\n\n" + why);
        }
        offlineView.setVisibility(View.VISIBLE);
    }

    private void hideOffline() {
        offlineView.setVisibility(View.GONE);
    }

    // ------------------------------------------------------------------ 点通知进面板
    /** 这个 Intent 是不是"点到期提醒进来的"。 */
    private boolean wantsPlans(Intent it) {
        return it != null && it.getBooleanExtra(PlanReminder.EXTRA_OPEN_PLANS, false);
    }

    /**
     * 把"掀开规划面板"这件事交给网页。
     *
     * ⚠️ 这里绝不能一进来就无脑 evaluateJavascript。两种情况会让那次调用变成
     *    **静默的空操作**：页面还没加载完（冷启动），或者当时根本没停在首页
     *    （比如用户正在读书）—— 这两种情况下 window.RadarPlansOpen 都不存在，
     *    表现就是"点了通知进来，什么都没发生"。
     *
     *    所以钩子带返回值，按结果分三条路：
     *      ok          → 成了，收工
     *      no          → 先回首页（面板只在首页上），加载完再试
     *      还没加载完  → 什么都不做，等 onPageFinished 再调一次这里
     *
     *    重试有上限。一个偶发的失败不该变成持续耗电的后台轮询。
     */
    private void dispatchPendingPanel() {
        if (!pendingOpenPlans || !pageLoaded || web == null) {
            return;
        }
        try {
            web.evaluateJavascript(JS_OPEN_PLANS, new ValueCallback<String>() {
                @Override
                public void onReceiveValue(String v) {
                    // evaluateJavascript 回的是 JSON 串，成功时形如 "ok"（带引号）
                    if (v != null && v.indexOf("ok") >= 0) {
                        pendingOpenPlans = false;
                        panelTriedRoot = false;
                        panelTries = 0;
                        return;
                    }
                    if (!panelTriedRoot) {
                        panelTriedRoot = true;
                        if (web != null) {
                            web.loadUrl(AppConfig.START_URL);   // onPageFinished 会再调回来
                        }
                        return;
                    }
                    if (panelTries++ < 8) {
                        if (web != null) {
                            web.postDelayed(new Runnable() {
                                @Override
                                public void run() {
                                    dispatchPendingPanel();
                                }
                            }, 350);
                        }
                        return;
                    }
                    pendingOpenPlans = false;      // 放弃，别无限重试
                }
            });
        } catch (Exception ignored) {
            pendingOpenPlans = false;
        }
    }

    /**
     * 让网页把"提醒"相关的状态重画一遍（页面里没有这个钩子就什么也不做）。
     *
     * 用在两个地方：从系统设置页授权回来（onResume）、以及权限弹框有结果时。
     * 这两种情况下 WebView 自己收不到任何事件（系统设置是另一个 Activity，
     * 权限框压根不是 Activity），不主动喊一声，页面上的状态就会一直停在旧值。
     */
    private void refreshReminderUi() {
        if (web == null) {
            return;
        }
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    web.evaluateJavascript(
                            "window.RadarReminderRefresh&&window.RadarReminderRefresh()", null);
                } catch (Exception ignored) {
                }
            }
        });
    }

    // ------------------------------------------------------------------ WebViewClient
    private class Client extends WebViewClient {

        /** 站内导航留在壳里；站外链接交给系统浏览器 —— 否则用户点了 GitHub 就出不来了。 */
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
            if (!"http".equals(scheme) && !"https".equals(scheme)) {
                return true;   // intent:// 之类一律不接
            }
            if (AppConfig.HOME_HOST.equals(u.getHost())) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (ActivityNotFoundException ignored) {
                // 没有能处理它的应用就算了，别崩
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            hideOffline();
            // 页面一开始重载，之前那套 JS 就都不在了 —— 掀面板的钩子自然也没了
            pageLoaded = false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            authRetried = false;
            if (bar.getProgress() >= 100) {
                bar.setVisibility(View.GONE);
            }
            pageLoaded = true;
            // ⭐ 页面加载完是"掀面板"唯一的可靠时机：这一刻 setupPlans() 才跑过，
            //    window.RadarPlansOpen 才真的存在。
            dispatchPendingPanel();
        }

        /** 断网 / DNS 失败 / 证书问题 —— 只有主文档失败才盖提示页，图片挂了不该盖。 */
        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) {
                CharSequence desc = err == null ? null : err.getDescription();
                showOffline(desc == null ? null : desc.toString());
            }
        }

        /**
         * Cookie 过期时站点返回 401。此时用带口令的地址重来一次就能自愈 ——
         * 用户在手机上没法手动清 Cookie，这个自愈是必须的。
         */
        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest req,
                                       android.webkit.WebResourceResponse res) {
            if (!req.isForMainFrame() || res == null || res.getStatusCode() != 401) {
                return;
            }
            if (authRetried) {
                showOffline(getString(R.string.auth_failed));
                return;
            }
            authRetried = true;
            web.loadUrl(AppConfig.START_URL);
        }
    }

    // ------------------------------------------------------------------ 生命周期 / 返回键
    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    /**
     * 点通知进来时，如果 App 还活着，走的是这里而不是 onCreate
     * （MainActivity 是 singleTask）。
     *
     * ⚠️ 必须 setIntent：不设的话 getIntent() 一直是**最初那次**的 Intent，
     *    "这次是点提醒进来的"这个信息当场就丢了 —— 而它的表现恰好是
     *    "第一次点有效、之后再点就没反应了"，最难查的那一类。
     */
    @Override
    protected void onNewIntent(Intent it) {
        super.onNewIntent(it);
        setIntent(it);
        if (wantsPlans(it)) {
            pendingOpenPlans = true;
            panelTriedRoot = false;
            panelTries = 0;
            dispatchPendingPanel();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        // 用户刚去系统设置里授权"安装未知应用"，现在回来了 —— 把之前下好的包接着装上。
        // 没有这一步的话，用户授完权会发现"什么都没发生"，还得自己再点一次下载，
        // 这正是这次要消灭的那种体验。
        if (pendingApk != null && canInstallPackages()) {
            File apk = pendingApk;
            pendingApk = null;
            launchInstaller(apk);
        }
        // 也可能是刚去开通知权限 / 准点提醒权限 / 自启动回来的：让设置页重画一遍状态。
        // （系统设置页是另一个 Activity，WebView 自己收不到任何事件。）
        refreshReminderUi();
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        // 先关掉可能还开着的对话框：Activity 都要销毁了，
        // 留着一个挂在已死窗口上的对话框只会造成 WindowLeaked。
        if (updateDialog != null) {
            if (updateDialog.isShowing()) {
                updateDialog.dismiss();
            }
            updateDialog = null;
        }
        if (dlDialog != null) {
            if (dlDialog.isShowing()) {
                dlDialog.dismiss();
            }
            dlDialog = null;
        }
        dlBar = null;
        dlMsg = null;
        // 摘掉 JS 桥再销毁 WebView：桥持有 Activity 的隐式引用，
        // 留着它会让已死的 Activity 被 JS 侧继续引用。
        web.removeJavascriptInterface("RadarNative");
        web.destroy();
        super.onDestroy();
    }

    private int dp(float v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ================================================================== 下拉刷新
    /** 下拉刷新回调。声明在外层类里 —— Java 8 **不允许在非静态内部类里声明接口**。 */
    interface OnRefresh {
        void onRefresh();
    }

    /**
     * 极简下拉刷新：只在"已经滚到顶部 + 向下拖够 90dp"时触发一次 reload()。
     *
     * 刻意**不消费**触摸事件（照旧交给 super），所以不会干扰正常滚动 ——
     * 引入 SwipeRefreshLayout 要拖进整个 AndroidX，对一个壳来说不值当。
     *
     * 必须是 static 嵌套类（同上：内部类里不能有静态声明），
     * 代价是不能直接用外层的 dp()，所以自带一个。
     */
    private static class PtrWebView extends WebView {

        private static final int THRESHOLD_DP = 90;

        private float startY;
        private boolean armed;
        private OnRefresh listener;

        PtrWebView(android.content.Context ctx) {
            super(ctx);
        }

        void setOnRefreshListener(OnRefresh l) {
            this.listener = l;
        }

        private int dp(float v) {
            return Math.round(v * getResources().getDisplayMetrics().density);
        }

        @SuppressLint("ClickableViewAccessibility")
        @Override
        public boolean onTouchEvent(MotionEvent e) {
            if (listener != null) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        startY = e.getY();
                        armed = false;
                        break;
                    case MotionEvent.ACTION_MOVE:
                        if (!armed && !canScrollVertically(-1)
                                && e.getY() - startY > dp(THRESHOLD_DP)) {
                            armed = true;
                        }
                        break;
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        if (armed) {
                            armed = false;
                            listener.onRefresh();
                        }
                        break;
                    default:
                        break;
                }
            }
            return super.onTouchEvent(e);
        }
    }
}
