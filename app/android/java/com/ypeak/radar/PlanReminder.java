package com.ypeak.radar;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 规划到期提醒 —— 从"手机里的规划"推导出"什么时候响"，并把它交给系统的闹钟。
 *
 * 为什么排程放在原生侧、而不是网页算好了告诉原生：
 *   ⭐ 因为**开机之后必须能重排**。开机时 WebView 根本没加载，网页那边
 *      一行 JS 都没跑过；如果排程信息只活在网页里，重启一次提醒就永久没了。
 *      原生侧能自己读 plans_json（就是 getPlans/setPlans 存的那串），
 *      所以把它放在这里是唯一能自洽的选择。
 *
 * 时刻约定：**到期当天早上 09:00**（本地时间）。用户选的。
 *
 * 几条刻意的取舍：
 *
 *   · **同一时刻只存在一个闹钟**（{@link #REQ_ALARM} 固定 requestCode）。
 *     要提醒的是"某一天"而不是"某一条规划"，所以每次只排最近的那一天；
 *     响过之后再排下一天。好处是天然免疫系统的"每个应用精确闹钟数量上限"，
 *     也不会因为清了规划而留下一堆孤儿闹钟。
 *
 *   · **过了点就不补**（除了开机那一种情况）。到期当天已经过了 9:00 才排到，
 *     不再补一条 —— 否则用户每次打开 App、或者随手改一条规划，
 *     都可能被一条"迟到的提醒"打脸。唯一的例外是**关机期间错过的**：
 *     那种情况开机后补一条（仅限当天），见 {@link #reschedule(Context, boolean)}。
 *
 *   · **没有精确闹钟权限就降级，但要看得见**。Android 14 起新装应用默认
 *     拿不到「闹钟与提醒」特殊权限，setExact* 会直接抛 SecurityException。
 *     这里的处理是退化成不精确闹钟（一定会响，但可能晚一些），
 *     并把这件事**如实显示在设置页上**，同时给一个"去开启"的入口。
 *     绝不假装自己是准点的。
 *
 *   · **异常一律吞掉**。排程失败最坏的结果是"这次不提醒"，而抛出去的结果是
 *     开 App 就崩 —— 两害相权，选前者。但设置页会显示"下一次提醒"，
 *     真出问题能看出来（这条链路必须有外部参照物，不能只靠"没报错"）。
 */
final class PlanReminder {

    /** 提醒钟点：到期当天 09:00（本地时间）。 */
    private static final int HOUR = 9;
    private static final int MINUTE = 0;

    /** 唯一的那个闹钟。固定 requestCode = 设置新闹钟时自动替换旧的。 */
    private static final int REQ_ALARM = 7301;

    /** "已提醒过的日子"最多记这么多天，防止这个字符串无限长下去。 */
    private static final int MAX_NOTIFIED_DAYS = 40;

    /** 到期闹钟送到广播接收器时用的 action。 */
    static final String ACTION_DUE = "com.ypeak.radar.action.PLAN_DUE";

    /** 闹钟里带着"这是哪一天" —— 响的时候再核一次，防止排程错位。 */
    static final String EXTRA_DAY = "day";

    /** 点通知进 App 时带上它，让 App 直接掀开规划面板。 */
    static final String EXTRA_OPEN_PLANS = "open_plans";

    private static final String PREFS = "radar";
    private static final String KEY_PLANS = "plans_json";
    private static final String KEY_NEXT_AT = "plan_next_at";
    private static final String KEY_NEXT_DAY = "plan_next_day";
    private static final String KEY_NOTIFIED = "plan_notified";

    private PlanReminder() {
    }

    // ================================================================ 数据

    /** 从 plans_json 里挑出来的一条。"净化过的"版本 —— 字段都在、日期一定是合法日期。 */
    private static final class Item {
        final String id;
        final String title;
        final String due;
        final String memo;

        Item(String id, String title, String due, String memo) {
            this.id = id;
            this.title = title;
            this.due = due;
            this.memo = memo;
        }
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /**
     * 读规划，只留下**结构正确且日期合法**的条目。
     *
     * 和网页侧的 sanitize() 是同一个判断：历史数据里什么都有可能
     * （字段缺失、类型不对、被截断的一半）。一条脏数据不该让整条提醒链路停摆，
     * 更不该让"日期"被当成别的东西解释 —— 所以 due 走的是严格格式校验
     * （{@link #isDay}），不合法就直接不算数，绝不猜。
     */
    private static List<Item> readPlans(Context ctx) {
        List<Item> out = new ArrayList<Item>();
        String raw;
        try {
            raw = prefs(ctx).getString(KEY_PLANS, "");
        } catch (Exception e) {
            return out;
        }
        if (raw == null || raw.length() == 0) {
            return out;
        }
        JSONObject root;
        try {
            root = new JSONObject(raw);
        } catch (Exception e) {
            return out;              // 解析不了 = 当作没有规划，不能让排程崩
        }
        JSONArray arr = root.optJSONArray("items");
        if (arr == null) {
            return out;
        }
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o == null) {
                continue;
            }
            String t = o.optString("t", "");
            String due = o.optString("due", "");
            if (t.length() == 0 || !isDay(due)) {
                continue;            // 没有标题、或者没定日期的：不参与提醒
            }
            if (o.optBoolean("done", false)) {
                continue;            // 做完的不用提醒
            }
            out.add(new Item(o.optString("id", ""), t, due, o.optString("memo", "")));
        }
        return out;
    }

    /** 手机上有没有"定了日期、还没做完"的规划。用来决定要不要跟用户要通知权限。 */
    static boolean hasDatedPlan(Context ctx) {
        return !readPlans(ctx).isEmpty();
    }

    // ================================================================ 排程

    /** 常规重排。开机那种"要补一条"的场景走 {@link #reschedule(Context, boolean)}。 */
    static void reschedule(Context ctx) {
        reschedule(ctx, false);
    }

    /**
     * 重排下一次提醒。这个方法可以在**任何线程**调用（只碰 SharedPreferences
     * 和 AlarmManager），但要快 —— 广播接收器的 onReceive 就指着它。
     *
     * @param catchUpMissed 要不要补一条"当天已经错过、但本来该提醒"的。
     *                      只在**开机**时为 true：闹钟不跨重启存活，重启会把
     *                      当天 9:00 那次直接吃掉，不补的话那天就真的没提醒了。
     *                      其它路径（开 App、改时间）一律 false ——
     *                      用户正看着屏幕，这时候糊一条通知纯属打扰。
     */
    static void reschedule(Context ctx, boolean catchUpMissed) {
        try {
            Notifier.ensureChannel(ctx);
        } catch (Exception ignored) {
        }
        if (catchUpMissed) {
            catchUp(ctx);
        }

        List<Item> items = readPlans(ctx);
        long now = System.currentTimeMillis();
        String day = null;
        long at = 0L;
        for (int i = 0; i < items.size(); i++) {
            Item it = items.get(i);
            long t = dueAt(it.due);
            if (t <= now) {
                continue;            // 过了点的不补（见类注释）
            }
            if (day == null || t < at) {
                day = it.due;
                at = t;
            }
        }

        if (day == null) {
            cancel(ctx);
            writeNext(ctx, null, 0L);
            return;
        }
        writeNext(ctx, day, at);
        setAlarm(ctx, at, day);
    }

    /**
     * 撤掉闹钟（没有该提醒的东西了）。
     *
     * ⚠️ 这里**刻意不动已经弹出的那条通知**。曾经在这里顺手 cancel 过，
     *    后来发现那会造出一个很讨厌的行为：用户 9:00 收到提醒、9:30 打开
     *    App（哪怕只是为了读一章书），这一趟重排就走进了"没有待提醒的"分支，
     *    于是那条**还没被看过**的通知被静默收走 —— 用户永远不知道自己被提醒过。
     *    "打开 App"不等于"看见了提醒"，这条不能替用户决定。
     *    该收的通知走 {@link #dropStaleNotification}：只在规划真的改完之后收。
     */
    static void cancel(Context ctx) {
        AlarmManager am = alarm(ctx);
        if (am == null) {
            return;
        }
        try {
            // ⚠️ 查一个已存在的 PendingIntent 必须用 FLAG_NO_CREATE，
            //    否则会**顺手新建**一个——那样 cancel 的就不是原来那个闹钟了。
            //    FLAG_IMMUTABLE 也要和创建时一致，否则算"不同的 PendingIntent"。
            PendingIntent pi = PendingIntent.getBroadcast(ctx, REQ_ALARM, dueIntent(ctx, ""),
                    PendingIntent.FLAG_NO_CREATE | PendingIntent.FLAG_IMMUTABLE);
            if (pi != null) {
                am.cancel(pi);
                pi.cancel();
            }
        } catch (Exception ignored) {
        }
    }

    /**
     * 规划改动之后，把"已经不该挂着"的那条通知收掉。
     *
     * 判据很窄：**今天提醒过**、而且今天已经**没有**到期未完成的规划了
     * （做完了 / 删了）→ 收掉。其它情况一律不动。
     *
     * 为什么不并进 {@link #reschedule}：reschedule 每次开 App 都会跑，
     * 而"开 App"和"看过了"是两件事，见 cancel() 的注释。
     */
    static void dropStaleNotification(Context ctx) {
        try {
            String today = todayStr();
            if (!isNotified(ctx, today)) {
                return;                       // 今天压根没提醒过，没什么可收
            }
            if (!dueOn(ctx, today).isEmpty()) {
                return;                       // 还有没做完的，留着
            }
            Notifier.cancelDue(ctx);
        } catch (Exception ignored) {
        }
    }

    private static AlarmManager alarm(Context ctx) {
        try {
            return (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * 把闹钟排到 {@code at}。
     *
     * 用 RTC_WAKEUP：到点要把睡着的机器叫起来，不然提醒就变成"下次解锁时才看到"，
     * 那对"早上 9 点提醒我"来说等于没做。
     */
    private static void setAlarm(Context ctx, long at, String day) {
        AlarmManager am = alarm(ctx);
        if (am == null) {
            return;
        }
        PendingIntent pi = PendingIntent.getBroadcast(ctx, REQ_ALARM, dueIntent(ctx, day),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        try {
            if (canExact(ctx)) {
                // 会不会被 Doze 拖后：allowWhileIdle 允许在打盹时也准点响一次
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            } else {
                // 没有「闹钟与提醒」权限：只能退化。它**一定**会响，但系统可以
                // 为了省电把它挪后（深睡时通常在一小时内）。设置页会把这件事写出来。
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            }
        } catch (SecurityException e) {
            // canScheduleExactAlarms() 说是行、真排的时候被拒 —— 只在权限刚被
            // 撤掉的一瞬间才可能发生。再退一级，绝不把异常抛出去。
            try {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
            } catch (Exception ignored) {
            }
        } catch (Exception ignored) {
        }
    }

    /** 有没有「闹钟与提醒」这个特殊权限。API 31 之前不需要，一律算有。 */
    static boolean canExact(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return true;
        }
        AlarmManager am = alarm(ctx);
        if (am == null) {
            return false;
        }
        try {
            return am.canScheduleExactAlarms();
        } catch (Exception e) {
            return false;
        }
    }

    private static Intent dueIntent(Context ctx, String day) {
        Intent it = new Intent(ctx, PlanAlarmReceiver.class);
        it.setAction(ACTION_DUE);
        it.setPackage(ctx.getPackageName());
        if (day != null && day.length() > 0) {
            it.putExtra(EXTRA_DAY, day);
        }
        return it;
    }

    /**
     * 排到哪一天了，记在 SharedPreferences 里。
     *
     * ⭐ 这不只是"设置页要显示"——它是**整条链路的参照物**。
     *    排程是否真的成功，光看"没报错"是看不出来的（AlarmManager 失败是静默的），
     *    所以把结果落成一个能被人读到的时间，用户一眼就能对上：
     *    "下一次提醒 9月20日 09:00"，到那天真的响了，才说明链路是通的。
     */
    private static void writeNext(Context ctx, String day, long at) {
        try {
            prefs(ctx).edit()
                    .putString(KEY_NEXT_DAY, day == null ? "" : day)
                    .putLong(KEY_NEXT_AT, at)
                    .apply();
        } catch (Exception ignored) {
        }
    }

    // ================================================================ 响

    /**
     * 闹钟响了。由 {@link PlanAlarmReceiver} 调过来。
     *
     * 顺序很重要：**先提醒、再重排**。反过来的话，重排把 KEY_NEXT_* 更新成
     * 下一天之后，一旦提醒这一步出岔子，就再也回不到这一天了。
     */
    static void onDueFired(Context ctx, String day) {
        if (!isDay(day)) {
            day = todayStr();        // 兜底：没带日期就当今天（老闹钟/意外情况）
        }
        notifyFor(ctx, day);
        reschedule(ctx);
    }

    /**
     * 开机时补一次当天错过的提醒。
     *
     * 判据很简单：今天已经过了 9:00、今天有没做完的规划、今天还没提醒过。
     * 三个都成立才补 —— 这样"早上 9 点开机之前"、"昨天开机的"都不会误触发。
     */
    private static void catchUp(Context ctx) {
        String today = todayStr();
        long at = dueAt(today);
        long now = System.currentTimeMillis();
        if (at <= 0L || at > now) {
            return;                  // 还没到点（今天 9:00 还没到），用不着补
        }
        if (now - at > 24L * 60 * 60 * 1000) {
            return;                  // 早过头的日子就别翻了（时区乱跳等极端情况）
        }
        notifyFor(ctx, today);
    }

    /**
     * 提醒"这一天"的规划。返回有没有真的发出去。
     *
     * 一天只提醒一次（{@link #isNotified} 挡着），所以重复的闹钟、
     * 开机补发与当天的正常触发不会叠成两条。
     */
    private static boolean notifyFor(Context ctx, String day) {
        List<Item> due = dueOn(ctx, day);
        if (due.isEmpty()) {
            return false;            // 这一天的都做完了/被删了：安静退场
        }
        if (isNotified(ctx, day)) {
            return false;
        }

        String title;
        String text;
        StringBuilder big = new StringBuilder();
        for (int i = 0; i < due.size(); i++) {
            Item it = due.get(i);
            if (i > 0) {
                big.append('\n');
            }
            big.append("· ").append(it.title);
            if (it.memo.length() > 0) {
                big.append("　").append(it.memo);
            }
        }
        if (due.size() == 1) {
            title = ctx.getString(R.string.notify_due_one, due.get(0).title);
            text = due.get(0).memo.length() > 0
                    ? due.get(0).memo
                    : ctx.getString(R.string.notify_due_text_one);
        } else {
            title = ctx.getString(R.string.notify_due_many, due.size());
            text = titlesOf(due);
        }

        boolean ok = Notifier.notifyDue(ctx, title, text, big.toString());
        if (ok) {
            // ⭐ 只有真发出去了才记"今天提醒过"。发失败（没权限）就留着，
            //    等用户把权限开回来，至少当天开机时还能补上。
            markNotified(ctx, day);
        }
        return ok;
    }

    // ================================================================ 给网页看的

    /**
     * 设置页要的那点信息，一次性给全（JSON 字符串）。
     *
     * 为什么返回 JSON 而不是拼好的句子：文案属于展示层，归网页管；
     * 原生这边只负责回答"事实是什么"。这样改文案不用重新发版装 APK。
     *
     * 字段：
     *   notify  现在能不能发通知
     *   exact   能不能准点（没有就是"可能晚一会儿"）
     *   pending 手机里有没有"定了日期还没做完"的规划
     *   when    下一次提醒的时刻，如"9月20日 09:00"；没有安排时是空串
     *   count   / titles  这一天有哪几条
     */
    static String nextInfo(Context ctx) {
        JSONObject o = new JSONObject();
        try {
            o.put("notify", Notifier.canNotify(ctx));
            o.put("exact", canExact(ctx));
            o.put("pending", hasDatedPlan(ctx));

            String day = "";
            long at = 0L;
            try {
                SharedPreferences p = prefs(ctx);
                day = p.getString(KEY_NEXT_DAY, "");
                at = p.getLong(KEY_NEXT_AT, 0L);
            } catch (Exception ignored) {
            }
            if (isDay(day) && at > System.currentTimeMillis()) {
                List<Item> due = dueOn(ctx, day);
                o.put("when", fmtAt(at));
                o.put("count", due.size());
                o.put("titles", titlesOf(due));
            } else {
                o.put("when", "");
                o.put("count", 0);
                o.put("titles", "");
            }
        } catch (Exception e) {
            // 兜底：无论如何都要还一个能解析的 JSON，绝不把异常抛给网页
            return "{\"notify\":false,\"exact\":false,\"pending\":false,"
                 + "\"when\":\"\",\"count\":0,\"titles\":\"\"}";
        }
        return o.toString();
    }

    /** 设置页的"测试提醒"：立刻发一条，用来验证整条链路。 */
    static boolean testNow(Context ctx) {
        return Notifier.notifyTest(ctx);
    }

    // ================================================================ 日期

    /**
     * 严格校验 `YYYY-MM-DD`。
     *
     * ⚠️ 刻意不用 SimpleDateFormat 去"宽容地解析"：它会把 "2026-9-2"、甚至
     *    带时间的一串都吃下去，解析出别的日期来。提醒这种东西一旦把日期理解错，
     *    表现是"某天莫名其妙响了"或者"该响的时候没响"，事后极难查。
     *    所以格式不对就直接不算数 —— 宁可漏一条，不可错一条。
     */
    private static boolean isDay(String s) {
        if (s == null || s.length() != 10) {
            return false;
        }
        if (s.charAt(4) != '-' || s.charAt(7) != '-') {
            return false;
        }
        for (int i = 0; i < 10; i++) {
            if (i == 4 || i == 7) {
                continue;
            }
            char c = s.charAt(i);
            if (c < '0' || c > '9') {
                return false;
            }
        }
        int m = (s.charAt(5) - '0') * 10 + (s.charAt(6) - '0');
        int d = (s.charAt(8) - '0') * 10 + (s.charAt(9) - '0');
        return m >= 1 && m <= 12 && d >= 1 && d <= 31;
    }

    /** 那一天的 09:00 本地时刻（毫秒）。格式不合法返回 -1。 */
    private static long dueAt(String day) {
        if (!isDay(day)) {
            return -1L;
        }
        try {
            int y = Integer.parseInt(day.substring(0, 4));
            int m = Integer.parseInt(day.substring(5, 7));
            int d = Integer.parseInt(day.substring(8, 10));
            // clear() 之后 set：分秒毫秒都归零，不受"当前时刻"影响
            Calendar c = Calendar.getInstance();
            c.clear();
            c.set(y, m - 1, d, HOUR, MINUTE, 0);
            return c.getTimeInMillis();
        } catch (Exception e) {
            return -1L;
        }
    }

    /**
     * 今天的日期串（**本地时间**）。
     *
     * ⚠️ 必须走本地时区：用 UTC 的话，东八区凌晨 0-8 点算出来的"今天"是昨天，
     *    开机补提醒会在那段时间判断反。Locale.US 是为了拿到 ASCII 数字 ——
     *    某些区域设置下 %d 会输出本地数字，拼出来的串就不再是 YYYY-MM-DD 了。
     */
    static String todayStr() {
        Calendar c = Calendar.getInstance();
        return String.format(Locale.US, "%04d-%02d-%02d",
                c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }

    private static String fmtAt(long at) {
        try {
            return new SimpleDateFormat("M月d日 HH:mm", Locale.US).format(new Date(at));
        } catch (Exception e) {
            return "";
        }
    }

    /** 这一天还没做完的规划。 */
    private static List<Item> dueOn(Context ctx, String day) {
        List<Item> out = new ArrayList<Item>();
        List<Item> all = readPlans(ctx);
        for (int i = 0; i < all.size(); i++) {
            if (day.equals(all.get(i).due)) {
                out.add(all.get(i));
            }
        }
        return out;
    }

    /** "A、B、C"（超过 3 条就尾巴上写"等 N 条"），给通知正文和设置页共用。 */
    private static String titlesOf(List<Item> items) {
        StringBuilder sb = new StringBuilder();
        int show = items.size() > 3 ? 3 : items.size();
        for (int i = 0; i < show; i++) {
            if (i > 0) {
                sb.append('、');
            }
            sb.append(items.get(i).title);
        }
        if (items.size() > show) {
            sb.append(" 等 ").append(items.size()).append(" 条");
        }
        return sb.toString();
    }

    // ================================================================ 已提醒记录

    private static boolean isNotified(Context ctx, String day) {
        try {
            String s = prefs(ctx).getString(KEY_NOTIFIED, "");
            if (s == null || s.length() == 0) {
                return false;
            }
            // 两头补逗号，保证"整段匹配"而不是子串匹配
            return ("," + s + ",").indexOf("," + day + ",") >= 0;
        } catch (Exception e) {
            return false;
        }
    }

    private static void markNotified(Context ctx, String day) {
        try {
            String s = prefs(ctx).getString(KEY_NOTIFIED, "");
            String[] parts = (s == null || s.length() == 0) ? new String[0] : s.split(",");
            StringBuilder sb = new StringBuilder();
            int keep = parts.length > MAX_NOTIFIED_DAYS ? parts.length - MAX_NOTIFIED_DAYS : 0;
            for (int i = keep; i < parts.length; i++) {
                if (parts[i].length() == 0 || parts[i].equals(day)) {
                    continue;        // 空的、以及今天（重记一次挪到末尾）
                }
                if (sb.length() > 0) {
                    sb.append(',');
                }
                sb.append(parts[i]);
            }
            if (sb.length() > 0) {
                sb.append(',');
            }
            sb.append(day);
            prefs(ctx).edit().putString(KEY_NOTIFIED, sb.toString()).apply();
        } catch (Exception ignored) {
        }
    }
}
