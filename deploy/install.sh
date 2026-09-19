#!/usr/bin/env bash
# 一键部署：在服务器上把 daily-radar 装成 systemd 服务。
#
#   bash deploy/install.sh
#
# 幂等：重复执行只会更新代码与重启服务，不会重复造 venv。
# 前提：把项目放在 $APP_DIR（默认 /root/daily-radar），脚本自身位于其下。
set -euo pipefail

APP_DIR="${APP_DIR:-/root/daily-radar}"
PY="${PYTHON:-python3}"
VENV="$APP_DIR/.venv"

echo "==> 应用目录：$APP_DIR"
cd "$APP_DIR"

# ------------------------------------------------------------------ 1. venv
if [ ! -d "$VENV" ]; then
  echo "==> 创建虚拟环境"
  if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    echo "    venv 不可用，尝试安装 python3-venv"
    apt-get update -qq
    apt-get install -y -qq python3-venv
    "$PY" -m venv "$VENV"
  fi
fi

echo "==> 安装依赖（deploy/requirements.txt）"
"$VENV/bin/pip" install -q --upgrade pip
# 依赖清单只有一份，放 deploy/requirements.txt —— 免得"装了什么"散落在脚本里，
# 加一个模块却忘了改这里，线上就静默降级。
#
# ⚠️ 先走内网源（快）；内网源对更新的包有滞后（latex2mathml 只到 3.78.1，
#    而 requirements 钉的是 3.81.1），失败就整体改走 PyPI 重来一次。
if ! "$VENV/bin/pip" install -q -r "$APP_DIR/deploy/requirements.txt"; then
  echo "    内网源不满足（多半是版本滞后），改走 PyPI"
  "$VENV/bin/pip" install -q -i https://pypi.org/simple/ \
    -r "$APP_DIR/deploy/requirements.txt" \
    || echo "    [warn] 依赖未装全 → 读书模块的公式会退回纯文本显示"
fi

# --------------------------------------------------------------- 2. 首轮采集
echo "==> 首次采集（失败不阻塞部署，稍后可用网页上的 ↻ 重试）"
if "$VENV/bin/python" -m radar list; then
  "$VENV/bin/python" -m radar run all || echo "    [warn] 采集有模块失败，继续装服务"
fi

# ---------------------------------------------------------------- 3. systemd
echo "==> 安装 systemd 单元"
install -m 644 deploy/radar.service        /etc/systemd/system/radar.service
install -m 644 deploy/radar-collect.service /etc/systemd/system/radar-collect.service
install -m 644 deploy/radar.timer          /etc/systemd/system/radar.timer
systemctl daemon-reload

systemctl enable --now radar.service
systemctl enable --now radar.timer

# ------------------------------------------------------------------ 4. 配置
# .env 不入库（.gitignore 里有），所以新机器上要先生成一份。
# 两个服务（web 与定时采集）都靠 EnvironmentFile 读它 —— 少了它不会报错，
# 只会安静降级：口令失效、翻译退回免 key 接口、中文介绍整段不生成。
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "==> 已从 .env.example 生成 .env"
fi
if ! grep -qE '^RADAR_LLM_KEY=.+' "$APP_DIR/.env"; then
  echo "    [info] .env 里没有 RADAR_LLM_KEY"
  echo "           → 翻译走免 key 接口、不生成中文介绍（功能正常，只是没有大模型那段）"
  echo "           → 要开就在 .env 里加一行 RADAR_LLM_KEY=sk-...，然后 systemctl start radar-collect"
fi

# ------------------------------------------------------------------ 5. 收尾
PORT="$(grep -oP '"port"\s*:\s*\K[0-9]+' config.json | head -1 || echo 18090)"
IP="$(hostname -I | awk '{print $1}')"
# 已经配过 HTTPS 的话（存在 certbot 证书），radar 实际是被 nginx 反代的，
# 对外入口是 443 而不是 config.json 里的 18090。
if [ -f /etc/letsencrypt/live/118.196.100.121/fullchain.pem ]; then
  ACCESS="https://118.196.100.121/  （nginx 反代 → 127.0.0.1:18090）"
else
  ACCESS="http://$IP:$PORT/   （还没配 HTTPS，跑 bash deploy/setup_https.sh 装上）"
fi
echo
echo "==> 完成"
echo "    网页：   $ACCESS"
echo "    状态：   systemctl status radar --no-pager"
echo "    日志：   journalctl -u radar -f        （采集日志跟服务同一个流）"
echo "    定时：   systemctl list-timers radar.timer"
echo "    手动跑： $VENV/bin/python -m radar run all"
echo "    体检：   bash deploy/healthcheck.sh [--refresh]"
if [ ! -f /etc/letsencrypt/live/118.196.100.121/fullchain.pem ]; then
  echo
  echo "    ⚠️ 还没配 HTTPS → PWA 装不成（Service Worker 需要安全上下文）"
  echo "       下一步：bash deploy/setup_https.sh"
fi
