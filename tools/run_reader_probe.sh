#!/usr/bin/env bash
# 把「起静态服务器 → 起无头 Chrome → 跑探针 → 收摊」串成一条命令。
#
# 为什么要这个外壳：
#  1) 探针本身只讲 CDP，不管环境；环境搭建每次都一样，写下来省得记错端口。
#  2) ⚠️ 收摊时**绝不能** `taskkill /IM chrome.exe` —— 会把用户自己开的 Chrome 一起杀掉。
#     正规做法是通过 CDP 让浏览器自己退（Browser.close），只影响我们起的这个实例。
set -u

export PATH="/usr/bin:/bin:$PATH"
cd "$(dirname "$0")/.." || exit 2

PY="C:/Users/y_pea/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODE="C:/Users/y_pea/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"
PORT="${PORT:-8099}"
CDP_PORT="${CDP_PORT:-9333}"
UD="$(mktemp -d)"

[ -x "$CHROME" ] || { echo "找不到 Chrome：$CHROME"; exit 2; }
[ -f public/reader/index.html ] || { echo "public/reader 还没构建，先 python -m radar build"; exit 2; }

"$PY" -m http.server "$PORT" --directory public >/tmp/reader_probe_httpd.log 2>&1 &
HTTPD=$!
"$CHROME" --headless=new --remote-debugging-port="$CDP_PORT" --user-data-dir="$UD" \
  --no-first-run --no-default-browser-check --disable-gpu about:blank \
  >/tmp/reader_probe_chrome.log 2>&1 &
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

# 等两个服务就绪
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/reader/" >/dev/null 2>&1 \
     && curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done

"$NODE" tools/reader_probe.mjs "http://127.0.0.1:$CDP_PORT" "http://127.0.0.1:$PORT"
