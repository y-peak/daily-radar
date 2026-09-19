package com.ypeak.radar;

import android.app.AlarmManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 每日简报闹钟的落点，外加所有"排好的闹钟可能已经失效"的时机。
 *
 * 为什么和 {@link PlanAlarmReceiver} 分开两个类，而不是把分支塞进那一个：
 * 两者**互相独立** —— 用户可能只开简报、不开规划提醒（或反过来）。
 * 合成一个接收器的话，一边的异常/超时会把另一边一起拖下水；
 * 而且简报这一边要 goAsync() 拉网络，规划那边全是本地操作，
 * 寿命需求完全不同，混在一起只会让"为什么这一条会慢"变得难查。
 *
 * ⚠️ 两个接收器**都**会在开机时被叫起来，这是有意的：各自重排各自的三/一个闹钟，
 *    互不干扰。系统允许同一个 action 有多个接收器。
 *
 * ⚠️ 闹钟**不跨重启存活**（AlarmManager 重启后一律清空），所以 BOOT_COMPLETED
 *    这一条是必须的 —— 少了它，"重启过一次"就等于"以后再也不提醒了"，
 *    而且全程不报任何错。这正是本项目最忌讳的静默失效。
 */
public class BriefAlarmReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (ctx == null || intent == null) {
            return;
        }
        String action = intent.getAction();
        if (action == null) {
            return;
        }

        if (BriefAlarm.ACTION_DUE.equals(action)) {
            // 到点了。时段从 Intent 里带过来，响的时候再核一次。
            //
            // ⭐ 这里用 goAsync()：onReceive 只有约 10 秒，而这一趟要联网取简报。
            //    不用它的话，网络稍慢就会被系统直接杀掉 —— 表现是"偶尔不响"，
            //    而且查不出原因。生命交给 BriefAlarm 在后台线程里 finish()。
            BriefAlarm.onDueFired(ctx, intent.getStringExtra(BriefAlarm.EXTRA_SLOT),
                    goAsync());
            return;
        }

        if (Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            // ⭐ 带补发：闹钟不跨重启，重启会把当天已经过点的那次直接吃掉。
            //    补发窗口很窄（90 分钟，见 BriefAlarm.CATCHUP_WINDOW_MS）——
            //    简报的时段含义绑着时间，早上的简报晚上才弹反而更糟。
            //
            // ⚠️ 补发要联网，所以这里同样走 goAsync()（细节见 BriefAlarm.catchUp）：
            //    这条路的调用点在主线程上，直接同步做网络会被系统判成
            //    NetworkOnMainThreadException —— 不崩，但每条补发都变成"拿不到"。
            //    重排闹钟由 catchUp 在后台线程之前先做掉（纯本地，很快）。
            BriefAlarm.catchUp(ctx, goAsync());
            return;
        }

        // 下面这几个都不补发，只重排：用户此刻就在用手机（或刚改完系统时间），
        // 这时候糊一条通知过去纯属打扰。
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)      // 覆盖安装：闹钟也没了
                || Intent.ACTION_TIME_CHANGED.equals(action)      // 改了系统时间，绝对时刻要重算
                || Intent.ACTION_TIMEZONE_CHANGED.equals(action)  // 换了时区，"08:00"的含义变了
                || AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED.equals(action)) {
            // 最后那个是"闹钟与提醒"权限被授予的通知：刚才只能用不精确闹钟排的，
            // 现在有权限了，重排一次就能升回准点。
            BriefAlarm.reschedule(ctx);
        }
    }
}
