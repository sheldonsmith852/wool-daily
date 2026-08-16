# 深圳薅羊毛日报 · Linux 部署指南（WorkBuddy Lighthouse 赠送实例）

> 适用场景：把本仓库部署到腾讯云轻量应用服务器（Ubuntu 22.04 LTS，2 核 2G 4M），
> 用 cron 每日定时跑，常驻云算力（本地 Windows 关机也不中断）。

## 结论先行：代码无需改业务逻辑

`pipeline.py` 全程用 `os.path.dirname(os.path.abspath(__file__))` 计算路径，
**没有硬编码 `C:\` 路径**；外部调用（小红书脚本）用 `sys.executable`，跨平台通用。
所有 `.py` 文件已是 LF 行尾，Linux 不会报 `^M` 错误。
**真正的运行依赖只有两个：`requests` + `beautifulsoup4`（不需要 PyMuPDF / pandas）。**

## 一、服务器环境准备

```bash
# 系统 Python（Ubuntu 22.04 默认 3.10，代码兼容，无需升级）
sudo apt update
sudo apt install -y python3-venv python3-pip

# 中文网页/报告需要中文字体（部分源标题含中文，渲染/归档用）
sudo apt install -y fonts-wqy-zenhei fonts-wqy-microhei
```

## 二、部署代码

```bash
# 方式 A（推荐）：从私有 git 仓克隆
git clone <你的私有仓库地址> ~/wool
cd ~/wool

# 方式 B：手动 scp 上传后解压到 ~/wool
```

## 三、建虚拟环境 + 装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 四、配置密钥（.env，务必不提交）

把本地 `D:\...\wool\.env` 的内容写到服务器（含两行）：

```bash
cat > ~/wool/.env <<'EOF'
# PushPlus 个人微信推送 token（https://www.pushplus.plus）
PUSHPLUS_TOKEN=你的token
REDFOX_API_KEY=你的红狐key
EOF
chmod 600 ~/wool/.env
```

> 这两个值不要写进 `config.json`（已留空），也不要提交到 git（已被 .gitignore 排除）。

## 五、先干跑一次（验证，不推送）

```bash
cd ~/wool
WOOL_DRYRUN=1 bash run_wool.sh
# 看 wool_log.txt 末尾是否打印 OK total=... ；看 wool_report.md 是否生成
```

## 六、挂 cron 定时

```bash
crontab -e
# 每天 08:00 跑（时区默认 UTC，注意 Cloud 实例时区；如需北京时间自行换算）
0 8 * * * cd /home/ubuntu/wool && /home/ubuntu/wool/run_wool.sh
```

## 七、防火墙（Lighthouse 控制台 / MCP）

- 默认只开 **22 (SSH)**。
- 日报是本地生成 + PushPlus 推微信，**不需要对外开 web 端口**。
- 若日后要对外分发网页，单独开 80/443 并仅暴露静态文件目录，切勿把任何管理端口放公网。

## ⚠️ 已知限制：小红书信源在裸服务器上会被跳过

`pipeline.py` 里的小红书抓取依赖一个 **WorkBuddy 本地 skill 脚本**
（`~/.workbuddy/skills/xiaohongshu-search/scripts/fetch_xhs_hot_articles.py`，走红狐 `redfox.hk`）。

裸 Linux 服务器没有 WorkBuddy 桌面端 → 该脚本不存在 → 代码会**优雅跳过**并打印
`XHS_SKIP 脚本缺失`，其余 6 个源（55信用卡 / 羊毛村 / 什么值得买 / 工行 / 本地宝 / 联盟）照常运行。

若要在服务器也跑小红书，需要：① 把该 skill 脚本随项目一起部署；
② 确保 `REDFOX_API_KEY` 已配；③ 该脚本可能依赖 WorkBuddy skill 内部环境，需单独验证可独立运行。
（建议先上线其余 6 源，小红书作为后续增强。）

## 首次运行提示

`wool_state.json`（去重历史）默认不提交，云上首次跑会把大量在售优惠标记为 🆕，
属正常现象，之后逐日去重。若要延续本地去重状态，可单独把 `wool_state.json` 传上去（不要提交进 git）。
