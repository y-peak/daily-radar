#!/usr/bin/env bash
#
# 服务器侧一键部署 —— 把 /root/daily-radar 同步到 GitHub 上的最新 main。
#
# 为什么要有这个脚本：
#   以前从本地用 `volcano put` 手工撒文件 —— 漏传不报错（坑 4）、
#   "线上跑的是哪一版"说不清、也没法回溯。改成"**服务器是仓库的工作副本**"之后：
#     · 线上状态 == 某次 commit，可复现、可回退
#     · 同步是一条命令，而不是一份要记在脑子里的文件清单
#
# 它**只碰 git 跟踪的文件**。运行时资产（.env / .venv / data / public / apk）
# 全在 .gitignore 里，`reset --hard` 不会动它们 —— 这是这个方案成立的前提。
# 所以：**新增需要保留的运行时文件时，务必同步更新 .gitignore**。
#
# 用法（在项目根目录或任意位置）：
#   bash deploy/pull.sh                 # 完整流程：同步 → 依赖 → 建站 → 重启
#   bash deploy/pull.sh --check         # 只看会变成哪个 commit，不落地
#   bash deploy/pull.sh --no-build      # 只同步代码，不重建站点
#   bash deploy/pull.sh --no-restart    # 不重启服务
#
# ⚠️ **首次**部署要下 17MB 的 pack，经国内链路可能耗掉一分钟以上。
#    挂在 ssh/工具调用的超时里会"输出丢了但远端还在跑"（实测踩过）。
#    首跑建议放 tmux 里，输出落日志：
#      tmux new-session -d -s deploy 'bash deploy/pull.sh >/tmp/deploy.log 2>&1; echo EXIT=$? >>/tmp/deploy.log'
#      tail -f /tmp/deploy.log
#
set -euo pipefail

ROOT="${RADAR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

# ------------------------------------------------------------------ 0. 自检
# 必须先确认"我站在项目根目录上"。这个脚本会执行 `git init` 和
# `git reset --hard` —— **一个跑错目录的部署脚本比没有部署脚本危险得多**。
#
# 本次就真踩了：把脚本上传到 `/root/_pull_bootstrap.sh` 再执行，
# `dirname/..` 算出来是 `/`，于是它在**文件系统根目录**上 `git init`、
# 配了对 daily-radar 的 remote，接着才因为 fetch 卡住（见下方 SSH 说明）被超时杀掉
# —— 万幸 `reset --hard` 还没轮到执行，`/` 下一个文件都没动。
# 只要那次 fetch 成功，`reset --hard` 就会**对着 `/` 展开一个仓库**。
# 所以这个自检不是锦上添花，是必需品。
if [ ! -f config.json ] || [ ! -d radar ]; then
  echo "✗ 这里不像 daily-radar 的项目根: $ROOT" >&2
  echo "  期望同时看到 config.json 与 radar/ 目录。" >&2
  echo "  用 RADAR_ROOT=/root/daily-radar 显式指定，或把脚本放回 deploy/ 下再跑。" >&2
  exit 2
fi

REMOTE="${RADAR_REMOTE:-git@github.com:y-peak/daily-radar.git}"
BRANCH="${RADAR_BRANCH:-main}"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

DO_BUILD=1
DO_RESTART=1
DO_CHECK=0
for arg in "$@"; do
  case "$arg" in
    --no-build)   DO_BUILD=0 ;;
    --no-restart) DO_RESTART=0 ;;
    --check)      DO_CHECK=1 ;;
    -h|--help)    sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "未知参数：$arg（--help 看用法）" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s ==\n' "$*"; }

# ⚠️⚠️ 必须显式配成**非交互**的 SSH，否则这个脚本会永远卡住。
#
#    服务器的 `~/.ssh/known_hosts` 里**没有 github.com 条目**（它只被 `ssh -T` 之
#    类手工命过，而那次往往带了 `StrictHostKeyChecking=no`，不落 known_hosts）。
#    于是默认的 `git fetch` 会停在
#        The authenticity of host 'github.com (...)' can't be established.
#        Are you sure you want to continue connecting (yes/no/[fingerprint])?
#    上等人敲 y —— 从调用方看就是**没有输出、不返回、最后被超时杀掉**（本次实测：
#    SIGTERM 收场，且因为脚本 `set -e` 直接中断，后面什么都没执行）。
#
#    accept-new：首次自动信任并**记进 known_hosts**，之后密钥变了会报错
#    （比 `no` 安全 —— `no` 每次都不校验，中间人换了密钥也不会响）。
#    BatchMode=yes：任何需要交互（密码 / passphrase / 主机指纹）一律直接失败，
#    绝不挂起。这对"将来放进 systemd timer 自动跑"是硬要求。
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=15}"

# ------------------------------------------------------------------ 1. 自举
# 目录还不是仓库时（首次部署）自己把它变成仓库 —— 这样脚本可以独立使用，
# 不需要先手工 init，也不会因为"忘了先 init"而静默什么都不做。
say "仓库状态"
if [ ! -d .git ]; then
  echo "还不是 git 仓库 → 自举初始化（branch=$BRANCH）"
  git init -q
  git symbolic-ref HEAD "refs/heads/$BRANCH"
fi
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE"
else
  git remote add origin "$REMOTE"
fi
echo "remote = $(git remote get-url origin)"

# ------------------------------------------------------------------ 2. 取最新
say "拉取 origin/$BRANCH"
git fetch -q --prune origin "$BRANCH"
NEW="$(git rev-parse FETCH_HEAD)"
OLD="$(git rev-parse --verify -q HEAD 2>/dev/null || true)"

if [ "$OLD" = "$NEW" ]; then
  echo "已经是最新：$(git log -1 --format='%h %s' "$NEW")"
  [ "$DO_CHECK" = 1 ] && exit 0
else
  if [ -z "$OLD" ]; then
    echo "首次落地 → $NEW"
  else
    echo "将更新："
    git --no-pager log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/  /'
  fi
  if [ "$DO_CHECK" = 1 ]; then
    echo "（--check：只看不落地）"
    exit 0
  fi
fi

# 更新前先记下旧依赖清单，用来判断"这次要不要 pip install"
OLD_REQ=""
if [ -n "$OLD" ]; then
  OLD_REQ="$(git show "$OLD:deploy/requirements.txt" 2>/dev/null || true)"
fi

# ------------------------------------------------------------------ 3. 落地
# ⚠️ `reset --hard` 会覆盖工作区里**被跟踪**的文件，但对 untracked / ignored
#    的 .env、data/、public/、apk/、.venv/ 一概不碰。这正是我们要的语义：
#    "代码以仓库为准，数据以机器为准"。
say "同步工作区"
git reset -q --hard "$NEW"
echo "HEAD = $(git rev-parse --short HEAD)  $(git log -1 --format=%s)"

if [ "$DO_BUILD" = 1 ]; then
  # ---------------------------------------------------------------- 4. 依赖
  NEW_REQ="$(cat deploy/requirements.txt 2>/dev/null || true)"
  if [ "$OLD_REQ" != "$NEW_REQ" ]; then
    say "依赖清单有变化 → pip install"
    "$PY" -m pip install -q -r deploy/requirements.txt \
      || "$PY" -m pip install -q -i https://pypi.org/simple/ -r deploy/requirements.txt
    echo "依赖已安装（$PY）"
  else
    say "依赖无变化，跳过 pip"
  fi

  # ---------------------------------------------------------------- 5. 建站
  # **必做**：`/static/<rel>` 优先发 public/ 下的预构建副本，
  # 只同步源码而不 rebuild，线上会继续发旧副本 —— 表现为"代码全对但页面没变"（坑 7）。
  say "重建站点"
  "$PY" -m radar build
fi

# ------------------------------------------------------------------ 6. 重启
# 静态页面改动其实不需要重启（web 只发文件），但路由/核心代码改动需要。
# 不区分、一律重启：这个服务重启不到一秒，少一个判断就少一类"改了没生效"。
if [ "$DO_RESTART" = 1 ]; then
  say "重启 radar.service"
  systemctl restart radar.service
  sleep 1
  echo "服务状态：$(systemctl is-active radar.service)"
fi

say "完成"
echo "HEAD = $(git rev-parse --short HEAD)  $(git log -1 --format='%h %s')"
# 打印工作区状态：**非空就要有人来看一眼** —— 正常情况下服务器是忠实的仓库副本，
# 除了 ignored 的运行时资产之外不该有差异。有差异往往意味着有人直接在服务器上改过。
UNTRACKED_DIRTY="$(git status --porcelain --untracked-files=no | head -20)"
if [ -n "$UNTRACKED_DIRTY" ]; then
  echo "⚠️ 工作区有未提交改动（服务器上被直接改过？）："
  echo "$UNTRACKED_DIRTY"
else
  echo "工作区干净（与仓库一致）"
fi
