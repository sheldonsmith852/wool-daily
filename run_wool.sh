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

"$PY" pipeline.py >> wool_log.txt 2>&1
