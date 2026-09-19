package com.ypeak.radar;

import android.app.AlarmManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 到期闹钟的落点，外加所有"排好的闹钟可能已经失效"的时机。
 *
 * 为什么要单独一个接收器，而不是把逻辑塞进 MainActivity：
 * 这些广播**必须在 App 没打开时也能收到**。放在 Activity 里的话，
 * 开机之后没人去点那个图标，重排就永远不会发生 —— 提醒从重启那一刻起静默失效。
 *
 * ⚠️ 广播接收器跑在**主线程**，而且系统给它的大约只有 10 秒。
 *    这里做的全是本地活（读一串 JSON、排一个闹钟、发一条通知），
 *    几毫秒就完了，不需要 goAsync()。
 *
 * ⚠️ 闹钟**不跨重启存活**（AlarmManager 里的东西重启后一律清空），
 *    所以 BOOT_COMPLETED 这一条是必须的 —— 少了它，"重启过一次"就等于
 *    "以后再也不提醒了"，而且全程不会报任何错。
 */
public class PlanAlarmReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (ctx == null || intent == null) {
            return;
        }
        String action = intent.getAction();
        if (action == null) {
            return;
        }

        if (PlanReminder.ACTION_DUE.equals(action)) {
            // 到点了。日期从 Intent 里带过来，响的时候再核一次。
            PlanReminder.onDueFired(ctx, intent.getStringExtra(PlanReminder.EXTRA_DAY));
            return;
        }

        if (Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            // ⭐ 这里带 true：闹钟不跨重启，重启会把当天 9:00 那次直接吃掉。
            //    如果开机时已经过了 9:00、当天又有没做完的规划，补一条 ——
            //    否则用户会以为"开着手机反而没提醒"。
            PlanReminder.reschedule(ctx, true);
            return;
        }

        // 下面这几个都不补发，只重排：用户此刻就在用手机（或者刚改完系统时间），
        // 这时候糊一条通知过去纯属打扰。
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)      // 覆盖安装：闹钟也没了
                || Intent.ACTION_TIME_CHANGED.equals(action)      // 改了系统时间，绝对时刻要重算
                || Intent.ACTION_TIMEZONE_CHANGED.equals(action)  // 换了时区，9:00 的含义变了
                || AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED.equals(action)) {
            // 最后那个是"闹钟与提醒"权限被授予的通知：刚才只能用不精确闹钟排的，
            // 现在有权限了，重排一次就能升回准点。
            PlanReminder.reschedule(ctx);
        }
    }
}
