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
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
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
 *   · **壳自身更新** —— 就是下面 `checkUpdate()` 干的事。壳很少变，
 *     所以要"偶尔查一次 + 别唠叨"，而不是每次启动都弹。
 *
 * 五个必须自己处理的地方（不做就会被用户感知为"这破 App 有问题"）：
 *   1. 站外链接（GitHub 仓库、许可证页…）要用系统浏览器打开，不能困在 WebView 里
 *   2. 断网要有可重试的提示页，不能只留一片白屏
 *   3. 返回键要能网页后退，而不是直接退出
 *   4. 顶端下拉刷新 —— 用户对 WebView 应用的默认预期
 *   5. 有新版本要能自己发现 —— 否则用户永远停在装的那一版
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

    private SharedPreferences prefs;

    /** 正在显示的更新对话框。Activity 销毁时要主动关掉，否则会带着已死的窗口泄漏。 */
    private AlertDialog updateDialog;

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

        // ⭐ 把"版本 + 检查更新"暴露给网页。
        //    注册必须在 loadUrl **之前** —— 否则首次加载的页面里
        //    window.RadarNative 是 undefined，那条入口要等下次加载才出现。
        //    只暴露"读版本号"和"触发检查"两个只读动作，不暴露任何能改文件/网络的方法。
        web.addJavascriptInterface(new NativeBridge(), "RadarNative");

        if (state == null || web.restoreState(state) == null) {
            web.loadUrl(AppConfig.START_URL);
        }

        prefs = getSharedPreferences("radar", Context.MODE_PRIVATE);
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
    }

    // ================================================================== 检测新版本
    /**
     * 问服务器"现在最新是哪个版本"，比自己新就提示。
     *
     * 几条刻意的选择：
     *   · **放后台线程** —— 网络请求绝不能上主线程（会 ANR）。
     *   · **失败就静默** —— 查更新失败绝不能打扰用户，它只是个附赠能力。
     *   · **6 小时最多查一次**，且**跳过过的版本不再弹**
     *     —— 否则每次冷启动都弹，用户会直接卸载。
     *   · **下载交给系统浏览器**，不在 App 内装。小米/华为这类 ROM 对
     *     "应用内拉起安装"限制很多，交给浏览器反而最稳。
     *   · 不申请 `REQUEST_INSTALL_PACKAGES` —— 那是应用内安装才需要的敏感权限。
     */
    private void checkUpdate(final boolean manual) {
        if (!manual) {
            // 自动模式：6 小时最多查一次。**主动模式跳过这个闸** ——
            // 用户点了"检查更新"却什么也不发生，是最糟的体验。
            final long last = prefs.getLong("last_check", 0L);
            if (System.currentTimeMillis() - last < UPDATE_CHECK_INTERVAL_MS) {
                return;
            }
        }
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
                    if (!manual && prefs.getInt("skipped", 0) >= code) {
                        return;
                    }
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            promptUpdate(code, name);
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

    /** 从后台线程发 Toast —— Toast 必须回到主线程。 */
    private void toastLater(final String msg) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (!isFinishing() && !isDestroyed()) {
                    Toast.makeText(MainActivity.this, msg, Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    private void promptUpdate(final int code, String name) {
        // ⚠️ 必须同时查 isFinishing() 和 **isDestroyed()**：
        //    查更新的线程可能在 Activity 已经销毁之后才回到主线程，
        //    这时 show() 会抛 BadTokenException 直接崩掉。
        //    （本 Activity 锁了竖屏且吃掉了 configChanges，所以旋转不会重建；
        //      但"不保留活动"/内存压力仍可能销毁它 —— 这类崩溃低频但很难查。）
        if (isFinishing() || isDestroyed()) {
            return;
        }
        String shown = (name == null || name.length() == 0) ? String.valueOf(code) : name;
        AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle(R.string.update_title)
                .setMessage(getString(R.string.update_msg, shown, AppConfig.VERSION_NAME))
                .setPositiveButton(R.string.update_now, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        openDownload();
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

    /** 交给系统浏览器：下载 + 安装都由它负责，比 App 内自己装可靠得多。 */
    private void openDownload() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(AppConfig.APK_URL)));
        } catch (ActivityNotFoundException ignored) {
            // 没有浏览器（极罕见）就算了，别崩
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
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        // 先关掉可能还开着的更新对话框：Activity 都要销毁了，
        // 留着一个挂在已死窗口上的对话框只会造成 WindowLeaked。
        if (updateDialog != null) {
            if (updateDialog.isShowing()) {
                updateDialog.dismiss();
            }
            updateDialog = null;
        }
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
