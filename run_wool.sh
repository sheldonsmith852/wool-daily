#!/usr/bin/env bash
# 深圳薅羊毛日报 - Linux 启动器
# 对应 Windows 的 run_wool.bat（那个 .bat 因硬编码 D:\ 路径和 Windows python，上云不能用）。
# 用法：
#   bash run_wool.sh
#   # 或加入 crontab（见 DEPLOY.md）
set -uo pipefail
cd "$(dirname "$0")" || exit 1

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
