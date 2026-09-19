package com.ypeak.radar;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.regex.Pattern;

/**
 * 极小的只读 ContentProvider：把下载目录里的 APK 以 `content://` 形式暴露出去。
 *
 * <p>为什么需要它：从 Android 7（API 24）起，把 `file://` 的 URI 交给别的应用
 * 会直接抛 `FileUriExposedException` —— 这是系统在防"路径信息泄漏"。
 * 所以"把下好的安装包交给系统安装器"这条路必须走 `content://`。
 *
 * <p>为什么不用 androidx 的 FileProvider：本工程刻意不引任何依赖（不跑 Gradle，
 * 整个 APK 只有自己的 classes.dex + 资源）。为了一个"读文件"的能力把
 * AndroidX 拖进来不值得，框架自带的 ContentProvider 足够。
 *
 * <p>⚠️ 安全边界全在 {@link #resolve} 里：只认**下载目录下、形如 *.apk 的
 * 单层文件名**。路径穿越（`../`）和任意子目录都被挡在门外 ——
 * 这个 Provider 一旦被别的应用利用，泄漏的就是任意文件了。
 * 另外清单里 `exported="false"`，外部应用本来就调不到它；
 * 真正允许的只有我们通过 Intent 临时授权出去的那一个 URI。
 */
public class ApkProvider extends ContentProvider {

    /** 必须和 AndroidManifest.xml 里的 android:authorities 完全一致。 */
    public static final String AUTHORITY = "com.ypeak.radar.apk";

    /**
     * 允许的文件名：以字母或数字开头，只含字母/数字/下划线/点/连字符，且以 .apk 结尾。
     *
     * <p>刻意**不允许** `/` 和 `..` —— 用白名单而不是"过滤掉 .."，
     * 因为黑名单总有想不到的写法（URL 编码、连续斜杠、Windows 分隔符…）。
     */
    private static final Pattern SAFE_NAME =
            Pattern.compile("^[A-Za-z0-9][A-Za-z0-9_.-]*\\.apk$");

    /** 给某个已下载的文件拼出它的 content:// 地址。 */
    public static Uri uriFor(String name) {
        return Uri.parse("content://" + AUTHORITY + "/" + name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    /** 安装器会看这个 MIME 来决定怎么处理。 */
    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = resolve(uri);
        // 只读：这个 Provider 存在的唯一目的就是把包送出去，不该有写入口。
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    /** 把 URI 解析成一个**确定在下载目录里**的文件；任何可疑输入一律拒绝。 */
    private File resolve(Uri uri) throws FileNotFoundException {
        String name = (uri == null) ? null : uri.getLastPathSegment();
        if (name == null || !SAFE_NAME.matcher(name).matches()) {
            throw new FileNotFoundException("不接受的路径：" + name);
        }
        if (getContext() == null) {
            throw new FileNotFoundException("Provider 还没有 Context");
        }
        File dir = getContext().getExternalFilesDir(null);
        if (dir == null) {
            throw new FileNotFoundException("应用外部目录不可用");
        }
        File f = new File(dir, name);
        if (!f.isFile()) {
            throw new FileNotFoundException("没有这个文件：" + name);
        }
        return f;
    }

    // ---------------------------------------------------------- 只读：其余一律拒绝
    // 显式抛 UnsupportedOperationException 而不是返回 null/0：
    // 万一将来有人误用，报错比"静默什么都不做"好查得多。
    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("ApkProvider 是只读的");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("ApkProvider 是只读的");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("ApkProvider 是只读的");
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        throw new UnsupportedOperationException("ApkProvider 是只读的");
    }
}
