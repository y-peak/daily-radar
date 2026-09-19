#!/usr/bin/env bash
# 部署后的健康检查。输出一份"现在到底怎么样"的快照，用来替代一堆手敲的 curl。
#
#   bash deploy/healthcheck.sh              # 只检查
#   bash deploy/healthcheck.sh --refresh    # 额外触发一次采集，验证更新口子
#
# 重点验证"更新口子"这条链路：
#   页面里烘的指纹  ==  /api/status 报的指纹   ← 相等才说明"指纹变了 ⇔ 数据更新了"
#
# 口令/端口从 .env 读，和服务本体同一套来源，不用手动传参。
set -uo pipefail

APP_DIR="${APP_DIR:-/root/daily-radar}"
PORT="${PORT:-}"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || PY=python3

cd "$APP_DIR"

# ------------------------------------------------- 端口与口令（.env 优先）
if [ -f "$APP_DIR/.env" ]; then
  # shellcheck disable=SC1090
  . <(grep -E '^\s*(RADAR_PORT|RADAR_TOKEN|RADAR_IP)\s*=' "$APP_DIR/.env" | sed 's/^/export /')
fi
if [ -z "$PORT" ]; then
  PORT="${RADAR_PORT:-$(grep -oP '"port"\s*:\s*\K[0-9]+' "$APP_DIR/config.json" | head -1)}"
fi
PORT="${PORT:-18090}"
BASE="http://127.0.0.1:$PORT"
TOKEN="${RADAR_TOKEN:-}"

AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "X-Radar-Token: $TOKEN")

fp() { curl -s "${AUTH[@]}" --max-time 10 "$BASE/api/status" \
       | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["fingerprint"])'; }

echo "================ 1. 服务状态 ================"
systemctl is-active radar >/dev/null 2>&1 \
  && echo "radar.service       active" \
  || { echo "radar.service       未运行！"; journalctl -u radar -n 20 --no-pager; exit 1; }
systemctl is-enabled radar.timer >/dev/null 2>&1 \
  && echo "radar.timer         enabled" || echo "radar.timer         未启用"
systemctl list-timers radar.timer --no-pager 2>/dev/null | head -2 | tail -1
echo "监听端口            $PORT  $BASE"
[ -n "$TOKEN" ] && echo "访问口令            已启用（healthcheck 自动带上）"

echo
echo "================ 2. 页面可达性 ================"
FAIL=0
for p in / /github_trending/ /market_flow/ /static/style.css /static/app.js \
         /manifest.json /api/status /api/modules /api/app /app; do
  printf '%-22s ' "$p"
  out="$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" --max-time 15 "$BASE$p")"
  size="$(curl -s -o /dev/null -w '%{size_download}' "${AUTH[@]}" --max-time 15 "$BASE$p")"
  echo "HTTP $out  ${size}B"
  [ "$out" = "200" ] || FAIL=$((FAIL + 1))
done

# /api/app 是"App 自己发现新版本"的唯一依据 —— 它答错了，用户就永远停在旧版。
# 所以不只查可达，还要查**它答的版本号是不是真的等于线上那个 APK 的版本号**。
echo
echo "---- 安卓壳的版本自洽性（/api/app vs 实际 APK）----"
APPJSON="$(curl -s "${AUTH[@]}" --max-time 15 "$BASE/api/app")"
SAY_CODE="$(printf '%s' "$APPJSON" | sed -nE 's/.*"versionCode":([0-9]+).*/\1/p')"
SAY_NAME="$(printf '%s' "$APPJSON" | sed -nE 's/.*"versionName":"([^"]*)".*/\1/p')"
SAY_SHA="$(printf '%s' "$APPJSON" | sed -nE 's/.*"sha256":"([^"]*)".*/\1/p')"
if [ -z "$SAY_CODE" ]; then
  echo "  /api/app 没报出 versionCode  ✗"
  FAIL=$((FAIL + 1))
else
  echo "  /api/app 自报：versionCode=$SAY_CODE versionName=$SAY_NAME"
  # 真实 APK 的 sha256（直接下下来算）
  REAL_SHA="$(curl -s "${AUTH[@]}" --max-time 60 "$BASE/app" | sha256sum | cut -d' ' -f1)"
  if [ -n "$SAY_SHA" ] && [ "$SAY_SHA" = "$REAL_SHA" ]; then
    echo "  接口声明的 sha256 与实际下载一致 ✓"
  else
    echo "  ✗ 接口声明的 sha256(${SAY_SHA:0:16}…) 与实际下载(${REAL_SHA:0:16}…) 不一致"
    echo "     → 多半是 sidecar JSON 没跟着 APK 一起传（两者必须成对）"
    FAIL=$((FAIL + 1))
  fi
fi

# ⚠️ 光校验"接口与所发的那个文件自洽"是**不够的**：自洽是"自己跟自己一致"，
#    版本静默倒退时它照样通过。实测过：把最新包的 sidecar 删掉，
#    `_latest_apk()` 会静静回退到**上一个有 sidecar 的包**（1.3 → 1.2），
#    而上面的 sha256 比对完全正常（因为发的就是 1.2、报的也是 1.2 的元数据）。
#    → 必须再比一次"磁盘上按 mtime 最新的包" vs "接口实际在发的包"。
SERVED="$(curl -s -D - -o /dev/null "${AUTH[@]}" --max-time 30 "$BASE/app" \
          | sed -nE 's/.*filename=([^;[:space:]]+).*/\1/p' | tr -d '\r')"
NEWEST="$(ls -t "$APP_DIR"/apk/*.apk 2>/dev/null | head -1 | xargs -r basename)"
echo "  磁盘最新包：${NEWEST:-（无）}   接口在发：${SERVED:-（无）}"
if [ -z "$NEWEST" ]; then
  echo "  ✗ apk 目录里没有安装包"
  FAIL=$((FAIL + 1))
elif [ "$NEWEST" != "$SERVED" ]; then
  echo "  ✗ 两者不一致 —— 线上版本**静默倒退**了！"
  echo "     多半是 $NEWEST 的 sidecar JSON 丢了（缺 sidecar 的包会被跳过）"
  echo "     → 把 ${NEWEST%.apk}.json 一起传上去即可"
  FAIL=$((FAIL + 1))
else
  echo "  接口发的是磁盘上最新那个 ✓"
fi

echo
echo "================ 3. TLS / HTTPS（IP 证书只有 160 小时，必须盯着）================"
# 证书 6.7 天就过期，续期一旦失灵，浏览器会直接报不安全 —— 而且到时候
# Service Worker 会失去安全上下文，PWA 的离线能力跟着一起没。所以要主动查。
if ! systemctl is-active nginx >/dev/null 2>&1; then
  echo "nginx.service       未运行！HTTPS 入口已断"
  FAIL=$((FAIL + 1))
else
  echo "nginx.service       active"
  CERT="/etc/letsencrypt/live/${RADAR_IP:-118.196.100.121}/fullchain.pem"
  if [ -f "$CERT" ]; then
    EXP="$(openssl x509 -enddate -noout -in "$CERT" | cut -d= -f2)"
    SECS=$(( $(date -d "$EXP" +%s) - $(date +%s) ))
    DAYS=$(( SECS / 86400 ))
    echo "证书到期            $EXP（剩 ${DAYS} 天）"
    if [ "$SECS" -lt 172800 ]; then
      echo "→ ⚠️ 剩余不足 2 天，续期可能失灵，立刻手动续："
      echo "   /opt/certbot/bin/certbot renew --deploy-hook 'systemctl reload nginx'"
      FAIL=$((FAIL + 1))
    else
      echo "→ 有效期正常 ✓"
    fi
    # 真实校验证书链（不加 -k）：验证失败 = 用户手机会看到证书警告
    # 带上口令，否则 401 会掩盖"其实连通且证书正常"这个事实
    TLS="$(curl -s -o /dev/null -m 15 -w '%{http_code} %{ssl_verify_result}' \
           "${AUTH[@]}" "https://${RADAR_IP:-118.196.100.121}/api/status" 2>/dev/null)"
    echo "HTTPS 校验          HTTP ${TLS%% *} / verify=${TLS##* }（0=受信）"
    [ "${TLS##* }" = "0" ] || { echo "→ 证书链不受信 ✗"; FAIL=$((FAIL + 1)); }
  else
    echo "找不到证书 $CERT"
    FAIL=$((FAIL + 1))
  fi
  systemctl list-timers radar-cert-renew.timer --no-pager 2>/dev/null | head -2 | tail -1
fi

echo
echo "================ 4. 数据指纹 ================"
BEFORE="$(fp)"
echo "服务端指纹      $BEFORE"
# 页面里烘的指纹（拿首页比对）
PAGE_FP="$(curl -s "${AUTH[@]}" --max-time 10 "$BASE/" \
           | grep -o 'data-fingerprint="[^"]*"' | head -1 | cut -d'"' -f2)"
echo "首页烘入指纹    $PAGE_FP"
if [ -n "$BEFORE" ] && [ "$BEFORE" = "$PAGE_FP" ]; then
  echo "→ 一致 ✓  手机端「指纹变了 = 有新数据」成立"
else
  echo "→ 不一致 ✗  需要重建站点：$PY -m radar build"
  FAIL=$((FAIL + 1))
fi

echo
echo "================ 5. 数据概况 ================"
"$PY" -m radar status

if [ "${1:-}" = "--refresh" ]; then
  echo
  echo "================ 6. 更新口子（触发一次采集）================"
  CODE="$(curl -s -o /tmp/radar_refresh.json -w '%{http_code}' -X POST \
          "${AUTH[@]}" --max-time 15 "$BASE/api/refresh")"
  echo "POST /api/refresh → HTTP $CODE  $(cat /tmp/radar_refresh.json)"
  sleep 3
  echo "3 秒后 busy = $(curl -s "${AUTH[@]}" --max-time 10 "$BASE/api/status" \
        | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["busy"])')"
  echo "等待采集完成（最多 300s）…"
  for _ in $(seq 1 100); do
    B="$(curl -s "${AUTH[@]}" --max-time 10 "$BASE/api/status" \
         | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["busy"])')"
    [ "$B" = "False" ] && break
    sleep 3
  done
  AFTER="$(fp)"
  echo "刷新前指纹      $BEFORE"
  echo "刷新后指纹      $AFTER"
  [ -n "$AFTER" ] && [ "$BEFORE" != "$AFTER" ] \
    && echo "→ 指纹已变化 ✓  手机端能自动察觉并拉取新数据" \
    || { echo "→ 指纹未变化 ✗  检查 /api/status 或 store.fingerprint"; FAIL=$((FAIL + 1)); }
  echo
  "$PY" -m radar status | head -8
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "⚠️  有 $FAIL 项异常。日志：journalctl -u radar -n 50 --no-pager"
  exit 1
fi
echo "检查完毕，全部正常。日志：journalctl -u radar -n 50 --no-pager"
