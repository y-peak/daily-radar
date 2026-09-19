#!/usr/bin/env bash
# =============================================================================
#  个人情报台 —— 构建安卓 APK（**不用 Gradle**）
# =============================================================================
#
#  为什么不用 Gradle：AGP 要从 dl.google.com/maven 拉一堆依赖，
#  而我们只想把一个 WebView 壳打成包。build-tools 里的
#  aapt2 / d8 / apksigner / zipalign 四个命令行工具就够了 ——
#  整个工具链 = JDK + build-tools + platform，三个 zip，解压即用。
#
#  用法：
#      ./build.sh --token <访问口令>
#      RADAR_TOKEN=xxx ./build.sh
#      ./build.sh --token xxx --deploy        # 顺手传到服务器供手机下载
#
#  工具链准备（只需一次，约 310MB，解到 $ANDROID_SDK_ROOT）：
#      JDK 17          https://mirrors.huaweicloud.com/openjdk/17.0.2/openjdk-17.0.2_windows-x64_bin.zip
#      build-tools 34  https://dl.google.com/android/repository/build-tools_r34-windows.zip
#      platform-35     https://dl.google.com/android/repository/platform-35_r02.zip
#
#  ⚠️ 三个容易踩的点，脚本里都已经处理：
#    1. **构建目录必须是纯 ASCII 路径**。本仓库在 `远程服务器/` 下，
#       Java 系工具（aapt2/d8/javac）在中文路径上容易出幺蛾子
#       → 先把源码拷到 $ANDROID_SDK_ROOT/build/apk 再编译，产物最后再拷回来。
#    2. **给 Windows 程序的参数要用 Windows 风格的路径**（`C:/...`）。
#       Git Bash 的自动路径转换时灵时不灵，尤其 `javac @文件` 里的路径
#       **完全不转换** —— 所以 sources.txt/classes.txt 里写的就是 `C:/...`。
#    3. **必须先 zipalign 再 apksigner**。反了签名会失效；而且 targetSdk>=30
#       强制要 v2 签名，所以 `jarsigner` 不够，必须用 apksigner。
# =============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------- 参数
TOKEN="${RADAR_TOKEN:-}"
DEPLOY=0
SERVER="${RADAR_SERVER:-volcano}"
# 必须落在 radar 的 `<root>/apk/` —— web.py 的 /dl/<name> 只认这个目录
# （放 /var/www/dl 之类不会被任何路由读到，--deploy 等于白传）
REMOTE_DIR="${RADAR_REMOTE_DIR:-/root/daily-radar/apk}"

# 上一次构建的版本号。**必须放在 dist/ 之外** —— dist/ 每次构建都被清空，
# 版本历史放里面等于没有。这个文件已在 .gitignore 里。
LAST_FILE="$HERE/.last_version"
LAST_CODE=0
LAST_NAME="1.0"
if [ -f "$LAST_FILE" ]; then
    read -r LAST_CODE LAST_NAME < "$LAST_FILE" || true
    case "$LAST_CODE" in ''|*[!0-9]*) LAST_CODE=0 ;; esac
    [ -n "$LAST_NAME" ] || LAST_NAME="1.0"
fi

# 把 "1.2" 里的结尾数字 +1 → "1.3"；抠不出结尾数字就原样返回（宁可不动也别乱改）。
bump_name() {
    _n="$(printf '%s' "$1" | sed -nE 's/.*[^0-9]([0-9]+)$/\1/p')"
    if [ -z "$_n" ]; then
        printf '%s' "$1"
        return
    fi
    printf '%s' "$1" | sed -E "s/[0-9]+$/$((_n + 1))/"
}

# ⭐ 版本号默认**自动递增**，而不是写死。
#    写死的后果很严重：忘了带 --code 就会打出比线上更旧的包，而 `/app`
#    是按 mtime 取最新的 → 线上"最新版"反而变旧 → **所有人从此收不到更新提示**，
#    而且全程不报任何错。这是最典型也最难发现的一类静默失效。
#    （同类问题的判据：**别让"默认值"能指向一个比现状更旧的版本**。）
VERSION_CODE="${VERSION_CODE:-$((LAST_CODE + 1))}"
if [ -z "${VERSION_NAME:-}" ]; then
    VERSION_NAME="$(bump_name "$LAST_NAME")"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --token)   TOKEN="${2:-}"; shift 2 ;;
        --token=*) TOKEN="${1#*=}"; shift ;;
        --deploy)  DEPLOY=1; shift ;;
        --code)    VERSION_CODE="${2:-}"; shift 2 ;;
        --name)    VERSION_NAME="${2:-}"; shift 2 ;;
        --allow-stale) ALLOW_STALE=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

# 版本必须比上一次新 —— 否则线上"最新版"会被更旧的包顶掉。
if [ "$VERSION_CODE" -le "$LAST_CODE" ]; then
    if [ "$DEPLOY" = "1" ] && [ "${ALLOW_STALE:-0}" != "1" ]; then
        cat >&2 <<MSG
✗ 版本号没有前进：本次 versionCode=$VERSION_CODE，上次是 $LAST_CODE。
  上传一个更旧的包会把线上"最新版"顶掉，所有已装的 App **再也收不到更新提示**。
  （/app 是按 mtime 取最新的，它不认版本号大小）
  确认要这么做就加 --allow-stale；否则请用更大的 --code。
MSG
        exit 2
    fi
    echo "⚠️  versionCode=$VERSION_CODE 不大于上次的 $LAST_CODE（仅本地构建，未上传）" >&2
fi

if [ -z "$TOKEN" ]; then
    if [ -f "$HERE/.token" ]; then
        TOKEN="$(tr -d ' \r\n' < "$HERE/.token")"
    else
        cat >&2 <<'MSG'
✗ 缺少访问口令。三种给法任选一种：
    ./build.sh --token <口令>
    RADAR_TOKEN=<口令> ./build.sh
    echo '<口令>' > app/android/.token      # 该文件已在 .gitignore 里
MSG
        exit 2
    fi
fi

# ---------------------------------------------------------------- 工具链
SDK_WIN="${ANDROID_SDK_ROOT:-C:/Users/y_pea/.workbuddy/toolchains/android}"
to_unix() { cygpath -u "$1" 2>/dev/null || echo "$1"; }
SDK="$(to_unix "$SDK_WIN")"
JDK="$SDK/jdk/jdk-17.0.2"
BT="$SDK/build-tools/android-14"
ANDROID_JAR="$SDK_WIN/platform/android-35/android.jar"

# B  = 给 Windows 程序用的路径；  BU = 给 shell 用的路径
B="$SDK_WIN/build/apk"
BU="$SDK/build/apk"

[ -d "$JDK" ]         || { echo "✗ 找不到 JDK：$JDK" >&2; exit 1; }
[ -d "$BT" ]          || { echo "✗ 找不到 build-tools：$BT" >&2; exit 1; }
[ -f "$(to_unix "$ANDROID_JAR")" ] || { echo "✗ 找不到 android.jar：$ANDROID_JAR" >&2; exit 1; }

# ⚠️ JAVA_HOME 必须是 **Windows 风格**路径：d8.bat / apksigner.bat 是批处理，
#    它拿 JAVA_HOME 去拼 "…\bin\java.exe"，给 /c/… 会报 invalid directory。
#    而 PATH 里给 shell 用的那份要用 unix 风格，否则 bash 找不到 javac。
JDK_WIN="$SDK_WIN/jdk/jdk-17.0.2"
export JAVA_HOME="$JDK_WIN"
export PATH="$JDK/bin:$BT:$PATH"

# Windows 上这些工具是 .exe / .bat
if [ -f "$BT/aapt2.exe" ]; then
    AAPT2="$BT/aapt2.exe"; D8="$BT/d8.bat"
    APKSIGNER="$BT/apksigner.bat"; ZIPALIGN="$BT/zipalign.exe"
    KEYTOOL="$JDK/bin/keytool.exe"
else
    AAPT2="$BT/aapt2"; D8="$BT/d8"
    APKSIGNER="$BT/apksigner"; ZIPALIGN="$BT/zipalign"
    KEYTOOL="$JDK/bin/keytool"
fi

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in python3 python py; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
fi
[ -n "$PY" ] || { echo "✗ 需要 python（用于把 classes.dex 塞进 APK）" >&2; exit 1; }

# ---------------------------------------------------------------- 签名密钥
KS_WIN="${RADAR_APK_KEYSTORE:-C:/Users/y_pea/.workbuddy/keys/radar-apk.jks}"
KS="$(to_unix "$KS_WIN")"
KS_PASS="${RADAR_APK_PASS:-}"
if [ -z "$KS_PASS" ]; then
    if [ -f "$KS.pass" ]; then
        KS_PASS="$(tr -d ' \r\n' < "$KS.pass")"
    else
        # 口令随机生成，存到**仓库外**。密钥必须稳定：换了签名密钥，
        # 手机就得先卸载旧版才能装新版（签名不一致会被系统拒绝覆盖）。
        mkdir -p "$(dirname "$KS")"
        KS_PASS="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        printf '%s\n' "$KS_PASS" > "$KS.pass"
        chmod 600 "$KS.pass" 2>/dev/null || true
        echo "· 已生成签名口令 → $KS.pass（请勿提交/删除）"
    fi
fi

if [ ! -f "$KS" ]; then
    mkdir -p "$(dirname "$KS")"
    echo "· 生成签名密钥 → $KS"
    "$KEYTOOL" -genkeypair -v \
        -keystore "$KS_WIN" -alias radar -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass "$KS_PASS" -keypass "$KS_PASS" \
        -dname "CN=Personal Radar, OU=Home, O=YPeak, C=CN" >/dev/null 2>&1
fi

# ---------------------------------------------------------------- 准备构建目录
echo "· 构建目录 $B"
# ⚠️⚠️ 这一步**刻意一次删除都不做**，只建目录。理由不是省事，是这套脚本得能用：
#
#   1. 目录级的删除（`rm -rf "$BU"`、`rm -rf "$BU/classes"` …）会被执行环境的
#      **批量删除防护**拦下（"SAFE_DELETE_BULK_CONFIRM_REQUIRED"：单次上限 50，
#      而且是**按轮次累计**的）。表现是整个构建脚本静默中止、APK 一个都出不来，
#      而报错信息看起来跟安卓、跟代码都毫无关系。
#      构建脚本不该被一条外部策略弄成不可用 —— 那是把风险转嫁给了"下次想发版的人"。
#
#   2. 其实压根不需要删：构建目录里**每一个产物都是被全量覆盖的** ——
#      res/、AndroidManifest、*.java 都是全量拷贝；res.zip / base.apk /
#      unsigned.apk / aligned.apk / sources.txt / classes.txt / classes.dex
#      都是"写"而不是"追加"。
#
#   3. 唯一真正的风险是**陈旧产物**：仓库里删掉了某个 .java，构建目录里却还留着
#      上次编出来的 .class，d8 会把它一起打进 APK（然后装到手机上才发现）。
#      这个风险不靠"每次先删干净"来防 —— 那正是第 1 条要避开的动作 ——
#      而是靠下面两道**守卫**，把它变成一次看得见的失败。
mkdir -p "$BU/res_src" "$BU/java/com/ypeak/radar" "$BU/gen" "$BU/classes" "$BU/dex"

# 源码拷到 ASCII 路径（见文件头 ⚠️1），顺便注入口令
cp -r "$HERE/res/." "$BU/res_src/"
# manifest 也拷过来：aapt2 是 Windows 程序，把带中文的仓库路径直接交给它
# 会因为代码页问题"找不到文件"，绕开最省事
cp "$HERE/AndroidManifest.xml" "$BU/AndroidManifest.xml"
# ⚠️ 整个 java 目录一起拷，**不要写死文件名**。
#    以前这里是 `cp .../MainActivity.java`。加第二个类（ApkProvider）时，
#    这种写法要么编译报"找不到符号"，要么更糟 —— 如果那个类只是被反射/清单引用
#    （ContentProvider 就是被 AndroidManifest 引用的），javac 根本不检查它，
#    于是**编译通过、装上才发现类不存在**。整目录拷贝把这类漏编一次性消灭。
#    只拷 *.java：`AppConfig.java.in` 是**模板**，不参与编译。
#    （以前是"整目录拷进来、再 rm 掉那个 .in"—— 那种写法让一次构建里多出一步
#      无谓的删除，而删除正是最容易被安全策略拦下来的动作。拷的时候就别带它。）
for _src in "$HERE"/java/com/ypeak/radar/*.java; do
    [ -f "$_src" ] || continue
    cp "$_src" "$BU/java/com/ypeak/radar/"
done

# 逐个类点名确认：漏了哪个，这里立刻报出来，而不是等装到手机上。
# ⚠️ 这里只列**手写的**类。AppConfig 是下一步由模板生成的，
#    此刻还不存在 —— 把它列进来只会让构建永远失败。
#    （AppConfig 由后面的"逐字段核对"负责，那份检查更严：它比对的是内容。）
# ⚠️ 新增类时**必须**同步加到这个列表里。PlanAlarmReceiver 是"被 manifest
#    引用"的类型 —— 漏拷它的话 javac 根本不会报错（没人 new 它），
#    编译一路通过，装到手机上才发现开机/到点时接收器不存在。
for cls in MainActivity ApkProvider Notifier PlanReminder PlanAlarmReceiver; do
    [ -f "$BU/java/com/ypeak/radar/$cls.java" ] \
        || { echo "✗ 构建目录里缺 $cls.java（新增类时忘了拷？）" >&2; exit 1; }
done

# ---------------------------------------------------------------- 陈旧产物守卫（一）：资源
# 仓库里已经删掉的资源，不该还留在构建目录里被 aapt2 打进去。
# 不删、只报 —— 报出来的话，清理构建目录是人的决定，不是脚本悄悄替他做的。
_stale_res=""
while IFS= read -r _f; do
    [ -f "$HERE/res/${_f#"$BU/res_src/"}" ] || _stale_res="$_stale_res ${_f#"$BU/res_src/"}"
done < <(find "$BU/res_src" -type f)
if [ -n "$_stale_res" ]; then
    cat >&2 <<MSG
✗ 构建目录里有仓库中已不存在的资源：$_stale_res
  它们是上一次构建的残留，直接编进去会让 APK 里出现"已经删掉的东西"。
  处理：把 $BU/res_src 里这些文件删掉（或整个构建目录清空）后重跑。
MSG
    exit 1
fi

# ---------------------------------------------------------------- 由模板生成 AppConfig
# ⚠️ 注入前必须转义 sed 的替换串：
#    `&` 在 sed 替换串里表示"整个匹配到的文本"，`\` 和分隔符同理。
#    不转义的话，口令里只要有一个 `&`，生成的 URL 就会**静默变错**
#    —— 占位符是替换掉了（所以旧的检查拦不住），但内容是错的。
#    这和整条流水线里"安静失效不报错"是同一类坑，必须在源头堵。
esc_sed() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

# 版本号也一起注入：App 要拿它和服务器返回的版本比大小，
# 写死在两处（manifest 的 flag 和 AppConfig）迟早会不同步。
sed -e "s|__RADAR_TOKEN__|$(esc_sed "$TOKEN")|g" \
    -e "s|__RADAR_VERSION_CODE__|$(esc_sed "$VERSION_CODE")|g" \
    -e "s|__RADAR_VERSION_NAME__|$(esc_sed "$VERSION_NAME")|g" \
    "$HERE/java/com/ypeak/radar/AppConfig.java.in" \
    > "$BU/java/com/ypeak/radar/AppConfig.java"

# 逐个占位符检查：漏一个不会报错，只会让 App 里出现字面的
# "__RADAR_VERSION_NAME__" 之类，字符串位置的占位符漏了会**静默出错**，
# 所以要显式拦住。
for ph in __RADAR_TOKEN__ __RADAR_VERSION_CODE__ __RADAR_VERSION_NAME__; do
    if grep -q "$ph" "$BU/java/com/ypeak/radar/AppConfig.java"; then
        echo "✗ 占位符 $ph 没被替换（口令里有特殊字符？）" >&2
        exit 1
    fi
done

# 再验一遍"替换得对不对"，而不只是"替换过了"：
# 生成结果里必须真的出现那串 token 和三处版本常量。
grep -qF "TOKEN = \"$TOKEN\";" "$BU/java/com/ypeak/radar/AppConfig.java" \
    || { echo "✗ 生成的 AppConfig 里 TOKEN 与传入口令不一致（sed 转义有问题？）" >&2; exit 1; }
grep -qF "VERSION_CODE = $VERSION_CODE;" "$BU/java/com/ypeak/radar/AppConfig.java" \
    || { echo "✗ 生成的 AppConfig 里 VERSION_CODE 不对" >&2; exit 1; }
grep -qF "VERSION_NAME = \"$VERSION_NAME\";" "$BU/java/com/ypeak/radar/AppConfig.java" \
    || { echo "✗ 生成的 AppConfig 里 VERSION_NAME 不对" >&2; exit 1; }

# ---------------------------------------------------------------- 1/6 编译资源
echo "· 1/6 aapt2 compile"
( cd "$BU" && "$AAPT2" compile --dir res_src -o res.zip )

# ---------------------------------------------------------------- 2/6 链接资源 + 生成 R.java
echo "· 2/6 aapt2 link"
( cd "$BU" && "$AAPT2" link \
    -o base.apk \
    -I "$ANDROID_JAR" \
    --manifest AndroidManifest.xml \
    -R res.zip \
    --java gen \
    --auto-add-overlay \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    --min-sdk-version 24 \
    --target-sdk-version 35 )

# ---------------------------------------------------------------- 3/6 编译 Java
echo "· 3/6 javac"
# android.jar 是 Java 8 字节码（major=52），所以用 -source/-target 8 +
# -bootclasspath 指向它 —— 编译期只认 Android 的 API，
# 不会误用 JDK 有、Android 没有的类。
find "$B/java" "$B/gen" -name '*.java' > "$BU/sources.txt"
javac -encoding UTF-8 -nowarn \
    -source 8 -target 8 -bootclasspath "$ANDROID_JAR" \
    -d "$B/classes" "@$B/sources.txt"

# ---------------------------------------------------------------- 陈旧产物守卫（二）：类
# ⚠️ 这条守卫是"不删构建目录"这个决定的前提，不能省。
#    仓库里删掉/改了名的 .java，构建目录里还会留着上次的 .class ——
#    d8 照样把它打进 APK，编译**一声不响**，而手机上那个类还在。
#    这里把每一份 .class 反查回源码：找不到源码就是残留，当场拦下。
#    （内部类 X$1 取顶层名 X 再查；R.java 由 aapt2 生成在 gen/ 下。）
_stale_cls=""
while IFS= read -r _c; do
    _top="$(printf '%s' "$(basename "$_c" .class)" | sed 's/\$.*$//')"
    if [ ! -f "$BU/java/com/ypeak/radar/$_top.java" ] \
       && [ ! -f "$BU/gen/com/ypeak/radar/$_top.java" ]; then
        _stale_cls="$_stale_cls $_top"
    fi
done < <(find "$BU/classes" -name '*.class')
if [ -n "$_stale_cls" ]; then
    cat >&2 <<MSG
✗ 构建目录里有**没有源码对应**的 .class：$_stale_cls
  它们是上一次构建留下的，会被 d8 一起打进 APK（编译不报错，装到手机上才发现）。
  处理：把 $BU/classes 清空（或删掉上面那几个 .class）后重跑。
MSG
    exit 1
fi

# ---------------------------------------------------------------- 4/6 dex
echo "· 4/6 d8"
find "$B/classes" -name '*.class' > "$BU/classes.txt"
"$D8" --lib "$ANDROID_JAR" --min-api 24 --output "$B/dex" "@$B/classes.txt"

# ---------------------------------------------------------------- 5/6 打包（塞进 classes.dex）
echo "· 5/6 组装未签名 APK"
"$PY" - "$B/base.apk" "$B/dex/classes.dex" "$B/unsigned.apk" <<'PYEOF'
import sys, zipfile
base, dex, out = sys.argv[1], sys.argv[2], sys.argv[3]
with open(dex, 'rb') as fh:
    dex_bytes = fh.read()
src = zipfile.ZipFile(base, 'r')
dst = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
try:
    for item in src.infolist():
        data = src.read(item.filename)
        zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
        # ⭐ resources.arsc 必须**不压缩**：targetSdk>=30 的硬性要求，
        #    压缩了安装时会被系统直接拒绝（"resources.arsc must be stored"）。
        zi.compress_type = (zipfile.ZIP_STORED if item.filename == 'resources.arsc'
                            else zipfile.ZIP_DEFLATED)
        zi.external_attr = 0o644 << 16
        dst.writestr(zi, data)
    zi = zipfile.ZipInfo('classes.dex', date_time=(2026, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    dst.writestr(zi, dex_bytes)
finally:
    src.close(); dst.close()
with zipfile.ZipFile(out) as z:
    names = z.namelist()
    assert 'classes.dex' in names, "classes.dex 没进去"
    assert 'AndroidManifest.xml' in names, "AndroidManifest.xml 没进去"
    ct = z.getinfo('resources.arsc').compress_type
    assert ct == zipfile.ZIP_STORED, "resources.arsc 被压缩了，装不上"
    print(f"    条目 {len(names)} 个 · resources.arsc=stored · classes.dex={len(dex_bytes)}B")
PYEOF

# ---------------------------------------------------------------- 6/6 对齐 + 签名
echo "· 6/6 zipalign + apksigner"
"$ZIPALIGN" -f -p 4 "$B/unsigned.apk" "$B/aligned.apk"
SIGNED_WIN="$B/radar-$VERSION_NAME.apk"
"$APKSIGNER" sign \
    --ks "$KS_WIN" --ks-key-alias radar \
    --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
    --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
    --out "$SIGNED_WIN" "$B/aligned.apk"

# 从 ASCII 构建目录拷回仓库（产物走的是 cp，不经过 Windows 程序，中文路径无妨）
# ⚠️ 这里**不删 dist/ 里的旧包**，只覆盖本次这两个文件名。
#    删的那一行同样会被批量删除防护拦下（见"准备构建目录"那段的长注释）。
#    代价是 dist/ 会按版本号累积几个百来 KB 的旧包 —— 而 dist/ 在 .gitignore 里，
#    既不会入库也不会影响任何路由，比"构建因为删文件而整个跑不起来"划算得多。
#    （真正对外提供下载的是服务器上的 <root>/apk/，那边按版本留档本来就是有意的。）
mkdir -p "$HERE/dist"
OUT="$HERE/dist/radar-$VERSION_NAME.apk"
cp "$BU/radar-$VERSION_NAME.apk" "$OUT"

# ---------------------------------------------------------------- 校验
echo
echo "================ 校验 ================"
"$APKSIGNER" verify --verbose "$SIGNED_WIN" | sed 's/^/  /' || true
"$ZIPALIGN" -c -v 4 "$SIGNED_WIN" >/dev/null 2>&1 && echo "  4 字节对齐 ✓" || echo "  对齐 ✗"
"$AAPT2" dump badging "$SIGNED_WIN" 2>/dev/null \
    | grep -E "^(package|application-label|sdkVersion|targetSdkVersion|launchable-activity|uses-permission)" \
    | sed 's/^/  /'
# ⚠️ 别用 python 算 sha256：产物在中文目录下，而命令行参数传给 Windows 版 Python
#    会按控制台代码页解码 → 路径变乱码 → FileNotFoundError。用 msys 的 sha256sum。
if command -v sha256sum >/dev/null 2>&1; then
    SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
else
    SHA="$(certutil -hashfile "$(cygpath -w "$OUT")" SHA256 2>/dev/null | sed -n 2p | tr -d ' \r')"
fi
echo "  大小 $(du -h "$OUT" | cut -f1) · sha256 ${SHA:0:16}…"

# ---------------------------------------------------------------- 版本元数据
# 服务器上的 `/api/app` 要回答"最新是哪个版本"，而版本号**不在文件名里**
# （文件名只有 versionName）。所以构建时顺手落一份 sidecar JSON：
# 接口直接读它，不用去解析 APK 的二进制 manifest。
SIZE="$(wc -c < "$OUT" | tr -d ' \r')"
BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SIDECAR="$HERE/dist/radar-$VERSION_NAME.json"
cat > "$SIDECAR" <<JSON
{
  "versionCode": $VERSION_CODE,
  "versionName": "$VERSION_NAME",
  "size": $SIZE,
  "sha256": "$SHA",
  "built": "$BUILT",
  "notes": "${RADAR_APK_NOTES:-}"
}
JSON
echo "  版本元数据 → $SIDECAR (versionCode=$VERSION_CODE)"

# 记下这次构建的版本，供下次自动递增。**放在 dist/ 之外** ——
# dist/ 是构建产物目录（.gitignore 里），里面按版本号累积着历次产物，
# 把"上一版是什么"这种**状态**混在产物堆里，迟早会被人连产物一起清掉。
# 这个文件必须比产物活得久：它是版本号单调递增的唯一依据（见文件头的 ⚠️）。
printf '%s %s\n' "$VERSION_CODE" "$VERSION_NAME" > "$LAST_FILE"
echo "  已记录版本 → $LAST_FILE ($VERSION_CODE $VERSION_NAME)"

# ---------------------------------------------------------------- 可选：上传
if [ "$DEPLOY" = "1" ]; then
    echo
    APK_NAME="radar-$VERSION_NAME.apk"
    JSON_NAME="radar-$VERSION_NAME.json"
    echo "· 上传到 $SERVER:$REMOTE_DIR"
    # volcano 不在 PATH 里时用绝对路径兜底（沙箱的 PATH 常是旧快照）
    if ! command -v "$SERVER" >/dev/null 2>&1; then
        SERVER_BIN="/c/Users/y_pea/.workbuddy/bin/$SERVER"
        command -v "$SERVER_BIN" >/dev/null 2>&1 && SERVER="$SERVER_BIN"
    fi
    if command -v "$SERVER" >/dev/null 2>&1; then
        # cd 过去用相对文件名：绝对中文路径经 msys 传给 Windows 版 Python 会乱码
        # sidecar JSON 必须跟着 APK 一起传 —— /api/app 就是读它回答版本号的
        ( cd "$HERE/dist" \
          && "$SERVER" run "mkdir -p $REMOTE_DIR" \
          && "$SERVER" put "$APK_NAME" "$REMOTE_DIR/$APK_NAME" \
          && "$SERVER" put "$JSON_NAME" "$REMOTE_DIR/$JSON_NAME" \
          && "$SERVER" run "ls -la $REMOTE_DIR" )
        echo "  下载地址： https://118.196.100.121/app"
        echo "  版本查询： https://118.196.100.121/api/app"
        echo "  校验值：   sha256 ${SHA:0:16}…（手机上下完可对比）"
    else
        echo "  ✗ 找不到 $SERVER 命令，跳过上传" >&2
    fi
fi

echo
echo "✅ 完成：$OUT"
