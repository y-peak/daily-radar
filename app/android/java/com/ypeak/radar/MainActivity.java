package com.ypeak.radar;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
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
                return p.edit().putString(PREF_PLANS, json).commit();
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
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            authRetried = false;
            if (bar.getProgress() >= 100) {
                bar.setVisibility(View.GONE);
            }
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
