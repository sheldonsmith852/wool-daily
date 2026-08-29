#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
深圳薅羊毛日报 - 数据管线（混合架构·抓取/解析/分类/过滤/去重/渲染）

设计：
- 抓取层：requests 原始抓取 + BeautifulSoup 解析（确定性、可单测）
- 日期层：从列表页解析每条优惠的发布时间
          · 55信用卡 → 绝对日期「YYYY年M月D日」（在 item-content 容器内）
          · 羊毛村   → 相对时间「X天前/小时前/周前」（在 <li> 容器内），归一化为日期
- 分类层：关键词规则分类器（快/准/可单测），给每条 Deal 打「优惠类型」标签
- 过滤层：黑名单类型（景区/酒店/研学）直接剔除；新鲜度过滤（max_age_days，默认 30）
- 去重：按 来源+标题+链接 哈希，状态持久化在 wool_state.json
- 渲染：按「优惠类型」分组输出「发布日期/距今」列
        · 推送版用 markdown 表格（PushPlus，微信服务通知）
        · 本地 HTML 备份表格；企业微信机器人用列表版（无表格兜底）
- 投递：内置 PushPlus（token 直发个人微信，绕过 agent-mail 确认闸），
        企业微信群机器人 webhook 作为备用通道。

去重语义：日报展示「当前在列的全部优惠」，对新出现的标 🆕。
"""
import os
import sys
import re
import json
import hashlib
import datetime as _dt
import time
import subprocess
import tempfile
from contextlib import contextmanager
from html import escape as _esc

import requests
from bs4 import BeautifulSoup

WORKDIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(WORKDIR, "wool_state.json")
REPORT_PATH = os.path.join(WORKDIR, "wool_report.md")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---- 日期解析 ----
CN_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
REL_DATE = re.compile(r"(\d+)\s*(天|小时|分钟|周|个月|月)前")
ABS_DATE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
MD_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日?")
URL_DATE = re.compile(r"/(20\d{4,5})(?:/|$)")


def esc(s):
    return _esc(str(s), quote=True)


def find_date_near(anchor, pattern):
    """从 anchor 向上找第一个文本含日期的祖先，返回匹配到的原始日期串。"""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        t = node.get_text(" ", strip=True)
        m = pattern.search(t)
        if m:
            return m.group(0)
    return ""


def norm_date(raw, now=None):
    """把原始日期串归一化为 (iso_date, age_days)；解析失败返回 ('', None)。"""
    if not raw:
        return ("", None)
    m = CN_DATE.search(raw)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            dt = _dt.date(y, mo, d)
        except ValueError:
            return ("", None)
        return (dt.isoformat(), (_dt.date.today() - dt).days)
    m = REL_DATE.search(raw)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"分钟": _dt.timedelta(minutes=n),
                "小时": _dt.timedelta(hours=n),
                "天": _dt.timedelta(days=n),
                "周": _dt.timedelta(weeks=n),
                "月": _dt.timedelta(days=n * 30),
                "个月": _dt.timedelta(days=n * 30)}
        delta = mult.get(unit)
        if delta is None:
            return ("", None)
        base = now or _dt.datetime.now()
        dt = (base - delta).date()
        return (dt.isoformat(), (_dt.date.today() - dt).days)
    m = ABS_DATE.search(raw)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            dt = _dt.date(y, mo, d)
        except ValueError:
            return ("", None)
        return (dt.isoformat(), (_dt.date.today() - dt).days)
    m = MD_DATE.search(raw)
    if m:
        try:
            dt = _dt.date(_dt.date.today().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return ("", None)
        return (dt.isoformat(), (_dt.date.today() - dt).days)
    return ("", None)


def norm_date_url(url):
    """从 URL 路径段解析日期：年(4)+月(1-2)+日(1-2) 无零填充，
    如 .../news/2026812/ → 2026-08-12；无法解析返回空串。"""
    m = URL_DATE.search(url or "")
    if not m:
        return ""
    seg = m.group(1)
    rest = seg[4:]
    for mm, dd in [(rest[:1], rest[1:]), (rest[:2], rest[2:])]:
        try:
            cand = f"{seg[:4]}-{int(mm):02d}-{int(dd):02d}"
            _dt.date.fromisoformat(cand)
            return cand
        except (ValueError, IndexError):
            continue
    return ""


def age_label(d):
    iso = d.get("date")
    if not iso:
        return "日期未知"
    try:
        age = (_dt.date.today() - _dt.date.fromisoformat(iso)).days
    except ValueError:
        return "日期未知"
    if age <= 0:
        return "今天"
    if age == 1:
        return "昨天"
    return f"{age}天前"


# ---- 分类体系：按优先级匹配，命中即定类型 ----
TYPE_RULES = [
    ("🏨 酒店住宿", ["希尔顿", "威斯汀", "住宿", "民宿", "房型", "客栈",
                   "宾馆", "度假酒店", "酒店自助", "酒店内"]),
    ("🏞️ 景区门票", ["门票", "景区", "乐园", "博物馆", "展览", "动物园",
                   "海洋世界", "世界之窗", "欢乐谷", "摩天轮", "美术馆",
                   "度假村", "温泉", "影视城", "展馆", "主题公园",
                   "公园", "大观园", "民俗村", "农场", "生态园",
                   "农庄", "田园"]),
    ("🎓 教育研学", ["研学", "独立营", "亲子营", "科技营", "科普课", "DIY",
                   "绘画体验", "手工体验", "体验营", "探索营", "创客"]),
    ("🥤 奶茶饮品", ["奶茶", "咖啡", "茶饮", "喜茶", "瑞幸", "霸王茶姬",
                   "茶百道", "蜜雪", "沪上阿姨", "贡茶", "星巴克",
                   "库迪", "幸运咖", "果茶", "柠檬茶"]),
    ("🛵 外卖红包", ["外卖", "饿了么", "美团"]),
    ("🍜 餐饮美食", ["餐厅", "自助餐", "美食", "套餐", "烧烤", "火锅",
                   "必胜客", "肯德基", "麦当劳", "披萨", "汉堡",
                   "小吃", "料理"]),
    ("💰 支付立减", ["支付宝", "云闪付", "微信", "立减金", "减1.5",
                   "支付", "银行", "信用卡", "储蓄卡", "红包"]),
    ("🛒 电商券", ["淘宝", "京东", "拼多多", "满减", "隐藏券", "电商",
                  "优惠券", "抵扣券", "代金券"]),
    ("🚗 交通出行", ["机票", "打车", "加油", "高铁", "出行", "携程",
                   "航班", "滴滴", "加油卡"]),
]
TYPE_ORDER = ["🥤 奶茶饮品", "🍜 餐饮美食", "🛵 外卖红包", "💰 支付立减",
              "🛒 电商券", "🚗 交通出行", "🎟️ 深圳活动", "📦 其他"]
BLOCKED_TYPES = {"🏨 酒店住宿", "🏞️ 景区门票", "🎓 教育研学"}


def classify(d):
    # 本地宝等活动源自带分类标记，直接采用，避免被通用关键词误路由
    if d.get("_force_type"):
        return d["_force_type"]
    text = (d.get("title", "") + " " + d.get("detail", "")).lower()
    for label, kws in TYPE_RULES:
        for kw in kws:
            if kw.lower() in text:
                return label
    # 电商聚合源（SMZDM/联盟）未命中具体类型时，统一归「电商券」
    if d.get("source") in ("smzdm", "pdd"):
        return "🛒 电商券"
    return "📦 其他"


def _get(url):
    """统一抓取，返回解码后的 HTML 文本；失败返回空串。"""
    import sys
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=25)
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        print("fetch error", url, e, file=sys.stderr)
        return ""


def _attach_date(d, anchor, pattern, now):
    raw = find_date_near(anchor, pattern)
    iso, _ = norm_date(raw, now)
    d["date"] = iso
    d["date_raw"] = raw
    return d


def fetch_55card():
    """抓取 55信用卡（支付宝/云闪付/翼支付/银行立减金），🟡。
    绝对日期「YYYY年M月D日」在 item-content 容器内，随标题链接一起解析。"""
    deals = []
    seen = set()
    cats = ["alipay", "yunshanfu", "bestpay", "chuxuka", "creditcard"]
    pages = ["https://www.55card.cn/"] + [
        f"https://www.55card.cn/category/{c}" for c in cats]
    pat = re.compile(r"/(alipay|chuxuka|creditcard|bestpay|yunshanfu)/\d")
    now = _dt.datetime.now()
    for url in pages:
        html = _get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            title = a.get_text(strip=True)
            if not title or len(title) < 4 or not pat.search(href):
                continue
            if not href.startswith("http"):
                href = "https://www.55card.cn" + href
            if href in seen:
                continue
            seen.add(href)
            d = {
                "platform": "55信用卡",
                "category": "支付立减",
                "city": "",
                "title": title,
                "detail": "",
                "url": href,
                "confidence": "🟡",
                "source": "55card.cn",
                "date": "",
                "date_raw": "",
            }
            _attach_date(d, a, CN_DATE, now)
            deals.append(d)
    return deals


def fetch_yangmaocun():
    """抓取羊毛村线报（外卖/奶茶/支付/银行立减等），🟡。
    相对日期「X天前/小时前/周前」在 <li> 容器内，归一化为日期。
    采集上限 MAX 防止整站历史线报灌入。"""
    deals = []
    seen = set()
    MAX = 600  # 首页约 500+ 线报链接，放大上限确保奶茶等后置分类不被截断
    now = _dt.datetime.now()
    # 首页 ym2.cc 含全部 /ymxb/ 线报链接；ymnnc.com 为镜像站，作备用
    for url in ("https://ym2.cc/", "https://ymnnc.com/ymxb/"):
        html = _get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            title = a.get_text(strip=True)
            if not title or len(title) < 4:
                continue
            if "/ymxb/" not in href and "ymnnc.com" not in href:
                continue
            if href.startswith("/"):
                href = "https://ym2.cc" + href
            if href in seen:
                continue
            seen.add(href)
            d = {
                "platform": "羊毛村",
                "category": "综合线报",
                "city": "",
                "title": title,
                "detail": "",
                "url": href,
                "confidence": "🟡",
                "source": "ym2.cc",
                "date": "",
                "date_raw": "",
            }
            _attach_date(d, a, REL_DATE, now)
            deals.append(d)
            if len(deals) >= MAX:
                return deals
    return deals


def fetch_smzdm():
    """抓取 SMZDM 官方 JSON 接口（优惠精选聚合），🟢官方通道。
    接口 https://api.smzdm.com/v1/youhui/articles?limit=N 返回 data.rows，
    含标题/价格/精确时间戳(article_unix_date)/商城，比 RSS 更全更准。
    article_unix_date 为秒级时间戳，直接转 iso 日期；limit 可调大扩大覆盖。"""
    deals = []
    seen = set()
    limit = 100
    try:
        r = requests.get(
            f"https://api.smzdm.com/v1/youhui/articles?limit={limit}",
            headers={"User-Agent": UA}, timeout=20)
        r.encoding = r.apparent_encoding
        j = r.json()
    except Exception as e:
        print("smzdm api error", e)
        return deals
    rows = (j.get("data") or {}).get("rows") or []
    for x in rows:
        title = (x.get("article_title") or "").strip()
        link = (x.get("article_url") or "").strip()
        if not title or not link or link in seen:
            continue
        seen.add(link)
        iso = ""
        pub_date = None
        ts = x.get("article_unix_date")
        if ts:
            try:
                pub_date = _dt.datetime.fromtimestamp(int(ts)).date()
                iso = pub_date.isoformat()
            except Exception:
                pass
        # 仅保留今天+昨天发布的好价，过滤长期常青帖，避免该信源"老不变"
        if pub_date is not None and (_dt.date.today() - pub_date).days > 1:
            continue
        price = x.get("article_price") or ""
        detail = f"¥{price}" if price else ""
        deals.append({
            "platform": "什么值得买",
            "category": "综合优惠",
            "city": "",
            "title": title,
            "detail": detail,
            "url": link,
            "confidence": "🟢",
            "source": "smzdm",
            "date": iso,
            "date_raw": x.get("article_format_date", ""),
        })
    return deals


def _pdd_sign(params, secret):
    """拼多多开放平台 MD5 签名：参数按 key 升序拼接 + secret，md5 小写。"""
    import hashlib as _hs
    s = "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
    return _hs.md5(s.encode("utf-8")).hexdigest()


def _fetch_pdd(c):
    """拼多多多多进宝（MD5 签名，最标准的联盟 API）。🟢官方。
    返回当前在售高佣/有券商品，属「快照」源（无发布日期，price 参与判重）。
    推广链接需 pdd.ddk.goods.promotion.url.generate 转链，此处先用 goods_sign 占位。"""
    import time as _t
    deals = []
    params = {
        "type": "pdd.ddk.goods.search",
        "client_id": c["client_id"],
        "timestamp": int(_t.time()),
        "page": 1,
        "page_size": 20,
        "sort_type": 0,  # 0=综合
    }
    params["sign"] = _pdd_sign(params, c["client_secret"])
    try:
        r = requests.post("https://gw-api.pinduoduo.com/api/router",
                          data=params, timeout=20)
        j = r.json()
    except Exception as e:
        print("pdd error", e)
        return deals
    glist = (j.get("goods_search_response") or {}).get("goods_list") or []
    for g in glist:
        price = g.get("min_group_price") or 0
        coupon = g.get("coupon_discount") or 0
        deals.append({
            "platform": "拼多多联盟",
            "category": "电商券",
            "city": "",
            "title": g.get("goods_name", ""),
            "detail": f"券后约¥{price/100:.2f}，券¥{coupon/100:.2f}",
            "url": g.get("goods_sign", ""),
            "confidence": "🟢",
            "source": "pdd",
            "date": "",
            "date_raw": "",
            "price": str(price),
            "mode": "snapshot",
        })
    return deals


def fetch_union():
    """联盟开放平台聚合源（淘宝/京东/拼多多/苏宁/唯品会），🟢官方。
    前置条件：config.json['union'] 里各家需填 appkey/secret/pid 等，
    个人需先去各联盟开放平台注册开发者账号。未配置则跳过并告警。
    联盟返回「当前在售优惠」= 快照，非事件流；price 参与判重，
    价格变化才重新标 🆕，解决与现有 only-new 语义冲突。"""
    cfg = load_config().get("union", {}) or {}
    deals = []
    pdd = cfg.get("pdd") or {}
    if pdd.get("client_id") and pdd.get("client_secret"):
        deals += _fetch_pdd(pdd)
    else:
        print("UNION_SKIP pdd: 未配置 client_id/client_secret")
    for name in ("taobao", "jd", "suning", "vip"):
        if cfg.get(name):
            print(f"UNION_TODO {name}: 框架就绪，待实现调用")
    return deals


# ---- 小红书信源（红狐 REDFOX API，经「小红书爆款笔记查询」skill 脚本调用）----
XHS_SCRIPT = os.path.join(os.path.expanduser("~"),
                          ".workbuddy", "skills", "xiaohongshu-search",
                          "scripts", "fetch_xhs_hot_articles.py")
# 明显无关内容直接剔除（明星应援/代购/招聘/租房/二手/婚恋等）——硬规则，保留代码
XHS_NEG = re.compile(r"(演唱会|应援|代购|招聘|求职|出租|转租|二手|闲鱼|"
                      r"婚恋|相亲|征婚|拼单|粉丝|接机|见面会|求租)")
# 内容路由：先判饮品 → 再判活动 → 否则丢弃（分类逻辑核心，保留代码，杜绝错分）
XHS_TYPE_DRINK = re.compile(r"(冰饮|奶茶|柠檬茶|果茶|咖啡|饮品|买一送一|第二杯|半价|"
                             r"特调|绵绵冰|冰淇淋|雪糕|杨枝甘露|葡萄柚|柠檬水)")
XHS_TYPE_EVENT = re.compile(r"(活动|市集|展览|快闪|派对|嘉年华|免费领|领免费|体验|"
                             r"报名|演出|比赛|摊位|亲子|手工|集市|音乐节|探店|"
                             r"开放|招募|福利|赠|送|打卡)")

# 以下「词表 / 阈值」全部外置到 config.json 的 xiaohongshu 段（改词调参不用碰代码）：
#   keywords       搜索关键词（仅偏置红狐召回方向，最终分类由标题内容路由决定）
#   window_days   抓最近 N 天内的笔记
#   per_kw        每个关键词最多取热度前 N 条
#   topic_neg     主题负向词（美发/穿搭/宠物/旅游等生活噪音）
#   sz_landmarks  深圳地域正向必校验词（城市 + 下辖区域/地标）
XHS_DEFAULTS = {
    "keywords": [
        ("🥤 奶茶饮品", "深圳 冰饮 买一送一"),
        ("🥤 奶茶饮品", "深圳 奶茶 免费"),
        ("🎟️ 深圳活动", "深圳 免费活动"),
        ("🎟️ 深圳活动", "深圳 市集"),
        ("🎟️ 深圳活动", "深圳 快闪店"),
        ("🎟️ 深圳活动", "深圳 展览 免费"),
    ],
    "window_days": 14,
    "per_kw": 20,
    "topic_neg": ["头发", "美发", "烫发", "染发", "剪发", "发型", "植发", "假发", "脱发", "护发",
                  "美甲", "美睫", "纹眉", "纹绣", "医美", "护肤", "化妆", "种草",
                  "穿搭", "ootd", "显瘦", "搭配", "减肥", "瘦身", "健身", "瑜伽",
                  "宠物", "撸猫", "猫", "狗", "孕期",
                  "租房", "买房", "装修", "楼盘", "学区",
                  "旅游", "攻略", "景点", "民宿", "出行"],
    "sz_landmarks": ["深圳", "南山", "福田", "罗湖", "宝安", "龙岗", "龙华", "坪山", "盐田", "光明", "大鹏",
                     "前海", "坂田", "布吉", "西丽", "沙井", "福永", "观澜", "石岩", "公明", "松岗",
                     "蛇口", "车公庙", "华强北", "会展中心", "深圳湾", "欢乐海岸", "海岸城", "万象城",
                     "科技园", "后海", "腾讯"],
}


def get_xhs_cfg():
    """小红书信源配置：config.json 的 xiaohongshu 段覆盖默认值。
    标量（window_days/per_kw）只接受正整数；词表（keywords/topic_neg/sz_landmarks）
    非空则整体替换。无效值回落默认，避免配置文件写错导致整源崩溃。"""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in XHS_DEFAULTS.items()}
    u = (load_config() or {}).get("xiaohongshu", {}) or {}
    for k in ("window_days", "per_kw"):
        v = u.get(k)
        if isinstance(v, int) and v > 0:
            cfg[k] = v
    if u.get("keywords"):
        cfg["keywords"] = [tuple(x) if isinstance(x, (list, tuple)) and len(x) == 2 else x
                           for x in u["keywords"]]
    for k in ("topic_neg", "sz_landmarks"):
        if u.get(k) and isinstance(u[k], list):
            cfg[k] = u[k]
    return cfg


def fetch_xiaohongshu():
    """小红书（红狐 REDFOX API）信源：按关键词抓深圳本地活动/冰饮探店笔记。
    现改用「小红书爆款笔记查询」skill 脚本 fetch_xhs_hot_articles.py（同一 REDFOX 接口，
    但返回按 相关性/热度/时效 综合评分排序的爆款笔记，互动1000+，质量更高）。
    前置：REDFOX_API_KEY 环境变量（已存于 .env）+ skill 脚本存在。"""
    if not os.environ.get("REDFOX_API_KEY"):
        print("XHS_SKIP 未配置 REDFOX_API_KEY")
        return []
    if not os.path.exists(XHS_SCRIPT):
        print("XHS_SKIP 脚本缺失", XHS_SCRIPT)
        return []
    deals = []
    seen_urls = set()
    xc = get_xhs_cfg()
    topic_neg_re = re.compile("(" + "|".join(re.escape(w) for w in xc["topic_neg"]) + ")")
    sz_re = re.compile("(" + "|".join(re.escape(w) for w in xc["sz_landmarks"]) + ")")
    end = _dt.date.today()
    start = end - _dt.timedelta(days=xc["window_days"])
    # 脚本副作用：总会写一个 HTML 报告，重定向到临时目录避免污染项目目录
    out_html = os.path.join(tempfile.gettempdir(), "wool_xhs_report.html")
    for _, kw in xc["keywords"]:
        try:
            r = subprocess.run(
                [sys.executable, XHS_SCRIPT, "--keyword", kw,
                 "--start-date", start.isoformat(),
                 "--end-date", end.isoformat(),
                 "--page-num", "1", "--page-size", "50", "--max-items", "50",
                 "--output-file", out_html],
                capture_output=True, text=True, timeout=90,
                env=os.environ.copy())
        except Exception as e:
            print("XHS_RUN_ERR", kw, e)
            continue
        if r.returncode != 0:
            print("XHS_ERR", kw, (r.stderr or "").strip()[:200])
            continue
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            print("XHS_JSON_ERR", kw, (r.stdout or "")[:200])
            continue
        for a in data.get("items", [])[:xc["per_kw"]]:
            url = a.get("noteLink") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = (a.get("title") or "").strip()
            if len(title) < 5:
                continue
            desc = a.get("desc") or ""
            blob = title + "\n" + desc
            if XHS_NEG.search(blob):
                continue  # 明星应援/代购/招聘等明显无关，剔除
            if topic_neg_re.search(blob):
                continue  # 美发/美妆/穿搭/宠物/房产/旅游等无关主题，剔除
            if not sz_re.search(blob):
                continue  # 未明确提及深圳或下辖区域/地标，视为外地，剔除
            # 按内容路由分类（不再依赖搜索关键词，避免语义召回错分）。
            # 路由同时看标题+描述(blob)，救回「描述有活动/饮品信号但标题没写」的笔记。
            if XHS_TYPE_DRINK.search(blob):
                ftype = "🥤 奶茶饮品"
            elif XHS_TYPE_EVENT.search(blob):
                ftype = "🎟️ 深圳活动"
            else:
                continue  # 标题与描述均无明确饮品/活动信号，丢弃避免错分
            # 日期：createTime 形如 "2026-08-12 18:30:55"，取日期部分
            date_val = ""
            ct = a.get("createTime") or ""
            if ct:
                try:
                    date_val = ct.split(" ")[0]
                    _dt.date.fromisoformat(date_val)
                except (ValueError, AttributeError):
                    date_val = ""
            deals.append({
                "platform": "小红书",
                "category": "小红书活动",
                "city": "深圳",
                "title": title,
                "detail": desc[:120],
                "url": url,
                "confidence": "🟢",
                "source": "xiaohongshu",
                "date": date_val,
                "date_raw": ct,
                "_force_type": ftype,
                "like_count": a.get("likedCount", 0) or 0,
                "interactive_count": a.get("interactiveCount", 0) or 0,
            })
    print(f"XHS_OK 抓取 {len(deals)} 条（{len(seen_urls)} 唯一链接）")
    return deals


# ---- Playwright 复用：共用一个无头 chromium，避免每天重复启动 ----
@contextmanager
def _own_browser():
    """无外部 browser 时自用：启动并在退出时关闭一个 Playwright chromium。"""
    from playwright.sync_api import sync_playwright
    pcm = sync_playwright().start()
    browser = pcm.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
    try:
        yield browser
    finally:
        try:
            browser.close()
        finally:
            pcm.stop()


def _pw_scrape(browser, url, js, wait=2000, timeout=25000):
    """在（已启动的）browser 上开新页、渲染 JS 页、eval 提取；页自行关闭，
    browser 由调用方管理（实现「共用一个浏览器」）。"""
    pg = browser.new_page()
    try:
        pg.goto(url, wait_until="networkidle", timeout=timeout)
        pg.wait_for_timeout(wait)
        return pg.evaluate(js) or []
    finally:
        pg.close()


def fetch_icbc(browser=None):
    """工商银行信用卡「优惠活动」列表（JS 渲染，需 Playwright 无头浏览器）。
    抓列表页，提取活动标题/链接/截止日期，命中💰支付立减为主。
    依赖：playwright + chromium（pip install playwright && playwright install chromium）。
    browser 可传入已启动的 chromium（与 bendibao 共用），None 时自起自用。
    银行优惠多为「常在售」活动，过去起始日统一刷新为今天，保证稳定展示。"""
    import re as _re
    DATE = _re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
    URL = "https://www.icbc.com.cn/column/1438058319784067091.html"
    JS = """() => {
      const out=[];
      const links=[...document.querySelectorAll('a[href*="/page/"]')];
      for(const a of links){
        const t=(a.innerText||'').trim();
        let h=a.getAttribute('href')||'';
        if(t.length<6) continue;
        let node=a.parentElement, ctx='';
        for(let i=0;i<4 && node;i++){ const c=node.innerText||''; if(c.length>t.length+4){ctx=c;break;} node=node.parentElement; }
        if(h.startsWith('/')) h='https://www.icbc.com.cn'+h;
        out.push({title:t, url:h, ctx:ctx});
      }
      return out;
    }"""
    deals = []
    try:
        def _core(b):
            items = _pw_scrape(b, URL, JS, wait=1500, timeout=25000)
            today = _dt.date.today()
            for it in items:
                raw = it.get("ctx", "")
                m = DATE.search(raw)
                d = ""
                if m:
                    d = m.group(0).replace("年", "-").replace("月", "-").replace("/", "-")
                    try:
                        if _dt.date.fromisoformat(d) < today:
                            d = today.isoformat()  # 过去起始日→常在售，刷新为今天
                    except ValueError:
                        d = today.isoformat()
                else:
                    d = today.isoformat()
                deals.append({
                    "platform": "工商银行",
                    "category": "银行优惠",
                    "city": "",
                    "title": it["title"],
                    "detail": "",
                    "url": it["url"],
                    "confidence": "🟢",
                    "source": "icbc",
                    "date": d,
                    "date_raw": d,
                })
            print(f"ICBC_OK 抓取 {len(deals)} 条优惠活动")
        if browser is None:
            with _own_browser() as b:
                _core(b)
        else:
            _core(browser)
    except Exception as e:
        print("ICBC_FETCH_ERROR", e)
    return deals


def fetch_bendibao(browser=None):
    """深圳本地宝「深圳免费/周末活动」列表（JS 渲染，需 Playwright 无头浏览器）。
    抓首页活动类文章，提取标题/链接/日期，统一归「🎟️ 深圳活动」分类。
    覆盖：免费培训、市集、展览、演出、比赛、派对、嘉年华、亲子、交友、体验等。
    browser 可传入已启动的 chromium（与 icbc 共用），None 时自起自用。"""
    import re as _re
    # 事件类关键词（过滤掉 招聘/政策/买房/养老金等民生专题）
    EV = _re.compile(r"(免费|活动|市集|展览|演出|比赛|派对|嘉年华|培训|"
                     r"交友|体验|亲子|手工|快闪|展会|音乐节|戏剧|工作坊|"
                     r"报名|周末活动|相亲|公益|集市)")
    # 优惠券类（低质量，明确排除）：消费券/优惠券/代金券/满减/抢券/ * 券等
    NEG = _re.compile(r"(消费券|优惠券|代金券|满减|抢券|领券|券面|用券|"
                       r"发券|领消费券|优惠明细|优惠规则|适用门店)")
    # 招聘/考试类（民生专题，明确排除）：招聘、招考、考试、公考、编制等
    BLOCK = _re.compile(r"(招聘|招考|招录|考试|笔试|面试|准考证|查分|公务员|"
                        r"事业编|考公|考编|教师招聘|校招|社招|应聘|求职|公职)")
    URL = "https://sz.bendibao.com/"
    JS = """() => {
      const out=[];
      const links=[...document.querySelectorAll('a')];
      for(const a of links){
        const t=(a.innerText||'').trim();
        let h=a.getAttribute('href')||'';
        if(t.length<6 || !h || h.startsWith('#') ||
           h.startsWith('javascript')) continue;
        if(h.startsWith('/')) h=location.origin+h;
        if(!(h.includes('bendibao.com'))) continue;
        let node=a.parentElement, ctx='';
        for(let i=0;i<3 && node;i++){ const c=node.innerText||''; if(c.length>t.length+2){ctx=c;break;} node=node.parentElement; }
        out.push({title:t, url:h, ctx:ctx});
      }
      return out;
    }"""
    deals = []
    try:
        def _core(b):
            items = _pw_scrape(b, URL, JS, wait=2000, timeout=25000)
            for it in items:
                t = it["title"]
                if not EV.search(t):
                    continue
                if NEG.search(t):
                    continue  # 优惠券类低质量，跳过
                if BLOCK.search(t):
                    continue  # 招聘/考试类民生专题，明确排除
                # 日期：优先 URL 路径段（.../2026812/ → 2026-08-12），否则回退上下文文本
                d = norm_date_url(it["url"]) or norm_date(it.get("ctx", ""))[0]
                deals.append({
                    "platform": "深圳本地宝",
                    "category": "深圳活动",
                    "city": "深圳",
                    "title": t,
                    "detail": "",
                    "url": it["url"],
                    "confidence": "🟢",
                    "source": "bendibao",
                    "date": d,
                    "date_raw": d,
                    "_force_type": "🎟️ 深圳活动",
                })
            print(f"BENDBAO_OK 抓取 {len(deals)} 条深圳活动")
        if browser is None:
            with _own_browser() as b:
                _core(b)
        else:
            _core(browser)
    except Exception as e:
        print("BENDBAO_FETCH_ERROR", e)
    return deals


def make_hash(d):
    # price 维度仅对「快照」源(联盟)生效：价格变化则 hash 变→重新标 🆕；
    # 事件源 price 为空，行为与原逻辑一致。
    price = d.get("price", "") or ""
    return hashlib.sha1(
        (d["source"] + "|" + d["title"] + "|" + d["url"] + "|" + price).encode("utf-8")
    ).hexdigest()


# 日报展示：优先用户关心的类型，每类取最新若干，总量封顶（防噪音+PushPlus 限额）
# 类型顺序单一来源：选取优先级直接复用展示顺序 TYPE_ORDER，改一处即同步，避免漏改。
SELECT_PRIORITY = TYPE_ORDER
# 选取/限量参数：默认值在此定义，运行时被 config.json 的 "select" 段覆盖，调优无需改代码。
SELECT_DEFAULTS = {
    "per_type": 10,                  # 每类展示上限（控制总量，优质优先）
    "max": 40,                       # 日报总条目上限（宁少勿滥）
    "smzdm_per_type": 5,             # 电商券（卖东西）类特别限量
    "smzdm_cap": 10,                 # 什么值得买源级总上限（避免该源霸屏）
    "yangmaocun_cap": 20,            # 羊毛村最多展示条数
    "yangmaocun_max_age_days": 10,   # 羊毛村仅保留 N 天内有明确日期的线报
    "state_keep_buffer_days": 7,     # 去重状态保留缓冲（天）
}


def get_select_cfg():
    """选取参数：config.json['select'] 覆盖默认值，调优不改代码。"""
    cfg = load_config().get("select", {}) or {}
    merged = dict(SELECT_DEFAULTS)
    for k, v in cfg.items():
        if k in SELECT_DEFAULTS and isinstance(v, int) and v > 0:
            merged[k] = v
    return merged


# 新鲜度过滤：只保留发布于最近 N 天内的（无日期项保留并标"日期未知"）。
# 注意：55信用卡源最新文章实测停在数月前，默认 30 天会把它整体过滤掉；
# 若过滤后条目过少（<10）则自动放宽保留全部，避免日报变空。
MAX_AGE_DAYS = 30


def select_deals(deals, max_age_days=MAX_AGE_DAYS):
    """按真实日期倒序；新鲜度过滤（兜底放宽）；再按类型优先级+限量展示。"""
    today = _dt.date.today()
    sc = get_select_cfg()  # 选取参数（可被 config.json 覆盖）

    def age_of(d):
        iso = d.get("date")
        if not iso:
            return None
        try:
            return (today - _dt.date.fromisoformat(iso)).days
        except ValueError:
            return None

    # 有日期的排前并倒序，无日期的沉底（保持原相对顺序）
    deals_sorted = sorted(deals, key=lambda d: d.get("date") or "0000-00-00",
                          reverse=True)

    if max_age_days and max_age_days > 0:
        kept = [d for d in deals_sorted
                if age_of(d) is None or age_of(d) <= max_age_days]
        if len(kept) < 10:
            kept = deals_sorted  # 兜底：过滤后过少则放宽，保留全部
    else:
        kept = deals_sorted

    # 羊毛村专项收紧：仅保留 10 天内有明确日期的线报，剔除旧帖与无日期项。
    # 旧帖/无日期多为过期或低质线报，按「优质优先」原则直接剔除。
    kept = [d for d in kept
            if d.get("platform") != "羊毛村"
            or (d.get("date") and age_of(d) is not None
                and age_of(d) <= sc["yangmaocun_max_age_days"])]

    from collections import defaultdict

    # ---- 源约束：羊毛村最多最近 20 条；其他源每源至少 2 条 ----
    by_source = defaultdict(list)
    for d in kept:
        by_source[d["source"]].append(d)

    # 羊毛村（platform=羊毛村）配额：优先保奶茶，再按日期补其他类
    YM_CAP = sc["yangmaocun_cap"]
    ym_items = next((v for v in by_source.values()
                     if v and v[0].get("platform") == "羊毛村"), [])
    ym_sorted = sorted(ym_items,
                       key=lambda d: (1 if d.get("type") == "🥤 奶茶饮品" else 0,
                                     d.get("date") or "0000-00-00"),
                       reverse=True)
    ym_keep = {id(x) for x in ym_sorted[:YM_CAP]}
    capped = [d for d in kept
              if d.get("platform") != "羊毛村" or id(d) in ym_keep]

    # 什么值得买（source=smzdm）配额：源级总上限，避免该源（多卖东西）霸屏
    SMZDM_CAP = sc["smzdm_cap"]
    smzdm_items = by_source.get("smzdm", [])
    smzdm_keep = {id(x) for x in sorted(
        smzdm_items, key=lambda d: d.get("date") or "0000-00-00",
        reverse=True)[:SMZDM_CAP]}
    capped = [d for d in capped
              if d.get("source") != "smzdm" or id(d) in smzdm_keep]

    # 其他源：每源强制保留最近 2 条（不足 2 条则全保留）。
    # 注：什么值得买（卖东西）不享受保底，由上方电商券限量统一约束。
    guaranteed = []
    for src, items in by_source.items():
        if not items:
            continue
        if items[0].get("platform") == "羊毛村":
            continue
        if items[0].get("platform") == "什么值得买":
            continue
        s = sorted(items, key=lambda d: d.get("date") or "0000-00-00",
                   reverse=True)
        for d in s[:2]:
            guaranteed.append(d)

    # 按类型优先级在「已限源」池内选取；电商券（卖东西）类单独限量。
    # 强制保留项（每源保底）先纳入，确保不丢；其余按类型优先级在剩余预算内补足，
    # 保证 total 始终 <= sc["max"]（不再被保底项突破封顶）。
    by_type = defaultdict(list)
    for d in capped:
        by_type[d["type"]].append(d)

    out = []
    seen_ids = set()
    for d in guaranteed:
        if id(d) not in seen_ids:
            out.append(d)
            seen_ids.add(id(d))

    type_cap = {**{t: sc["per_type"] for t in SELECT_PRIORITY},
                "🛒 电商券": sc["smzdm_per_type"]}
    for t in SELECT_PRIORITY:
        if len(out) >= sc["max"]:
            break
        taken = sum(1 for x in out if x["type"] == t)
        budget_t = max(0, type_cap.get(t, sc["per_type"]) - taken)
        if budget_t <= 0:
            continue
        for d in by_type.get(t, []):
            if id(d) in seen_ids or budget_t <= 0 or len(out) >= sc["max"]:
                break
            out.append(d)
            seen_ids.add(id(d))
            budget_t -= 1
    return out[:sc["max"]]


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": {}, "last_run": ""}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def prune_seen(seen, keep_days):
    """清理过期的去重记录，防止 wool_state.json 随运行天数无限膨胀。
    seen 结构：hash -> 最近出现的日期(iso)。仅保留 keep_days 内的条目。"""
    if not keep_days or keep_days <= 0:
        return seen
    cutoff = (_dt.date.today() - _dt.timedelta(days=keep_days)).isoformat()
    return {h: d for h, d in seen.items() if d and d >= cutoff}


def render(items, max_age=MAX_AGE_DAYS):
    """markdown 表格版（推送 PushPlus / 本地 .md）。含发布日期+距今。"""
    today = _dt.date.today().isoformat()
    groups = {}
    for d, is_new in items:
        groups.setdefault(d["type"], []).append((d, is_new))
    lines = [
        f"# 深圳薅羊毛日报 · {today}", "",
        f"> 🟢官方 🟡网站二手(点链接自核) ⚪线索。"
        f"展示近 {max_age} 天在售优惠（过期自动淘汰）；🆕 为新上架。", "",
    ]
    for t in TYPE_ORDER:
        if t not in groups:
            continue
        lines.append(f"## {t}（{len(groups[t])}）")
        lines.append("")
        lines.append("| 来源 | 发布 | 标题 | 置信 |")
        lines.append("|---|---|---|---|")
        for d, is_new in groups[t]:
            pub = (d["date"][5:] if d.get("date") else "—") + " · " + age_label(d)
            title = ("🆕 " + d["title"]) if is_new else d["title"]
            lines.append(
                f"| {d['platform']} | {pub} | [{title}]({d['url']}) "
                f"| {d['confidence']} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_html(items, max_age=MAX_AGE_DAYS):
    """本地 HTML 备份表格版（含发布日期+距今）。"""
    today = _dt.date.today().isoformat()
    groups = {}
    for d, is_new in items:
        groups.setdefault(d["type"], []).append((d, is_new))
    parts = [
        f'<h2>深圳薅羊毛日报 · {today}</h2>',
        '<table border="1" cellspacing="0" cellpadding="6" '
        'style="border-collapse:collapse;font-size:14px;width:100%">',
        '<thead><tr><th>来源</th><th>发布</th><th>标题</th>'
        '<th>置信</th></tr></thead><tbody>',
    ]
    for t in TYPE_ORDER:
        if t not in groups:
            continue
        parts.append(
            f'<tr><td colspan="4" style="background:#f0f0f0;'
            f'font-weight:bold">{esc(t)}（{len(groups[t])}）</td></tr>'
        )
        for d, is_new in groups[t]:
            pub = (d["date"][5:] if d.get("date") else "—") + " · " + age_label(d)
            parts.append(
                "<tr>"
                f"<td>{esc(d.get('platform', ''))}</td>"
                f"<td>{esc(pub)}</td>"
                f"<td><a href=\"{esc(d['url'])}\">{esc(d['title'])}</a></td>"
                f"<td>{d['confidence']}</td>"
                "</tr>"
            )
    parts.append("</tbody></table>")
    parts.append(
        "<p><small>🟢官方 🟡网站二手(点链接自核) ⚪线索。"
        f"展示近 {max_age} 天在售优惠（过期自动淘汰）；🆕 为新上架。</small></p>"
    )
    return "<html><body>" + "".join(parts) + "</body></html>"


def render_bot_md(items, max_age=MAX_AGE_DAYS):
    """企业微信群机器人版 markdown（无表格兜底、链接可点、4096 字节上限）。含发布日期。"""
    today = _dt.date.today().isoformat()
    groups = {}
    for d, is_new in items:
        groups.setdefault(d["type"], []).append((d, is_new))
    header = (
        f"# 深圳薅羊毛日报 · {today}\n"
        f"> 🟡 网站二手，点链接自行核实。展示近 {max_age} 天在售优惠（过期自动淘汰）；🆕 为新上架。\n"
    )
    blocks = []
    for t in TYPE_ORDER:
        if t not in groups:
            continue
        lines = [f"## {t}（{len(groups[t])}）", ""]
        for d, is_new in groups[t]:
            title = ("🆕 " + d["title"]) if is_new else d["title"]
            lines.append(
                f"- [{title}]({d['url']}) · {age_label(d)} · {d['confidence']}"
            )
        lines.append("")
        blocks.append("\n".join(lines))
    if not blocks:
        return [header + "\n> 今日无符合条件的优惠。"]
    return [header + "\n".join(blocks)]


def _load_env_file():
    """从 WORKDIR/.env 加载密钥类环境变量（若存在），避免明文写入 config.json。
    仅在对应变量未设置时生效；.env 不应提交到任何仓库/共享。"""
    p = os.path.join(WORKDIR, ".env")
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def load_config():
    cfg = os.path.join(WORKDIR, "config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_webhook():
    """webhook 地址优先级：环境变量 WOOL_WEBHOOK > config.json['webhook']。"""
    env = os.environ.get("WOOL_WEBHOOK")
    if env:
        return env.strip()
    return load_config().get("webhook", "").strip()


def chunk_md(md, limit=3800):
    """按 '## ' 分组切分，保证每条消息不超企业微信 4096 字节（留余量）。"""
    if len(md) <= limit:
        return [md]
    parts = md.split("\n## ")
    header = parts[0]
    chunks, cur = [], header
    for p in parts[1:]:
        block = "\n## " + p
        if len(cur) + len(block) > limit:
            chunks.append(cur)
            cur = header + block
        else:
            cur += block
    if cur:
        chunks.append(cur)
    return chunks


def send_webhook(md):
    url = load_webhook()
    if not url:
        print("WEBHOOK_NOT_CONFIGURED skip")
        return False
    ok = True
    for chunk in chunk_md(md):
        payload = {"msgtype": "markdown", "markdown": {"content": chunk}}
        try:
            r = requests.post(url, json=payload, timeout=15)
            print("webhook rc", r.status_code, r.text[:120])
            if r.status_code != 200 or "\"errcode\":0" not in r.text:
                ok = False
        except Exception as e:
            print("webhook error", e)
            ok = False
    return ok


def load_pushplus():
    """PushPlus token 优先级：环境变量 PUSHPLUS_TOKEN > WORKDIR/.env > config.json['pushplus_token']。
    密钥建议放环境变量或 .env，避免明文写入 config.json。"""
    env = os.environ.get("PUSHPLUS_TOKEN")
    if env:
        return env.strip()
    return load_config().get("pushplus_token", "").strip()


def notify_failure(msg):
    """关键步骤（推送等）失败时的本地兜底告警：写 wool_alert.txt + stderr。
    运维可监控该文件触发通知（如置顶到次日日报或发邮件）。"""
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}\n"
    print("ALERT", line, end="", file=sys.stderr)
    try:
        with open(os.path.join(WORKDIR, "wool_alert.txt"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def send_pushplus(md, title):
    """PushPlus 直发到个人微信服务通知（markdown 表格模板）。返回是否成功。
    含失败重试（指数退避 2s/4s），缓解偶发 SSL/网络抖动；
    全部失败后调用 notify_failure 落盘告警。"""
    token = load_pushplus()
    if not token:
        print("PUSHPLUS_NOT_CONFIGURED skip")
        return False
    chunks = chunk_md(md, limit=8000)
    ok_all = True
    for i, chunk in enumerate(chunks, 1):
        payload = {
            "token": token,
            "title": title + (f" ({i})" if i > 1 else ""),
            "content": chunk,
            "template": "markdown",
        }
        success = False
        for attempt in range(3):
            try:
                r = requests.post("https://www.pushplus.plus/send",
                                  json=payload, timeout=15)
                data = r.json() if r.text else {}
                if r.status_code == 200 and data.get("code", -1) == 200:
                    success = True
                    break
                print(f"pushplus rc={r.status_code} code={data.get('code')} "
                      f"attempt={attempt+1} {r.text[:160]}")
            except Exception as e:
                print(f"pushplus error attempt={attempt+1}: {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # 2s, 4s 退避
        if not success:
            ok_all = False
        if i < len(chunks) and success:
            time.sleep(2)  # 避免免费档推送频率过快
    if not ok_all:
        notify_failure(f"PushPlus 推送部分/全部失败（共 {len(chunks)} 段），"
                       f"请检查网络或 token 是否有效")
    return ok_all


def main():
    _load_env_file()
    today = _dt.date.today().isoformat()
    cfg = load_config()
    max_age = int(cfg.get("max_age_days", MAX_AGE_DAYS))
    state = load_state()
    seen = state.get("seen", {})

    # 源开关：默认启用哪些源（与 enabled_sources 配置联动，便于回退/扩展）
    SOURCES = {
        "55card": fetch_55card,
        "yangmaocun": fetch_yangmaocun,
        "smzdm": fetch_smzdm,
        "union": fetch_union,
        "icbc": fetch_icbc,
        "bendibao": fetch_bendibao,
        "xiaohongshu": fetch_xiaohongshu,
    }
    enabled = cfg.get("enabled_sources") or list(SOURCES.keys())
    PW_SOURCES = ("icbc", "bendibao")  # Playwright 源：共用一个 chromium
    raw = []

    # 非 Playwright 源（纯 requests）并发抓取，缩短总耗时
    concurrent = [n for n in ("55card", "yangmaocun", "smzdm", "union", "xiaohongshu") if n in enabled]
    if concurrent:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(concurrent)) as ex:
            futs = {ex.submit(SOURCES[n]): n for n in concurrent}
            for fut in as_completed(futs):
                try:
                    raw += fut.result()
                except Exception as e:
                    print("FETCH_ERROR", futs[fut], e)

    # Playwright 源共用一个 chromium（启动失败则各自回退到自带浏览器）
    shared_browser = None
    pw_ctx = None
    try:
        if set(PW_SOURCES) & set(enabled):
            from playwright.sync_api import sync_playwright
            pw_ctx = sync_playwright().start()
            shared_browser = pw_ctx.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
    except Exception as e:
        print("PW_SHARED_LAUNCH_FAIL fallback per-fetch:", e)
        shared_browser = None
        pw_ctx = None
    try:
        for name in PW_SOURCES:
            if name in enabled:
                raw += SOURCES[name](browser=shared_browser)
    finally:
        if shared_browser is not None:
            try:
                shared_browser.close()
            except Exception:
                pass
        if pw_ctx is not None:
            try:
                pw_ctx.stop()
            except Exception:
                pass
    for d in raw:
        d["type"] = classify(d)
    filtered = [d for d in raw if d["type"] not in BLOCKED_TYPES]

    # 去重判新（全量标记，避免次日抖动）
    for d in filtered:
        h = make_hash(d)
        is_new = h not in seen
        seen[h] = today
        d["_new"] = is_new

    # 日报语义 B：展示「当前在售全集」——近 max_age 天内活跃优惠每天列出，
    # 过期才淘汰；持续在售优惠常驻（不再只报一次）。d["_new"] 仍用于标 🆕。
    selected = select_deals(filtered, max_age)
    items = [(d, d["_new"]) for d in selected]

    md = render(items, max_age)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    html = render_html(items, max_age).replace("><", ">\n<")
    with open(REPORT_PATH.replace(".md", ".html"), "w", encoding="utf-8") as f:
        f.write(html)

    # 投递：PushPlus 主通道（markdown 表格，个人微信服务通知，无人值守）
    bot_md = render_bot_md(items, max_age)
    full_md = "\n".join(bot_md)
    if os.environ.get("WOOL_DRYRUN"):
        # 本地预览模式：只生成文件、不推送（用于调试/确认）
        print("DRY_RUN: 跳过推送")
    elif not send_pushplus(md, f"深圳薅羊毛日报 · {today}"):
        # 备用：企业微信群机器人 webhook；两者都失败则落盘告警
        if not send_webhook(full_md):
            notify_failure("主通道 PushPlus 与备用 webhook 均未送达，日报丢失")

    if not os.environ.get("WOOL_DRYRUN"):
        keep = max_age + get_select_cfg()["state_keep_buffer_days"]
        state["seen"] = prune_seen(seen, keep)
        state["last_run"] = today
        save_state(state)

    blocked = len(raw) - len(filtered)
    new_count = sum(1 for _, n in items if n)
    dated = sum(1 for d, _ in items if d.get("date"))
    print(f"OK total={len(raw)} passed={len(filtered)} "
          f"blocked={blocked} selected={len(items)} new={new_count} "
          f"dated={dated} max_age={max_age} report={REPORT_PATH}")


if __name__ == "__main__":
    main()
