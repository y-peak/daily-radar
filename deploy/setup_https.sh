#!/usr/bin/env bash
# 给 daily-radar 套上 HTTPS —— 用 Let's Encrypt 的 **IP 地址证书**。
#
# 为什么走这条路（对比过其他方案）：
#   * 需要 HTTPS 的原因：PWA 的 Service Worker 只在安全上下文可注册，
#     安卓 Chrome 只认 HTTPS 或 localhost，裸 http://IP 不算 —— 这是"装不上 app"的唯一剩余原因。
#   * Cloudflare quick tunnel：能用，但域名每次重启都变 → 已装的 app 图标会失效
#   * serveo 固定子域名：要注册 SSH key（需人工登录），且匿名额度已满
#   * **LE IP 证书（本方案）**：IP 是固定的 → URL 永远不变、手机不用装任何东西、
#     不用买域名也不用备案。LE 自 2026-01-15 起 GA 支持 IP 证书。
#   * 代价：IP 证书**强制 shortlived profile，有效期 160 小时（约 6.7 天）** →
#     必须自动续期，否则一周后浏览器就报证书错误。
#
# 架构：nginx 占 80/443 做 TLS 终结 → 反代到 radar（127.0.0.1:18090，只监听本机）
#      80 同时承担 ACME http-01 挑战（IP 证书只能用 http-01 / tls-alpn-01，不能用 DNS-01）
#
#   bash deploy/setup_https.sh
set -euo pipefail

IP="${RADAR_IP:-118.196.100.121}"
APP_DIR=/root/daily-radar
CERTBOT=/opt/certbot/bin/certbot
WEBROOT=/var/www/certbot

echo "==================================================="
echo " 为 $IP 配置 HTTPS（Let's Encrypt IP 证书）"
echo "==================================================="

# ---------------------------------------------------------------- 1. 装 nginx
echo
echo "==> 1. 安装 nginx"
if ! command -v nginx >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq nginx
fi
nginx -v 2>&1

# ------------------------------------------------------- 2. radar 挪到 18090
echo
echo "==> 2. radar 让出 80，改为只监听 127.0.0.1:18090"
cp -n "$APP_DIR/.env" "$APP_DIR/.env.bak-prehttps" 2>/dev/null || true
sed -i 's/^RADAR_PORT=.*/RADAR_PORT=18090/' "$APP_DIR/.env"
grep -q '^RADAR_HOST=' "$APP_DIR/.env" \
  && sed -i 's/^RADAR_HOST=.*/RADAR_HOST=127.0.0.1/' "$APP_DIR/.env" \
  || echo 'RADAR_HOST=127.0.0.1' >> "$APP_DIR/.env"
# 只监听本机：外网必须走 nginx，否则能绕过 TLS 直接打 http
grep -E '^RADAR_(PORT|HOST)=' "$APP_DIR/.env"

mkdir -p "$WEBROOT/.well-known/acme-challenge"
chmod -R 755 "$WEBROOT"

# ⚠️ 必须**先**重启 radar 把 80 让出来，再启动 nginx。
#    反过来 nginx 会因 "port 80 already in use" 起不来（第一次就是这么挂的）。
systemctl restart radar
sleep 2
echo "    radar: $(systemctl is-active radar)（监听 $(grep -oP '^RADAR_HOST=\K.*' "$APP_DIR/.env"):$(grep -oP '^RADAR_PORT=\K.*' "$APP_DIR/.env")）"

# ------------------------------- 3. 先写 80-only 配置（拿到证书前 nginx 必须能起来）
echo
echo "==> 3. 写入 nginx 配置（先 80，只代理 + 提供 ACME 挑战）"
cat > /etc/nginx/sites-available/daily-radar <<'NGINX'
# 个人情报台 —— 80 端口：ACME 挑战 + 反代
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    # certbot webroot 挑战目录。**必须匿名可访问**，不能加任何鉴权，
    # 否则 LE 校验失败。radar 的口令拦截在它后面，不冲突。
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
        try_files $uri =404;
    }

    location / {
        proxy_pass http://127.0.0.1:18090;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
NGINX

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/daily-radar /etc/nginx/sites-enabled/daily-radar
nginx -t
systemctl enable nginx
systemctl restart nginx
sleep 2
echo "    radar: $(systemctl is-active radar) / nginx: $(systemctl is-active nginx)"

# ------------------------------------------------- 4. 装 certbot（要 5.4+ 才支持 IP）
echo
echo "==> 4. 安装 certbot（要 5.4+ 才支持 --ip-address；Ubuntu 自带的太旧）"
# ⚠️ 服务器 pip 默认走火山内网镜像 mirrors.ivolces.com，**镜像有滞后**：
#    它上面 certbot 最新只到 5.2.2，而没有 --ip-address。
#    所以这里必须显式指定公网 PyPI。
if ! "$CERTBOT" --version 2>/dev/null | grep -qE '5\.([4-9]|[1-9][0-9])'; then
  [ -x /opt/certbot/bin/pip ] || python3 -m venv /opt/certbot
  /opt/certbot/bin/pip install -q --upgrade pip
  /opt/certbot/bin/pip install -q -U --index-url https://pypi.org/simple/ "certbot>=5.4"
fi
"$CERTBOT" --version

# ------------------------------------------------------------ 5. 签发 IP 证书
echo
echo "==> 5. 申请 IP 证书（shortlived profile，160 小时有效）"
echo "    校验方式 webroot → http://$IP/.well-known/acme-challenge/..."
"$CERTBOT" certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path "$WEBROOT" \
  --ip-address "$IP" \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --deploy-hook "systemctl reload nginx" \
  --cert-name "$IP"

# --------------------------------------------------- 6. 补上 443（证书就位后）
echo
echo "==> 6. 写入 HTTPS 配置"
cat > /etc/nginx/sites-available/daily-radar <<NGINX
# 个人情报台 —— 80 端口：ACME 挑战 + 反代
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
        try_files \$uri =404;
    }

    location / {
        proxy_pass http://127.0.0.1:18090;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}

# 个人情报台 —— 443 正式入口
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;

    ssl_certificate     /etc/letsencrypt/live/$IP/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$IP/privkey.pem;
    # 证书只有 160 小时，靠自动续期兜；这里用 TLS1.2+ 保持兼容
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1h;

    # ⚠️ 故意**不开 HSTS**：证书 6.7 天到期，万一自动续期挂了，
    #    有 HSTS 的话浏览器连"继续访问"的逃生口都不给。保持可控。

    location / {
        proxy_pass http://127.0.0.1:18090;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGINX

nginx -t
systemctl reload nginx

# ------------------------------------------------------------ 7. 自动续期
echo
echo "==> 7. 配置自动续期 timer"
cat > /etc/systemd/system/radar-cert-renew.service <<'UNIT'
[Unit]
Description=续期 个人情报台 的 Let's Encrypt IP 证书
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart=/opt/certbot/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
UNIT

cat > /etc/systemd/system/radar-cert-renew.timer <<'UNIT'
[Unit]
Description=每天两次检查 IP 证书续期

[Timer]
# 160 小时的证书，官方建议剩余 1/3 时续（约剩 2.2 天）。
# 一天跑两次，留足重试余量；RandomizedDelaySec 避免整点打 LE。
OnCalendar=*-*-* 03,15:17:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now radar-cert-renew.timer

echo
echo "==================================================="
echo " 完成"
echo "   HTTPS 入口：https://$IP/?t=<口令>"
echo "   证书到期：  $(openssl x509 -enddate -noout -in /etc/letsencrypt/live/$IP/fullchain.pem 2>/dev/null | cut -d= -f2)"
echo "   续期检查：  systemctl list-timers radar-cert-renew.timer --no-pager"
echo "   手动续期：  /opt/certbot/bin/certbot renew --deploy-hook 'systemctl reload nginx'"
echo "   nginx 配置：/etc/nginx/sites-available/daily-radar"
echo "==================================================="
