#!/usr/bin/env bash
# 把「起静态服务器 → 起无头 Chrome → 跑探针 → 收摊」串成一条命令。
# 阅读器探针和设置页探针共用这个外壳 —— 环境搭建那套每次都一样，
# 分两份写迟早会漂移（一份改了端口、另一份没改）。
#
# 用法：
#     bash tools/run_ui_probe.sh                       # 默认跑阅读器探针
#     bash tools/run_ui_probe.sh tools/settings_probe.mjs
#     PORT=8098 PROBE=tools/settings_probe.mjs bash tools/run_ui_probe.sh
#
# 为什么要这个外壳：
#  1) 探针本身只讲 CDP，不管环境。
#  2) ⚠️ 收摊时**绝不能** `taskkill /IM chrome.exe` —— 会把用户自己开的 Chrome 一起杀掉。
#     正规做法是通过 CDP 让浏览器自己退（Browser.close），只影响我们起的这个实例。
set -u

export PATH="/usr/bin:/bin:$PATH"
cd "$(dirname "$0")/.." || exit 2

PROBE="${1:-${PROBE:-tools/reader_probe.mjs}}"
PY="C:/Users/y_pea/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODE="C:/Users/y_pea/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
PORT="${PORT:-8099}"
CDP_PORT="${CDP_PORT:-9333}"

# ⚠️⚠️ --user-data-dir 必须是 **Windows 风格** 路径。
#
# 这个坑花了很久才定位，记在这里：本机 TEMP=/tmp，所以 `mktemp -d` 返回的是
# `/tmp/tmp.XXXX`。bash 自己用没问题，但 chrome.exe 是 Windows 原生程序，
# 它**不认识**这种路径 —— 而且失败方式极其隐蔽：
#   · 没有任何输出（stdout/stderr 都是空的）
#   · 不监听调试端口
#   · 进程起来一下就没了
# 于是探针那一侧只能报"连不上 CDP"，看起来像探针或网络的问题，其实完全无关。
# （同类坑在本项目里出现过一次：给 Windows 程序传参数必须用 C:/... 形式。）
UD="$(cygpath -m "$(mktemp -d)")"

[ -x "$CHROME" ] || { echo "找不到 Chrome：$CHROME"; exit 2; }
[ -f "$PROBE" ] || { echo "找不到探针脚本：$PROBE"; exit 2; }
[ -f public/index.html ] || { echo "public/ 还没构建，先 python -m radar build"; exit 2; }

"$PY" -m http.server "$PORT" --directory public >/tmp/ui_probe_httpd.log 2>&1 &
HTTPD=$!
"$CHROME" --headless=new --remote-debugging-port="$CDP_PORT" --user-data-dir="$UD" \
  --no-first-run --no-default-browser-check --disable-gpu about:blank \
  >/tmp/ui_probe_chrome.log 2>&1 &
CHROME_PID=$!

cleanup() {
  # 让 Chrome 自杀（只关这个实例，不动用户已开的窗口）
  "$NODE" -e "
    (async () => {
      try {
        const l = await (await fetch('http://127.0.0.1:${CDP_PORT}/json/version')).json();
        const ws = new WebSocket(l.webSocketDebuggerUrl);
        await new Promise(r => ws.onopen = r);
        ws.send(JSON.stringify({id:1, method:'Browser.close'}));
        await new Promise(r => setTimeout(r, 600));
      } catch (e) {}
      process.exit(0);
    })();
  " >/dev/null 2>&1
  kill "$HTTPD" 2>/dev/null
  kill "$CHROME_PID" 2>/dev/null
  sleep 1
  rm -rf "$UD" 2>/dev/null
}
trap cleanup EXIT

# 等两个服务就绪 —— 而且是**分开判定**。
#
# 为什么要把 Chrome 单独拎出来报错：Chrome 起不来时它自己一句日志都不写，
# 最后只会表现为"探针报错：连不上 CDP"。那句话把排查方向指错了（会让人去查
# 网络和探针），实际上问题在浏览器根本没起来。这里直接当场把 chrome 日志
# 打出来，让失败自己说清自己。
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 0.4
done
if ! curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
  echo "✗ 静态服务器没起来（端口 $PORT）。日志：" >&2
  tail -5 /tmp/ui_probe_httpd.log >&2
  exit 3
fi

CDP_UP=0
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    CDP_UP=1
    break
  fi
  sleep 0.4
done
if [ "$CDP_UP" != "1" ]; then
  cat >&2 <<MSG
✗ 无头 Chrome 没能起来（调试端口 $CDP_PORT 没有监听）。
  探针接下来只会报"连不上 CDP"，所以在这里先拦下。
  常见原因（按可能性排序）：
    · --user-data-dir 给了 POSIX 路径（/tmp/...）—— Windows 版 Chrome 不认，会静默退出
    · 端口被别的实例占了（换 CDP_PORT=9334 再试）
  本次 profile 目录：$UD
  chrome 日志：
MSG
  sed 's/^/    /' /tmp/ui_probe_chrome.log >&2 2>/dev/null
  exit 4
fi

echo "探针：$PROBE   站点：http://127.0.0.1:$PORT   CDP：http://127.0.0.1:$CDP_PORT"
"$NODE" "$PROBE" "http://127.0.0.1:$CDP_PORT" "http://127.0.0.1:$PORT"
