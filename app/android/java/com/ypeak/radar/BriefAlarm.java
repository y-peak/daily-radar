package com.ypeak.radar;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;

/**
 * 每日大盘简报的三个提醒闹钟 —— 到点了去服务器取一段话，弹成系统通知。
 *
 * 和 {@link PlanReminder} 最关键的区别：**内容不在本地**。
 * 规划提醒只要读手机里那串 JSON 就能拼出通知（所以它不联网也能响）；
 * 简报的内容在服务器上（`/api/brief`），闹钟响的时候必须**去取一次**。
 * 这带来两个后果，都是本类的设计起点：
 *
 *   1. 网络会失败。所以这条链路有**两段**要分开兜：取不到内容 ≠ 不提醒。
 *      取不到时发一条**明说取不到**的通知，而不是安静什么都不发 ——
 *      用户设了"每天 8 点提醒我"，结果没动静，他只会以为功能坏了，
 *      然后把它关掉。宁可给一条"今天拿不到"，也不要给零条。
 *
 *   2. 广播接收器的 onReceive 跑在主线程、只有约 10 秒，**不能在那儿联网**。
 *      所以走 {@link BroadcastReceiver#goAsync()}：把这次广播的"生命"延长到
 *      后台线程里，做完再 finish()。见 {@link #onDueFired}。
 *
 * 时刻：三个时段各一个闹钟，**每天自己重排下一天**。
 *
 *   ⚠️ 为什么不用 `setRepeating`：API 19 起它一律是**不精确**的，而且
 *      在 Doze 下基本不准；我们要的是"早上 8 点"而不是"8 点前后一小时"。
 *      所以每次响完就把同一天的下一次排上 —— 顺便天然免疫了系统对
 *      "每个应用精确闹钟数量"的限制（永远只有 3 个）。
 *
 *   ⭐ 收盘那档默认排在 **16:40**，不是 15:00。理由：行情与资金流是在
 *      **16:30 那次采集**里才落库的，15:10 去拉只会拿到上一交易日的收盘，
 *      而通知上写着"收盘复盘"—— 那就等于每天都在骗自己。
 */
final class BriefAlarm {

    /** 闹钟送到接收器时的 action。 */
    static final String ACTION_DUE = "com.ypeak.radar.action.BRIEF_DUE";

    /** 闹钟里带着"这是哪个时段"，响的时候再核一次，防止排程错位。 */
    static final String EXTRA_SLOT = "slot";

    static final String SLOT_MORNING = "morning";
    static final String SLOT_INTRADAY = "intraday";
    static final String SLOT_CLOSE = "close";

    /** 三个时段，顺序就是设置页上的顺序。 */
    static final String[] SLOTS = {SLOT_MORNING, SLOT_INTRADAY, SLOT_CLOSE};

    /**
     * 每个时段一个固定的 requestCode。
     *
     * ⚠️ 必须**互不相同**：PendingIntent 的身份是 (requestCode, action, component…)
     *    一起算的。三个时段共用同一个 requestCode 的话，后设的那个会把前面
     *    那个替换掉 —— 表现是"只响一个时段"，而且看不出任何错。
     */
    private static final int[] REQ = {7401, 7402, 7403};

    /**
     * 默认时刻：早间 08:00 / 尾盘 14:30 / 收盘 16:40。
     *
     * 收盘那个是 16:40 而不是 15:05，见类注释 —— 数据 16:30 才落库。
     */
    private static final int[][] DEFAULT_HM = {{8, 0}, {14, 30}, {16, 40}};

    private static final String PREFS = "radar";
    private static final String KEY_CONF = "brief_json";
    /** 每个时段"下一次排在什么时候"（毫秒），设置页要显示它。 */
    private static final String KEY_NEXT = "brief_next";
    /** 每个时段"今天已经发过了"（日期串），用来防重和开机补发判断。 */
    private static final String KEY_SENT = "brief_sent";

    /** 开机补发的时间窗：错过不超过它才补。 */
    private static final long CATCHUP_WINDOW_MS = 90L * 60 * 1000;

    /** 取简报的网络预算。onReceive 给的生命只有约 10 秒，这里必须更短。 */
    private static final int CONNECT_TIMEOUT = 5000;
    private static final int READ_TIMEOUT = 7000;

    private BriefAlarm() {
    }

    // ================================================================ 配置

    /** 一个时刻的配置：开关 + 时 + 分。 */
    private static final class Slot {
        final String key;
        boolean on;
        int h;
        int m;

        Slot(String key, boolean on, int h, int m) {
            this.key = key;
            this.on = on;
            this.h = h;
            this.m = m;
        }
    }

    static String labelOf(String slot) {
        if (SLOT_MORNING.equals(slot)) {
            return "早间";
        }
        if (SLOT_INTRADAY.equals(slot)) {
            return "尾盘";
        }
        return "收盘";
    }

    private static int indexOf(String slot) {
        for (int i = 0; i < SLOTS.length; i++) {
            if (SLOTS[i].equals(slot)) {
                return i;
            }
        }
        return -1;
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /**
     * 读配置。**坏数据一律退回默认值，绝不抛异常、也绝不"猜一半"。**
     *
     * 为什么这么小心：这份配置是手机上唯一能自己长坏的输入（用户改过、老版本写过、
     * 写了一半被杀掉）。读的时候抛一次异常，表现就是"每次开机都少一个闹钟"，
     * 而且完全没有提示。所以逐字段校验：时刻只接受 0-23 / 0-59，
     * 别的值直接换成默认时刻。
     */
    private static Slot[] read(Context ctx) {
        Slot[] out = new Slot[SLOTS.length];
        for (int i = 0; i < SLOTS.length; i++) {
            out[i] = new Slot(SLOTS[i], true, DEFAULT_HM[i][0], DEFAULT_HM[i][1]);
        }
        String raw = null;
        try {
            raw = prefs(ctx).getString(KEY_CONF, "");
        } catch (Exception ignored) {
        }
        if (raw == null || raw.length() == 0) {
            return out;              // 没配过 = 默认全开
        }
        JSONObject root;
        try {
            root = new JSONObject(raw);
        } catch (Exception e) {
            return out;              // 解析不了就整体退回默认，不半信半疑地读
        }
        for (int i = 0; i < out.length; i++) {
            JSONObject o = root.optJSONObject(out[i].key);
            if (o == null) {
                continue;
            }
            out[i].on = o.optBoolean("on", true);
            int h = o.optInt("h", DEFAULT_HM[i][0]);
            int m = o.optInt("m", DEFAULT_HM[i][1]);
            if (h < 0 || h > 23 || m < 0 || m > 59) {
                h = DEFAULT_HM[i][0];
                m = DEFAULT_HM[i][1];
            }
            out[i].h = h;
            out[i].m = m;
        }
        return out;
    }

    private static String toJson(Slot[] slots) {
        try {
            JSONObject root = new JSONObject();
            for (int i = 0; i < slots.length; i++) {
                JSONObject o = new JSONObject();
                o.put("on", slots[i].on);
                o.put("h", slots[i].h);
                o.put("m", slots[i].m);
                root.put(slots[i].key, o);
            }
            return root.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * 网页写回配置。返回**到底存住了没有**。
     *
     * 写法照抄 setPlans：用 commit() 而不是 apply()，否则拿不到真实结果，
     * 只能永远返回成功 —— 那就等于骗网页"已保存"。
     * 存住了顺手重排：**写配置只有这一条路径**，挂在这里就不可能"忘了排"。
     */
    static boolean save(Context ctx, String json) {
        SharedPreferences p = prefs(ctx);
        if (p == null || json == null || json.length() == 0 || json.length() > 2000) {
            return false;
        }
        // 先解析一遍：不合法的输入不许落盘，免得把一份坏配置写进去之后
        // 每次读都退回默认 —— 用户会发现自己刚刚设置的时刻"莫名其妙变回去了"
        try {
            new JSONObject(json);
        } catch (Exception e) {
            return false;
        }
        try {
            boolean ok = p.edit().putString(KEY_CONF, json).commit();
            if (ok) {
                reschedule(ctx);
            }
            return ok;
        } catch (Exception e) {
            return false;
        }
    }

    // ================================================================ 排程

    /**
     * 把三个闹钟按配置重排一遍。没有开着的时段就全部撤掉。
     *
     * 这个方法可以在**任何线程**调用（只碰 SharedPreferences 和 AlarmManager），
     * 但要快 —— 广播接收器的 onReceive 就指着它。
     */
    static void reschedule(Context ctx) {
        try {
            Notifier.ensureBriefChannel(ctx);
        } catch (Exception ignored) {
        }
        Slot[] slots = read(ctx);
        StringBuilder next = new StringBuilder();
        for (int i = 0; i < slots.length; i++) {
            if (!slots[i].on) {
                cancelAt(ctx, i);
                continue;
            }
            long at = nextAt(slots[i]);
            if (at <= 0L) {
                continue;
            }
            setAlarm(ctx, i, at, slots[i].key);
            if (next.length() > 0) {
                next.append(',');
            }
            next.append(slots[i].key).append('=').append(at);
        }
        try {
            prefs(ctx).edit().putString(KEY_NEXT, next.toString()).apply();
        } catch (Exception ignored) {
        }
    }

    /** 这个时段的闹钟排到哪了（毫秒），没排就是 0。 */
    private static long nextOf(Context ctx, String slot) {
        try {
            String s = prefs(ctx).getString(KEY_NEXT, "");
            if (s == null || s.length() == 0) {
                return 0L;
            }
            String[] parts = s.split(",");
            for (int i = 0; i < parts.length; i++) {
                int eq = parts[i].indexOf('=');
                if (eq <= 0) {
                    continue;
                }
                if (parts[i].substring(0, eq).equals(slot)) {
                    return Long.parseLong(parts[i].substring(eq + 1));
                }
            }
        } catch (Exception ignored) {
        }
        return 0L;
    }

    /**
     * 这个时段的**下一次**触发时刻。
     *
     * 严格取未来：今天这个点还没到就是今天，已经过了就是明天 ——
     * 「过了点就今天补一条」在这里是**不做**的（简报的时段含义绑着时间：
     * 早上 8 点的"早间简报"在下午 3 点弹出来没有意义）。
     * 只有开机那一种情况会补，且只在 {@link #CATCHUP_WINDOW_MS} 之内。
     */
    private static long nextAt(Slot slot) {
        try {
            Calendar c = Calendar.getInstance();
            c.set(Calendar.HOUR_OF_DAY, slot.h);
            c.set(Calendar.MINUTE, slot.m);
            c.set(Calendar.SECOND, 0);
            c.set(Calendar.MILLISECOND, 0);
            if (c.getTimeInMillis() <= System.currentTimeMillis()) {
                c.add(Calendar.DAY_OF_MONTH, 1);
            }
            return c.getTimeInMillis();
        } catch (Exception e) {
            return 0L;
        }
    }

    private static AlarmManager alarm(Context ctx) {
        try {
            return (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        } catch (Exception e) {
            return null;
        }
    }

    private static Intent dueIntent(Context ctx, String slot) {
        Intent it = new Intent(ctx, BriefAlarmReceiver.class);
        it.setAction(ACTION_DUE);
        it.setPackage(ctx.getPackageName());
        if (slot != null && slot.length() > 0) {
            it.putExtra(EXTRA_SLOT, slot);
        }
        return it;
    }

    /**
     * 把第 i 个时段的闹钟排到 {@code at}。
     *
     * 降级口径与 {@link PlanReminder} 完全一致：没有「闹钟与提醒」特殊权限时
     * 退化成不精确闹钟（一定会响，但系统可能为了省电挪后），
     * 而不是干脆不排 —— 后者是静默失效。
     */
    private static void setAlarm(Context ctx, int i, long at, String slot) {
        AlarmManager am = alarm(ctx);
        if (am == null || i < 0 || i >= REQ.length) {
            return;
        }
        PendingIntent pi = PendingIntent.getBroadcast(ctx, REQ[i], dueIntent(ctx, slot),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        try {
            if (PlanReminder.canExact(ctx)) {
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            } else {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            }
        } catch (SecurityException e) {
            try {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            } catch (Exception ignored) {
            }
        } catch (Exception ignored) {
        }
    }

    /** 撤掉第 i 个时段的闹钟。 */
    private static void cancelAt(Context ctx, int i) {
        AlarmManager am = alarm(ctx);
        if (am == null || i < 0 || i >= REQ.length) {
            return;
        }
        try {
            // ⚠️ 查已存在的 PendingIntent 必须用 FLAG_NO_CREATE，否则会**顺手新建**
            //    一个 —— 那样 cancel 掉的就不是原来那个闹钟了。（同 PlanReminder.cancel）
            PendingIntent pi = PendingIntent.getBroadcast(ctx, REQ[i], dueIntent(ctx, ""),
                    PendingIntent.FLAG_NO_CREATE | PendingIntent.FLAG_IMMUTABLE);
            if (pi != null) {
                am.cancel(pi);
                pi.cancel();
            }
        } catch (Exception ignored) {
        }
    }

    // ================================================================ 响

    /**
     * 闹钟响了。由 {@link BriefAlarmReceiver} 调过来。
     *
     * ⚠️ 这里要联网，所以**不能**在 onReceive 里同步做完 —— 接收器给的时间
     *    只有约 10 秒，超时会被系统直接杀掉，表现是"偶尔不响"。
     *    所以接收器用 goAsync() 把生命交到 {@code pending} 上，我们在后台线程里
     *    做完再 finish()。`pending` 为 null 时（不该发生）也照常工作，只是
     *    没有延长生命 —— 能响一次是一次，不该因为兜底逻辑把功能弄没。
     */
    static void onDueFired(final Context ctx, final String slot,
                           final BroadcastReceiver.PendingResult pending) {
        final Context app = ctx.getApplicationContext() == null
                ? ctx : ctx.getApplicationContext();
        final String s = indexOf(slot) >= 0 ? slot : SLOT_CLOSE;   // 兜底：不认识就当收盘
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    fire(app, s, false);
                } finally {
                    // ⭐ 顺序：**先提醒、再重排**（和 PlanReminder 一致）。
                    //    反过来的话，重排一旦出岔子就再也回不到这一次了。
                    try {
                        reschedule(app);
                    } catch (Exception ignored) {
                    }
                    if (pending != null) {
                        try {
                            pending.finish();
                        } catch (Exception ignored) {
                        }
                    }
                }
            }
        }, "radar-brief").start();
    }

    /**
     * 取一次简报并弹通知。返回结果码（设置页的"发一条看看"要用它说人话）。
     *
     * @param test 是不是用户在设置页手动按的。手动按的时候**不记"今天发过"** ——
     *             否则用户上午试了一下，当天 16:40 那条真的收盘简报就被自己顶掉了。
     */
    static String fire(Context ctx, String slot, boolean test) {
        if (!test && alreadySent(ctx, slot)) {
            return "dup";            // 今天这个时段已经发过：不重复打扰
        }
        String body = fetch(slot);
        String title;
        String text;
        String big;
        if (body == null) {
            // ⭐ 取不到也要发一条，而且**明说是取不到**。
            //    安静什么都不发的后果是用户以为功能坏了，然后把提醒关掉 ——
            //    那比一条"今天拿不到"的通知糟得多。
            title = labelOf(slot) + "简报暂时拿不到";
            text = "服务器或网络没应答。过一会儿打开 App 能看到。";
            big = text;
        } else {
            JSONObject o;
            try {
                o = new JSONObject(body);
            } catch (Exception e) {
                return "nofetch";
            }
            title = o.optString("title", labelOf(slot) + "简报");
            text = o.optString("line", "");
            big = o.optString("big", "");
            if (title.length() == 0 || text.length() == 0) {
                return "nofetch";
            }
        }
        int id = test ? Notifier.ID_BRIEF_TEST
                      : Notifier.ID_BRIEF_BASE + Math.max(0, indexOf(slot));
        boolean ok = Notifier.notifyBrief(ctx, id, title, text, big);
        if (!ok) {
            return "nonotify";        // 没权限。**不记"已发"**，等权限开回来还能补
        }
        if (!test) {
            markSent(ctx, slot);
        }
        return "ok";
    }

    /**
     * 开机时补一次当天错过的。
     *
     * 判据很窄：这个时段开着、今天的那个点已经过了、错过不超过
     * {@link #CATCHUP_WINDOW_MS}、而且今天这个时段还没发过 —— 四个都成立才补。
     *
     * 为什么不补更久的：简报的时段含义绑着时间。早上 8 点的"早间简报"
     * 晚上 8 点才弹出来，读的人会以为那是当下的行情 —— 比不弹更糟。
     *
     * ⚠️⚠️ 这个方法是**异步**的，而且必须异步：补发要联网（{@link #fire} →
     *    {@link #fetch}），而它的调用点是 BOOT_COMPLETED 的 onReceive ——
     *    那是**主线程**。在主线程上联网会抛 NetworkOnMainThreadException，
     *    被 fetch 的兜底 catch 吞掉，于是每一条补发都变成"服务器或网络没应答"。
     *    **不崩、不报错、通知照弹，只是内容是错的** —— 这正是本项目最难查的一类。
     *    （规划提醒那边没这个问题：它读本地 JSON，压根不联网，所以那边是同步的。）
     *
     * 所以这里和 {@link #onDueFired} 一个口径：接一个 goAsync() 给的 pending，
     * 在后台线程里做完再 finish()。pending 为 null（不该发生）也照常做，
     * 只是不延长生命 —— 能补一条是一条。
     */
    static void catchUp(final Context ctx, final BroadcastReceiver.PendingResult pending) {
        final Context app = ctx.getApplicationContext() == null
                ? ctx : ctx.getApplicationContext();
        // 先排下一天：这一步是纯本地的（SharedPreferences + AlarmManager），
        // 而且"闹钟还在不在"比"今天补不补一条"重要得多，不该被网络拖住。
        try {
            reschedule(app);
        } catch (Exception ignored) {
        }
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    catchUpNow(app);
                } catch (Exception ignored) {
                } finally {
                    if (pending != null) {
                        try {
                            pending.finish();
                        } catch (Exception ignored) {
                        }
                    }
                }
            }
        }, "radar-brief-boot").start();
    }

    /** 补发的实际动作。**只许在后台线程调用**（要联网，见 catchUp 的说明）。 */
    private static void catchUpNow(Context ctx) {
        Slot[] slots = read(ctx);
        long now = System.currentTimeMillis();
        for (int i = 0; i < slots.length; i++) {
            if (!slots[i].on) {
                continue;
            }
            long at = todayAt(slots[i]);
            if (at <= 0L || at > now || now - at > CATCHUP_WINDOW_MS) {
                continue;
            }
            try {
                fire(ctx, slots[i].key, false);
            } catch (Exception ignored) {
            }
        }
    }

    /** 今天这个时刻（毫秒）。已经过了、或者时刻不合法都照原样返回，由调用方判断。 */
    private static long todayAt(Slot slot) {
        try {
            Calendar c = Calendar.getInstance();
            c.set(Calendar.HOUR_OF_DAY, slot.h);
            c.set(Calendar.MINUTE, slot.m);
            c.set(Calendar.SECOND, 0);
            c.set(Calendar.MILLISECOND, 0);
            return c.getTimeInMillis();
        } catch (Exception e) {
            return 0L;
        }
    }

    /** 手动"发一条看看"。网页会拿返回的码去说一句人话。 */
    static String testNow(Context ctx, String slot) {
        String s = indexOf(slot) >= 0 ? slot : SLOT_CLOSE;
        try {
            return fire(ctx, s, true);
        } catch (Exception e) {
            return "nofetch";
        }
    }

    // ================================================================ 取内容

    /**
     * 拉一次 `/api/brief`。拿不到就返回 null（**不抛**）。
     *
     * ⚠️ 口令必须放在 URL 上：`HttpURLConnection` 有自己的 Cookie 存储，
     *    **不共享** WebView 的 CookieJar，不带口令就是 401。
     *    （和 AppConfig.API_APP 注释里说的是同一件事。）
     */
    private static String fetch(String slot) {
        HttpURLConnection conn = null;
        try {
            String url = AppConfig.API_BRIEF + "?slot=" + slot + AppConfig.AUTH_QUERY;
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestProperty("Accept", "application/json");
            if (conn.getResponseCode() != 200) {
                return null;
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
            return sb.toString();
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) {
                try {
                    conn.disconnect();
                } catch (Exception ignored) {
                }
            }
        }
    }

    // ================================================================ 给网页看的

    /**
     * 设置页要的事实，一次性给全（JSON）。
     *
     * 和 PlanReminder.nextInfo 同一个口径：**返回事实，不返回拼好的句子** ——
     * 文案归展示层，改措辞不用重新发版装 APK。
     *
     * 字段：
     *   notify      现在能不能发通知
     *   exact       能不能准点（false = 会响但可能晚一会儿）
     *   slots[]     key/label/on/h/m/when（when 是"下一次 M月d日 HH:mm"）
     *   anyOn       有没有开着的时段
     */
    static String nextInfo(Context ctx) {
        JSONObject o = new JSONObject();
        try {
            o.put("notify", Notifier.canNotify(ctx));
            o.put("exact", PlanReminder.canExact(ctx));
            Slot[] slots = read(ctx);
            org.json.JSONArray arr = new org.json.JSONArray();
            boolean anyOn = false;
            for (int i = 0; i < slots.length; i++) {
                JSONObject s = new JSONObject();
                s.put("key", slots[i].key);
                s.put("label", labelOf(slots[i].key));
                s.put("on", slots[i].on);
                s.put("h", slots[i].h);
                s.put("m", slots[i].m);
                if (slots[i].on) {
                    anyOn = true;
                }
                long at = nextOf(ctx, slots[i].key);
                s.put("when", at > 0L ? fmtAt(at) : "");
                arr.put(s);
            }
            o.put("slots", arr);
            o.put("anyOn", anyOn);
        } catch (Exception e) {
            // 兜底：无论如何都要还一个能解析的 JSON，绝不把异常抛给网页
            return "{\"notify\":false,\"exact\":false,\"anyOn\":false,\"slots\":[]}";
        }
        return o.toString();
    }

    private static String fmtAt(long at) {
        try {
            return new SimpleDateFormat("M月d日 HH:mm", Locale.US).format(new Date(at));
        } catch (Exception e) {
            return "";
        }
    }

    // ================================================================ 已发记录

    /**
     * "今天这个时段发过了吗"。
     *
     * 记在 SharedPreferences 里而不是内存：进程随时可能被杀，
     * 内存标记一丢，同一天就会重复弹。
     */
    private static boolean alreadySent(Context ctx, String slot) {
        return daySent(ctx).indexOf("|" + slot + "=" + day() + "|") >= 0;
    }

    private static void markSent(Context ctx, String slot) {
        try {
            String s = daySent(ctx);
            String token = "|" + slot + "=" + day() + "|";
            if (s.indexOf(token) >= 0) {
                return;
            }
            if (s.length() > 600) {
                s = "|";             // 太长了就整个丢掉重建：这里只关心今天
            }
            prefs(ctx).edit().putString(KEY_SENT, s + slot + "=" + day() + "|").apply();
        } catch (Exception ignored) {
        }
    }

    private static String daySent(Context ctx) {
        try {
            String s = prefs(ctx).getString(KEY_SENT, "");
            if (s == null || s.length() == 0) {
                return "|";
            }
            return s.startsWith("|") ? s : ("|" + s);
        } catch (Exception e) {
            return "|";
        }
    }

    /**
     * 今天的日期串（**本地时间**）。
     *
     * ⚠️ 必须走本地时区：用 UTC 的话，东八区凌晨 0-8 点算出来的"今天"是昨天 ——
     *    而早间简报正好排在这段时间里，会把凌晨那次的"已发"记到昨天，
     *    于是同一天弹两条。Locale.US 是为了拿到 ASCII 数字（某些区域设置下
     *    %d 会输出本地数字，拼出来就不再是 YYYY-MM-DD 了）。
     */
    private static String day() {
        Calendar c = Calendar.getInstance();
        return String.format(Locale.US, "%04d-%02d-%02d",
                c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }
}
