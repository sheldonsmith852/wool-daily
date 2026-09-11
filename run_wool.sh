#!/usr/bin/env bash
# 深圳薅羊毛日报 - Linux 启动器
# 对应 Windows 的 run_wool.bat（那个 .bat 因硬编码 D:\ 路径和 Windows python，上云不能用）。
# 用法：
#   bash run_wool.sh
#   # 或加入 crontab（见 DEPLOY.md）
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# 先拉取最新代码（公开仓库，HTTPS 匿名 pull 即可），确保 cron 跑的是 GitHub 上的最新版，
# 避免「本地改了代码、服务器却一直在跑旧版」的部署脱节（历史 bug：推送 8b14a8d 后服务器
# 一直停在手动同步的 e1485f5，导致买一送一闸门从未上线）。
#
# 失败必须留痕，不能静默：实测服务器到 GitHub 的 HTTPS 会偶发 TLS 握手失败
# （GnuTLS recv error -110），此时若用 `|| true` 吞掉，就会「以为在跑最新版、
# 实际在跑旧版」——正是上面那个部署脱节 bug 的复发路径。故失败时把当前 HEAD
# 版本写进日志，排查时能立刻看出当天跑的是哪个版本。
if ! git pull --ff-only --quiet 2>>wool_log.txt; then
  echo "[$(date '+%F %T')] WARN git pull 失败，本次跑的是本地旧代码 $(git rev-parse --short HEAD 2>/dev/null)" >> wool_log.txt
fi
echo "[$(date '+%F %T')] HEAD=$(git rev-parse --short HEAD 2>/dev/null)" >> wool_log.txt

# 优先用项目内 venv，否则回退系统 python3
if [ -x ./venv/bin/python ]; then
  PY=./venv/bin/python
else
  PY=python3
fi

# 若装了 Xvfb（新服务器），起虚显并设 DISPLAY，让 Playwright 走有界面模式，
# 以绕过 bendibao 等对「无 user_data_dir 的临时浏览器」的反爬挑战；
# 旧服务器无 Xvfb 则跳过，保持原无头逻辑不受影响。
if command -v Xvfb >/dev/null 2>&1; then
  if ! pgrep -x Xvfb >/dev/null 2>&1; then
    nohup Xvfb :1 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
    sleep 2
  fi
  export DISPLAY=:1
fi

"$PY" pipeline.py >> wool_log.txt 2>&1
