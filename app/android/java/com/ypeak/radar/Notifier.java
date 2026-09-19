package com.ypeak.radar;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;

/**
 * 系统通知 —— 规划到期提醒长这样。
 *
 * 用户的原话是"类似于微信来了消息的弹窗这种"，所以要的是**系统通知**，
 * 不是页面里盖一层浮层：顶上弹出来、有声有震动、留在通知栏里、
 * 点开进 App。这只有走 NotificationManager 才做得到。
 *
 * 几条刻意的选择：
 *
 *   · **不用 AndroidX 的 NotificationCompat**。本工程不引任何依赖库
 *     （不跑 Gradle，APK 里只有我们自己编出来的 classes.dex），
 *     框架自带的 Notification.Builder 就够。代价是要自己处理
 *     API 26 前后两套构造方式 —— 见 {@link #build}。
 *
 *   · **importance = HIGH** —— 只有 HIGH 才会"顶上弹出来"（heads-up）。
 *     DEFAULT 只是条静音横幅，跟用户要的那个效果差得远。
 *     注意：用户在系统设置里把渠道调低之后，我们再建多少次都改不回来
 *     （那是用户的选择，系统不允许应用覆盖）—— 所以设置页只如实显示状态，
 *     不假装能改。
 *
 *   · **渠道一旦创建就不重建**。渠道的 importance/sound 只在创建时生效；
 *     反复 createNotificationChannel 不会升级它，只会白白多一次系统调用。
 *
 *   · **权限要分开看**：API 33 起 POST_NOTIFICATIONS 是运行时权限；
 *     而在**任何**版本上，用户都能在系统设置里把这个应用的通知整个关掉
 *     （areNotificationsEnabled()）。两者都得查 —— 只查前者的话，
 *     在 API 32 上会"以为能发、实际发不出去"，属于本项目最忌讳的安静失效。
 */
final class Notifier {

    /** 渠道 id。改它等于换一个渠道 —— 老渠道的设置会留在系统里，所以别乱改。 */
    static final String CHANNEL_ID = "radar_plans";

    /**
     * 每日简报**单独的渠道**。
     *
     * 为什么不共用 CHANNEL_ID：系统设置里是按渠道静音的 —— 有人只想要每天
     * 的大盘简报、不想要待办式的规划提醒（或者反过来）。共用渠道的话这两件事
     * 只能一起开、一起关。另外渠道名也要诚实：简报塞进叫「规划提醒」的渠道里，
     * 用户去系统设置根本找不到"简报"两个字。
     */
    static final String CHANNEL_BRIEF_ID = "radar_brief";

    /**
     * 到期提醒的固定通知 id。
     *
     * ⭐ 用**固定值**而不是每条规划一个：同一天到期的几条合成一条通知
     * （见 PlanReminder.notifyFor），所以同一时刻只需要一条通知。
     * 固定 id 还顺带保证了"今天已经提醒过"不会被后来的覆盖成两条。
     */
    private static final int ID_DUE = 4101;

    /** 测试提醒用另一个 id：它和真正的到期提醒互不覆盖。 */
    private static final int ID_TEST = 4102;

    /**
     * 每日简报的通知 id。**一个时段一个**，从 {@link BriefAlarm#ID_BRIEF_BASE} 起算。
     *
     * ⚠️ 这里必须按时段分开，不能像到期提醒那样共用一个固定 id：
     *    到期提醒是"某一天"的一件事（所以一天一条），而简报一天三条、
     *    每条的内容都不一样 —— 共用一个 id 的话，16:40 的收盘简报会把
     *    早上那条还没看的早间简报**直接顶掉**，用户永远看不到。
     */
    static final int ID_BRIEF_BASE = 4201;

    /** 简报的"发一条看看"用另一个 id，免得把当天真该留着的那条覆盖了。 */
    static final int ID_BRIEF_TEST = 4210;

    private static final int REQ_TAP = 102;
    private static final int REQ_TAP_BRIEF = 103;

    private Notifier() {
    }

    // ------------------------------------------------------------------ 权限

    /**
     * 现在到底能不能发出通知。
     *
     * 两条都得满足：API 33+ 的运行时权限 **并且** 系统设置里的通知开关是开的。
     * 返回 false 时，上层必须**如实告诉用户**（设置页有状态行 + 授权入口），
     * 而不是照样往下走然后什么也没发生。
     */
    static boolean canNotify(Context ctx) {
        if (!hasRuntimePermission(ctx)) {
            return false;
        }
        NotificationManager nm = manager(ctx);
        if (nm == null) {
            return false;
        }
        try {
            return nm.areNotificationsEnabled();
        } catch (Exception e) {
            // 判断不了就别拦着 —— 宁可让它去试一次，也不要凭空说"你被禁了"
            return true;
        }
    }

    /** API 33 起 POST_NOTIFICATIONS 是运行时权限；更早的版本一律算已给。 */
    static boolean hasRuntimePermission(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return true;
        }
        try {
            return ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return true;
        }
    }

    // ------------------------------------------------------------------ 渠道

    /** 建通知渠道。API 26 之前没有渠道这个概念，直接返回。 */
    static void ensureChannel(Context ctx) {
        ensureChannel(ctx, CHANNEL_ID, R.string.notify_channel,
                R.string.notify_channel_desc);
    }

    /** 建每日简报的渠道。 */
    static void ensureBriefChannel(Context ctx) {
        ensureChannel(ctx, CHANNEL_BRIEF_ID, R.string.notify_brief_channel,
                R.string.notify_brief_channel_desc);
    }

    private static void ensureChannel(Context ctx, String id, int nameRes, int descRes) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager nm = manager(ctx);
        if (nm == null) {
            return;
        }
        try {
            if (nm.getNotificationChannel(id) != null) {
                return;          // 已存在：不重建，免得和用户的调整打架
            }
            NotificationChannel ch = new NotificationChannel(
                    id,
                    ctx.getString(nameRes),
                    NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription(ctx.getString(descRes));
            ch.enableVibration(true);
            ch.setVibrationPattern(new long[]{0, 180, 120, 180});
            Uri sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
            if (sound != null) {
                ch.setSound(sound, new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
            }
            ch.enableLights(true);
            ch.setShowBadge(true);
            nm.createNotificationChannel(ch);
        } catch (Exception ignored) {
            // 建不出渠道不该让提醒逻辑崩掉：下面发通知时会再兜一层
        }
    }

    // ------------------------------------------------------------------ 发送

    /**
     * 发一条"到期"通知。
     *
     * @return 真的发出去了没有。false 时调用方**不要**把这一天标记成"已提醒"，
     *         这样等用户把权限开回来，下一次还有机会补上。
     */
    static boolean notifyDue(Context ctx, String title, String text, String bigText) {
        return post(ctx, CHANNEL_ID, ID_DUE, title, text, bigText, tapIntent(ctx));
    }

    /** 设置页的"测试提醒"：立刻发一条，内容和到期提醒长得一样。 */
    static boolean notifyTest(Context ctx) {
        return post(ctx,
                CHANNEL_ID,
                ID_TEST,
                ctx.getString(R.string.notify_test_title),
                ctx.getString(R.string.notify_test_text),
                ctx.getString(R.string.notify_test_big),
                tapIntent(ctx));
    }

    /**
     * 发一条每日简报。
     *
     * 点它的落点**不是**规划面板（那是到期提醒的语义），而是直接进 App ——
     * 简报要连网才拿得到，点进来看到的还是那个 WebView，没必要多一个"掀面板"的动作。
     */
    static boolean notifyBrief(Context ctx, int id, String title, String text,
                               String bigText) {
        return post(ctx, CHANNEL_BRIEF_ID, id, title, text, bigText,
                briefTapIntent(ctx));
    }

    private static boolean post(Context ctx, String channelId, int id, String title,
                                String text, String bigText, PendingIntent tap) {
        if (!canNotify(ctx)) {
            return false;               // 没权限就别往下走，调用方会看到 false
        }
        if (CHANNEL_BRIEF_ID.equals(channelId)) {
            ensureBriefChannel(ctx);
        } else {
            ensureChannel(ctx);
        }
        NotificationManager nm = manager(ctx);
        if (nm == null) {
            return false;
        }
        try {
            nm.notify(id, build(ctx, channelId, title, text, bigText, tap));
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    @SuppressWarnings("deprecation")
    private static Notification build(Context ctx, String channelId, String title,
                                      String text, String bigText, PendingIntent tap) {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(ctx, channelId);
        } else {
            // API 24/25：没有渠道，声音/震动得挂在这一条上
            b = new Notification.Builder(ctx);
            b.setPriority(Notification.PRIORITY_HIGH);
            b.setDefaults(Notification.DEFAULT_SOUND | Notification.DEFAULT_VIBRATE);
        }

        b.setSmallIcon(android.R.drawable.ic_popup_reminder)
                .setContentTitle(title)
                .setContentText(text)
                .setAutoCancel(true)
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setContentIntent(tap);

        if (bigText != null && bigText.length() > 0) {
            // 折叠态一行放不下时，展开能看到完整清单
            b.setStyle(new Notification.BigTextStyle().bigText(bigText));
        }
        return b.build();
    }

    /**
     * 点通知要打开的东西。
     *
     * ⚠️ 这里用的是 `FLAG_IMMUTABLE`：API 31 起 PendingIntent **必须**声明可变性，
     * 不声明会直接抛异常。我们不需要外部改这个 Intent，所以选不可变。
     * 这个常量在 API 23 就有了，minSdk 24 上直接可用。
     *
     * 动作是"打开 App 并顺带把规划面板掀开" —— 由 MainActivity 读 EXTRA_OPEN_PLANS
     * 后通过 JS 钩子完成（见 MainActivity.dispatchPendingPanel）。
     */
    private static PendingIntent tapIntent(Context ctx) {
        Intent it = new Intent(ctx, MainActivity.class);
        it.setAction(Intent.ACTION_MAIN);
        it.addCategory(Intent.CATEGORY_LAUNCHER);
        it.putExtra(PlanReminder.EXTRA_OPEN_PLANS, true);
        it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(ctx, REQ_TAP, it,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /**
     * 点每日简报的落点：**只**打开 App，不带"掀开规划面板"那个 extra。
     *
     * ⚠️ 用不同的 requestCode（REQ_TAP_BRIEF）：PendingIntent 的身份是
     *    (requestCode, Intent 的 action/data/component…) 一起算的，
     *    两个语义不同的跳转共用一个 requestCode 会互相覆盖，表现为
     *    "点简报却掀开了规划面板"这种莫名其妙的行为。
     */
    private static PendingIntent briefTapIntent(Context ctx) {
        Intent it = new Intent(ctx, MainActivity.class);
        it.setAction(Intent.ACTION_MAIN);
        it.addCategory(Intent.CATEGORY_LAUNCHER);
        it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(ctx, REQ_TAP_BRIEF, it,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** 撤掉还没被点掉的通知（用户把规划全清了之后就别留着了）。 */
    static void cancelDue(Context ctx) {
        NotificationManager nm = manager(ctx);
        if (nm == null) {
            return;
        }
        try {
            nm.cancel(ID_DUE);
        } catch (Exception ignored) {
        }
    }

    private static NotificationManager manager(Context ctx) {
        try {
            return (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        } catch (Exception e) {
            return null;
        }
    }
}
