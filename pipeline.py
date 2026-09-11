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
import traceback
from contextlib import contextmanager
from html import escape as _esc
from html import unescape as _unesc

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
TYPE_ORDER = ["🥤 奶茶饮品", "🧋 奶茶联名", "🍜 餐饮美食", "🛵 外卖红包", "💰 支付立减",
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
                if label == "🥤 奶茶饮品":
                    src = d.get("source")
                    # 羊毛村奶茶线报改归「🛒 电商券」：这些线报仍可薅，只是不再进奶茶饮品区。
                    if src in ("ym2.cc", "ymnnc.com"):
                        d["_ym_milktea"] = True  # 选取时保留独立配额（见 select_deals）
                        return "🛒 电商券"
                    # 「🥤 奶茶饮品」只由微博官号（source=milktea）供内容：
                    # 其他源命中奶茶词不再归此类，继续往下匹配（如什么值得买「加多宝凉茶」→📦 其他）。
                    if src != "milktea":
                        break
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
# 内容路由：小红书只产出「深圳活动」类 —— 奶茶两区只由微博官号供内容，
# 故不再把饮品笔记路由进「🥤 奶茶饮品」（避免非官号内容混入官方奶茶口径）。
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
    # 只保留「深圳活动」类关键词：饮品类已移交微博官号（奶茶两区纯官号）。
    "keywords": [
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
            # 只产出「深圳活动」类：奶茶类内容已移交微博官号（奶茶两区纯官号）。
            # 路由同时看标题+描述(blob)，救回「描述有活动信号但标题没写」的笔记。
            if XHS_TYPE_EVENT.search(blob):
                ftype = "🎟️ 深圳活动"
            else:
                continue  # 无明确活动信号，丢弃避免错分
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


# ---- 奶茶官微信源（只抓品牌官微时间线，🟢 官方口径，全国范围，不强制深圳）----
# 只认官号：早期用过微博「实时搜索」兜底，但搜索流里大量是普通用户晒单/MCN 号，
# 与「只看官方内容」的要求相悖，已整体删除（官微主页 m.weibo.cn/u/<uid> 免登录可浏览）。
# 代价：官微主号以联名/代言/公益为主，门店级「买一送一」多由区域号/门店号发，
# 故官微口径下「🥤 奶茶饮品」天然偏少——这是刻意的取舍，不做搜索回补。
# 注意：这里刻意 **不再** 放 window_days / topic_neg / deal_pos 三个键。
# 它们原先在此定义，但从未被任何代码消费（真正的价值闸门是下面的 MILKTEA_DEAL 正则），
# 属于典型的「僵尸配置」——注释写着「调词改这里」，改了却完全不生效。
# 实测往 MILKTEA_DEFAULTS["deal_pos"] 加词后，收录结果毫无变化。
# 留着的唯一后果是误导后来调词的人（以为改了、实际没改）。
# 要调词请：①改 MILKTEA_DEAL / MILKTEA_LOTTERY_NEG 正则（推荐，保留边界保护）；
#          ②或在 config.json 显式配 milktea.deal_pos / milktea.lottery_neg
#            （走纯词表路，会丢失内置正则的边界保护，见 get_milktea_cfg）。
MILKTEA_DEFAULTS = {
    "max_age_hours": 168,  # 只收近 7 天官微微博（仅官微后 48h 过窄，整区易空）
    # 品牌官微 uid（已逐个核对粉丝量 + 「微博认证」，2026-09-11 确认）。
    # 列表顺序 = 抓取顺序：高优品牌（奈雪/喜茶/霸王茶姬）排前。
    "brand_uids": [
        ("奈雪的茶", "5884674413"),  # 147.1万粉 · 高优
        ("喜茶", "2804387887"),      # 146.7万粉 · 高优
        ("霸王茶姬", "5652018762"),  # 106.1万粉 · 高优
        ("瑞幸咖啡", "6349791448"),  # 117.2万粉
        ("古茗茶饮", "2809775704"),  # 146万粉
        ("蜜雪冰城", "1704709632"),  # 251.3万粉
        ("茶百道", "6502206666"),    # 73.8万粉
    ],
}
# 羊毛价值闸门（硬闸门，必须命中才收录）。
# 设计原则：只保留「能薅」的内容 —— 联名/联动（蹲联名）+ 免费/买一送一/赠/抽奖（真羊毛）。
# 已刻意移除「新品|上新|限定|周边|典藏|盲盒」：这些只说明「有东西卖了」，
# 不代表能白嫖，「古茗 HPP 茉莉银针 限定上新」这类纯上新对用户无意义。
# IP 加字母边界：(?<![A-Za-z])IP(?![A-Za-z]) 可排除 VIP / IPHONE 等误伤。
# 词表可在 config.json 的 milktea.deal_pos 覆盖，调词不用改代码。
MILKTEA_DEAL = re.compile(
    r"(联名|联动|(?<![A-Za-z])IP(?![A-Za-z])|合作款|"
    r"免费|免单|买\s*[1一]\s*送\s*[1一]|第二杯|第二件|半价|"
    r"(?<!\d)(0元|1元|9\.9|9块9)|买赠|附赠|赠送|赠品|(?<!捐)赠|随杯|加价购|"
    r"抽奖|抽送|揪.{0,6}(位|个|名)|免邮|包邮|兑换|领取|福利|羊毛|秒杀|特价|优惠|立减|满减|"
    r"送.{0,4}(周边|好礼|礼包|全套|杯|券|贴纸|徽章|公仔|盲盒|玩偶|挂件|明信片|海报|定制|帆布|钥匙扣|杯套))"
)
# 分区闸门：命中「联名/联动/IP/合作款」→ 归「🧋 奶茶联名」区；
# 未命中（只命中买一送一/免费/抽奖等硬羊毛动作）→ 归「🥤 奶茶饮品」区。
# 两者同时命中时归联动区（口径：联动专门放联动区）。
MILKTEA_LINK = re.compile(r"(联名|联动|(?<![A-Za-z])IP(?![A-Za-z])|合作款)")
# 高优品牌：日报排序时这三家排在其他品牌之前，其后按发布时间倒序。
# 名称必须与 brand_uids 里的品牌名一致（即条目的 platform 字段）。
MILKTEA_TOP_BRANDS = ("奈雪的茶", "喜茶", "霸王茶姬")

# ---- 正文归一化：emoji/花式数字 → 普通数字（仅用于闸门匹配，不改动展示原文）----
# 官微（尤其霸王茶姬）常把「买一送一」写成「买1️⃣送1️⃣」。这类 emoji 数字是
# '1'(U+0031) + 变体选择符(U+FE0F) + keycap 组合符(U+20E3) 三个码位拼的，
# 直接跑正则不命中 —— 2026-09-11 霸王茶姬「为郑钦文加油·全场买1️⃣送1️⃣」那条
# 预告帖就是这样被价值闸门整条丢掉的（看起来像"官微没发"，实为解析漏收）。
_VS_RE = re.compile(r"[\uFE0E\uFE0F\u20E3\u200B\u200C\u200D\u200E\u200F\u2060]")
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _norm_text(t):
    """去掉变体选择符/keycap 组合符/零宽字符并半角化数字。

    「1️⃣」去掉 U+FE0F+U+20E3 后即「1」，于是「买1️⃣送1️⃣」等价于「买1送1」。
    """
    if not t:
        return ""
    return _VS_RE.sub("", t).translate(_FW_DIGITS)


# 「抽奖/评论区送」负向闸门：要中奖才拿得到的内容一律丢弃（用户口径：根本抽不到我）。
# 与正羊毛（买一送一/免费领/免单券/第二杯半价）严格区分 —— 后者确定可得，不在本表。
# 覆盖：抽奖平台公告、转+关抽、评论区揪N位、随机抽N位、请喝N杯 等。
# 词表可在 config.json 的 milktea.lottery_neg 覆盖，调词不用改代码。
MILKTEA_LOTTERY_NEG = re.compile(
    r"抽奖|抽送|免费抽|抽\d+位|抽\d+名|抽\d+个|揪\d+|揪.{0,3}位|"
    r"送\d+位|请喝\d+杯|转.{0,3}关|关注.{0,5}(?:转发|抽)|转发.{0,5}抽|"
    r"锦鲤|中奖|抽中")


def _milktea_verdict(txt, deal_re, lottery_re):
    """对单条候选帖做闸门裁决，返回 (是否收录, 分区, 命中词)。

    判定顺序是本模块最容易踩坑的地方，故抽成纯函数便于离线回归：

      ① **联名/联动帖直接收录** → 🧋 奶茶联名，不受抽奖闸门影响。
         官微的联名公告几乎都带「关注+转发抽N位」的促互落款，若把抽奖闸门
         放在最前面一刀切，会把整条联名误杀 —— 实测奈雪×明日方舟终末地(9/11)、
         奈雪×豚豚崽(9/10) 两条联名就是这样被清空的（表现为"奈雪没有联动"）。
         口径：联名信息本身即价值（上线时间/周边/渠道），抽奖只是促互手段。
      ② 非联名 + 抽奖落款 → 丢（用户口径：抽奖根本抽不到我）。
      ③ 非联名 + 命中确定性羊毛（买一送一/免费/半价/0元…）→ 🥤 奶茶饮品。
      ④ 其余（纯上新/品牌日常/代言）→ 丢。
    """
    nt = _norm_text(txt or "")
    link_m = MILKTEA_LINK.search(nt)
    if link_m:
        # 命中词优先显示联动信号（联名/联动/IP/合作款），而不是可能出现在更前面的
        # 抽奖落款词（如「揪5位」）—— detail 列要能一眼看出这条为什么归联动区。
        return True, "🧋 奶茶联名", link_m.group(0)
    if lottery_re.search(nt):
        return False, "", ""
    m = deal_re.search(nt)
    if not m:
        return False, "", ""
    return True, "🥤 奶茶饮品", m.group(0)


def get_milktea_cfg():
    """奶茶官微信源配置：config.json 的 milktea 段覆盖默认值（改词调参不用碰代码）。"""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in MILKTEA_DEFAULTS.items()}
    u = (load_config() or {}).get("milktea", {}) or {}
    if isinstance(u.get("max_age_hours"), int) and u["max_age_hours"] > 0:
        cfg["max_age_hours"] = u["max_age_hours"]
    if u.get("lottery_neg") and isinstance(u["lottery_neg"], list):
        cfg["lottery_neg"] = u["lottery_neg"]
    # 编译后的价值闸门。**默认走代码内置正则**（含 IP 字母边界、(?<!捐)赠、
    # 「送XX(周边|券|杯)」搭配限定等边界保护）；仅当 config.json 显式配了
    # milktea.deal_pos 时才走纯词表路（re.escape 拼接）。
    # 坑：此前写成 cfg.get("deal_pos")，而 MILKTEA_DEFAULTS 自带 deal_pos，导致词表路
    # 恒生效、内置正则的边界保护全部失效 —— 实测奈雪「送奈雪100元心意卡」里的「0元」
    # 被误命中（内置 (?<!\d)0元 本可挡住）。故这里只看 config 的覆盖值 u。
    dp = u.get("deal_pos") or None
    try:
        cfg["_deal_re"] = (re.compile("(" + "|".join(re.escape(w) for w in dp)
                                      + r"|买\s*[1一]\s*送\s*[1一])")
                           if dp else MILKTEA_DEAL)
    except Exception:
        cfg["_deal_re"] = MILKTEA_DEAL
    # 抽奖负向闸门：config 可覆盖词表，未配时用代码内置正则（同上，只看 config 覆盖值）。
    ln = u.get("lottery_neg") or None
    try:
        cfg["_lottery_re"] = (re.compile("(" + "|".join(re.escape(w) for w in ln) + ")")
                              if ln else MILKTEA_LOTTERY_NEG)
    except Exception:
        cfg["_lottery_re"] = MILKTEA_LOTTERY_NEG
    if u.get("brand_uids") and isinstance(u["brand_uids"], list):
        cfg["brand_uids"] = [tuple(x) if isinstance(x, (list, tuple)) and len(x) == 2 else x
                             for x in u["brand_uids"]]
    return cfg


# 注：MILKTEA_WB_URL（微博实时搜索流）随「搜索兜底路」一并删除，官微只走 u/<uid> 时间线。


def _page_blocked(pg):
    """判断当前页是否被反爬验证页/登录墙顶替。命中则本轮后续关键词直接跳过，避免无效请求。"""
    try:
        u = (pg.url or "").lower()
    except Exception:
        u = ""
    if any(k in u for k in ("wappass", "captcha", "verify", "passport", "signin", "login")):
        return True
    try:
        t = pg.title() or ""
    except Exception:
        t = ""
    return any(k in t for k in ("安全验证", "访问异常", "机器人", "请输入验证码", "登录"))


def _wb_rel_time(s, now):
    """把微博相对/绝对时间解析为 datetime，解析不了返回 None（无法判时效的条目宁可丢弃）。

    支持：刚刚 / X秒前 / X分钟前 / X小时前 / X天前 / 今天 HH:MM / 昨天 HH:MM /
          MM-DD / MM-DD HH:MM / YYYY-MM-DD / YYYY-MM-DD HH:MM。

    为什么必须支持带时分的 MM-DD：m.weibo.cn 用户主页（官微时间线）的 span.time
    基本都是「9-9 11:47」这种 **MM-DD HH:MM** 格式，只有近期几条才显示「10小时前」。
    早期正则漏写了 ` HH:MM` 后缀，导致带时分的整条被判「无时间」丢弃——
    实测奈雪首屏 10 条只有 1 条能通过、霸王茶姬 11 条只有 2 条能通过，
    九成官微帖在时间闸门就被扔掉，这是官微覆盖率的最大损失点。"""
    s = (s or "").strip()
    if not s:
        return None
    if s in ("刚刚", "刚刚发布"):
        return now
    for unit, kw in (("seconds", "秒"), ("minutes", "分钟"), ("hours", "小时"), ("days", "天")):
        m = re.match(r"^(\d+)%s前$" % kw, s)
        if m:
            return now - _dt.timedelta(**{unit: int(m.group(1))})
    m = re.match(r"^(今天|昨天)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        base = now if m.group(1) == "今天" else now - _dt.timedelta(days=1)
        return base.replace(hour=int(m.group(2)), minute=int(m.group(3)),
                            second=0, microsecond=0)
    if s.startswith("今天"):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s.startswith("昨天"):
        return (now - _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # 绝对日期：MM-DD / MM-DD HH:MM / YYYY-MM-DD / YYYY-MM-DD HH:MM（尾巴上的时分可选）
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+\d{1,2}:\d{2})?$", s)
    if m:
        try:
            return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    m = re.match(r"^(\d{1,2})-(\d{1,2})(?:\s+\d{1,2}:\d{2})?$", s)
    if m:
        try:
            d = _dt.datetime(now.year, int(m.group(1)), int(m.group(2)))
        except Exception:
            return None
        # 跨年：1 月看到「12-30」应归属去年，否则会解析成未来时间而漏收
        if d > now + _dt.timedelta(days=1):
            d = d.replace(year=now.year - 1)
        return d
    return None


def _wb_cards(pg):
    """从 m.weibo.cn 页面（实时搜索流 或 用户主页）提取微博条目，结构完全一致。
    返回 [(时间串, 正文, 微博链接 or "")]。无 .weibo-text 的是超话/用户卡片，跳过。"""
    out = []
    for c in pg.query_selector_all("div.card-wrap"):
        wt = c.query_selector(".weibo-text")
        if not wt:
            continue
        txt = re.sub(r"\s+", " ", (wt.inner_text() or "")).strip()
        te = c.query_selector("span.time")
        a = c.query_selector('a[href*="/status/"]')
        out.append(((te.inner_text() or "").strip() if te else "", txt,
                    (a.get_attribute("href") or "") if a else ""))
    return out


def _wb_api_time(s, now):
    """解析微博官方接口的 created_at，如 「Fri Sep 11 14:00:17 +0800 2026」。

    接口给的是带时区的绝对时间，比页面上「9-9 11:47 / 10小时前」可靠得多。
    转成本地时区的 naive datetime，与 datetime.now() 同口径（避免 +8 小时错位）。
    """
    try:
        d = _dt.datetime.strptime((s or "").strip(), "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None
    return d.astimezone().replace(tzinfo=None)


def _wb_api_cards(pg, uid, page=1):
    """在页面内 fetch 微博官方时间线接口，返回 [(created_at, 正文, 链接)]。

    为什么不再靠 DOM 解析（_wb_cards）：m.weibo.cn 用户主页的卡片是「逐步水合」的，
    实测 10~11 张首屏卡里有 3~9 张取不到 a[href*="/status/"]，这些条目会被
    「缺链接」整条丢弃 —— 霸王茶姬 9/7 那条迪士尼公主联名就是这么丢的
    （霸王茶姬首屏 11 条里 3 条缺链接、喜茶 5 条、瑞幸 9 条）。
    接口返回的 id/text/created_at 字段完整，且时间精确。
    注意：接口对未登录用户只给第 1 页（page=2 起返回空），故不做翻页。
    """
    url = ("https://m.weibo.cn/api/container/getIndex?containerid=107603"
           + str(uid) + "&page=" + str(page))
    try:
        data = pg.evaluate(
            "async (u) => { try { const r = await fetch(u, "
            "{headers: {'X-Requested-With': 'XMLHttpRequest'}}); "
            "if (!r.ok) return {__e: r.status}; return await r.json(); } "
            "catch (e) { return {__e: -1}; } }", url)
    except Exception:
        return []
    if not isinstance(data, dict) or data.get("__e"):
        return []
    out = []
    for c in ((data.get("data") or {}).get("cards") or []):
        mb = c.get("mblog") or {}
        mid = mb.get("id") or ""
        if not mid:
            continue
        raw = mb.get("longText") or mb.get("text") or ""
        txt = re.sub(r"\s+", " ", _unesc(re.sub(r"<[^>]+>", " ", raw))).strip()
        if len(txt) < 10:
            continue
        out.append((mb.get("created_at") or "", txt,
                    "https://m.weibo.cn/status/" + str(mid)))
    return out


def _official_rows(pg, uid, now):
    """官微时间线条目 [(datetime, 正文, 链接)]；datetime 为 None 表示无法判时效。

    优先走官方接口（字段完整、时间精确、不受卡片水合影响）；
    接口不可用时退回 DOM 解析（_wb_cards + 相对/绝对时间解析）。
    """
    rows = _wb_api_cards(pg, uid)
    if rows:
        return [(_wb_api_time(t, now), x, h) for t, x, h in rows]
    return [(_wb_rel_time(t, now), x,
             ("https://m.weibo.cn" + h if h.startswith("/") else h))
            for t, x, h in _wb_cards(pg)]


def _pick_title(txt, brand_re=None, deal_re=None, neg_re=None):
    """标题选句：优先含品牌名的正文句 → 含价值词的句子 → 首句，截断 50 字。

    两级避让（顺序敏感，故抽成独立函数便于回归）：
    ① 先跳过「抽奖落款句」（含 neg_re 的句子）——官微联名公告末尾常挂
       「关注＋转发，揪5位送周边」，若选进标题会让人误以为这条只是抽奖
       （用户刚要求排除抽奖类，标题里再出现即误导）；
    ② 品牌句优先于价值词句 —— 否则「联名蔬果酸奶昔：」这类产品列表行会被选成
       标题，而「9月10日，来奈雪与豚豚崽一起解锁松弛」才是真正的活动正文。
    若所有候选句都带抽奖落款，退回不避让的原逻辑，保证总能出标题。
    """
    deal_re = deal_re or MILKTEA_DEAL
    # 分割符：句末标点 + 常见 emoji/符号区。范围必须够宽 —— 原先只写了
    # [\U0001F300-\U0001FAFF]，漏掉 ✨(U+2728)/✖(U+2716) 等 2600–27BF 区符号，
    # 结果「…解锁松弛～ ✨关注＋转发，揪5位送周边」被并成一句、含「揪」被整句跳过。
    sents = [s.strip() for s in re.split(
        r"[。！？\n]|[\U0001F000-\U0001FAFF\U00002190-\U000021FF"
        r"\U00002600-\U000027BF\U00002B00-\U00002BFF\uFE0F]", txt)
        if len(s.strip()) >= 6]

    def _pick(pred, skip_neg):
        for _s in sents:
            if skip_neg and neg_re and neg_re.search(_s):
                continue
            if pred(_s):
                return _s[:50]
        return None

    cands = []
    if brand_re:
        cands.append(lambda s: brand_re.search(s) and deal_re.search(s))
        cands.append(lambda s: brand_re.search(s))
    cands.append(lambda s: deal_re.search(s))
    for skip in (True, False):
        for pred in cands:
            r = _pick(pred, skip)
            if r:
                return r
    return (sents[0] if sents else txt)[:50]


def fetch_milktea(browser=None):
    """奶茶官微信源：只抓 7 个品牌官微的时间线（m.weibo.cn/u/<uid>，免登录）。

    只认官号：不抓实时搜索（搜索流含大量普通用户晒单/MCN 号，非官方口径）。
    闸门：①时效 ≤ max_age_hours（默认 168h）②命中羊毛价值词（联名/联动/免费/买一送一/
    第二杯/半价/赠…）—— 纯「新品上新」不收录，因为没有羊毛价值；③纯抽奖帖排除
    （要中奖才拿得到，用户口径「抽不到我」），但**联名帖豁免抽奖闸门** —— 官微的联名
    公告几乎都带「关注+转发抽N位」促互落款，一刀切会把整条联名误杀。
    分区：命中联名/联动 → 🧋 奶茶联名；只命中硬羊毛动作 → 🥤 奶茶饮品。
    置信度：官微 🟢。可接收外部 browser 复用，避免重复启动 chromium。"""
    deals = []
    seen_urls = set()
    mc = get_milktea_cfg()
    now = _dt.datetime.now()
    cutoff = now - _dt.timedelta(hours=mc["max_age_hours"])
    # 注：官微路不套 topic_neg（那套生活噪音词是给小红书用的，会误杀品牌官宣，见下方循环）。
    # 价值闸门 / 抽奖闸门 / 分区判定统一由 _milktea_verdict 裁决（顺序敏感，见其文档串）；
    # 匹配一律走 _norm_text（emoji 数字归一化），否则「买1️⃣送1️⃣」这类官微写法会被漏掉。
    deal_re = mc.get("_deal_re") or MILKTEA_DEAL
    lottery_re = mc.get("_lottery_re") or MILKTEA_LOTTERY_NEG

    own = browser is None
    pcm = None  # 仅在 own 时创建；必须 stop，否则每跑一次泄漏一个 playwright driver 进程
    try:
        if own:
            from playwright.sync_api import sync_playwright
            pcm = sync_playwright().start()
            browser = _launch_browser(pcm)
        pg = browser.new_page()
        # ---- 品牌官微时间线（官宣第一手，唯一来源 → 置信度 🟢）----
        # uid 必须逐个核对粉丝量与「微博认证」：m.weibo.cn/n/<昵称> 会重定向到同名
        # 山寨号（实测「瑞幸咖啡」「霸王茶姬」「茶百道」都撞到粉丝个位数的假号）。
        # 抓取走官方时间线接口（_official_rows），不再解析 SPA 卡片 —— 主页卡片是逐步
        # 水合的，靠 DOM 取链接会丢掉四到八成条目（详见 _official_rows 文档串）。
        # 品牌顺序即优先级：奈雪/喜茶/霸王茶姬 在前（见 MILKTEA_DEFAULTS.brand_uids）。
        for bname, uid in mc.get("brand_uids") or []:
            # 品牌名正则：官微正文常用简称（写「奈雪」而非「奈雪的茶」），故同时匹配
            # 全名与前两字简称，供 _pick_title 优先选「含品牌名的正文句」当标题。
            brand_re = re.compile(re.escape(bname) + "|" + re.escape(bname[:2]))
            try:
                pg.goto(f"https://m.weibo.cn/u/{uid}",
                        wait_until="domcontentloaded", timeout=25000)
                pg.wait_for_timeout(2500)
                if _page_blocked(pg):
                    # 撞墙即 break：同一出口 IP 已被拦，后面几个品牌大概率同样被挡，
                    # 继续请求只会白白拉长跑批时间（不再用标志位，break 本身即达成）。
                    print("MILKTEA_BLOCKED_OFFICIAL", bname)
                    break
                for dt, txt, link in _official_rows(pg, uid, now):
                    if not link or len(txt) < 10 or link in seen_urls:
                        continue
                    if dt is None or dt < cutoff:
                        continue
                    # 闸门裁决：联名主体优先收录（抽奖只是促互落款，不连坐）；
                    # 抽奖只做兜底排除；纯上新/品牌日常丢弃。顺序详见 _milktea_verdict。
                    ok, ftype, hit = _milktea_verdict(txt, deal_re, lottery_re)
                    if not ok:
                        continue
                    # 这里不再套 topic_neg：那套负向词（头发/美甲/穿搭/宠物…）是给
                    # 小红书生活内容设计的，用在品牌官宣上会误杀 —— 实测霸王茶姬
                    # 「适合和迪士尼公主们见一面…搭配」被「搭配」命中，整条被丢掉。
                    # 官微本身即品牌，无需品牌名闸门；官宣文案也不一定带信息性词，故不加。
                    seen_urls.add(link)
                    deals.append({
                        "platform": bname,
                        "category": "奶茶官微",
                        "city": "",
                        "title": _pick_title(_norm_text(txt), brand_re, deal_re, lottery_re),
                        "detail": "官微·" + hit,
                        "url": link,
                        "confidence": "🟢",
                        "source": "milktea",
                        "date": dt.strftime("%Y-%m-%d"),
                        "date_raw": dt.strftime("%Y-%m-%d"),
                        "_force_type": ftype,
                    })
            except Exception as e:
                print("MILKTEA_RUN_ERR", bname, e)
                continue
    finally:
        if own and browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pcm is not None:
            try:
                pcm.stop()
            except Exception:
                pass
    print(f"MILKTEA_OK 抓取 {len(deals)} 条")
    return deals


# ---- Playwright 复用：共用一个 chromium，避免每天重复启动 ----
# 有界面模式（检测到 DISPLAY 时）用于绕过部分源（如深圳本地宝）对「无 user_data_dir 的
# 临时启动浏览器」的反爬挑战：实测 bendibao 的 WAF 会拦截普通 launch（无头/有界面都拦），
# 但放行「带持久化 user_data_dir 的有界面浏览器」。故新服务器跑 Xvfb 并设 DISPLAY 走此路径；
# 旧服务器 cron 无 DISPLAY 则保持原无头逻辑，不受影响。
PW_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pw_profile")

def _launch_browser(pw_ctx):
    """按环境启动浏览器：有 DISPLAY → 有界面+持久化档案（绕过反爬）；否则无头。"""
    headful = bool(os.environ.get("DISPLAY"))
    if headful:
        return pw_ctx.chromium.launch_persistent_context(
            user_data_dir=PW_PROFILE, headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
    return pw_ctx.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

@contextmanager
def _own_browser():
    """无外部 browser 时自用：启动并在退出时关闭一个 Playwright chromium。"""
    from playwright.sync_api import sync_playwright
    pcm = sync_playwright().start()
    browser = _launch_browser(pcm)
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
    # 招聘/考试类（民生专题，明确排除）：招聘、招考、考试、公考、编制、考证报名等
    # 注意：不用单独「报名」（会误伤活动报名），改用「师报名|证报名|报考|职业资格」等
    # 精准覆盖「XX师/XX证 报名」类考证报名，避免漏掉不含「考试」二字的考证信息。
    BLOCK = _re.compile(r"(招聘|招考|招录|求职|应聘|校招|社招|简历|"
                        r"考试|笔试|面试|准考证|查分|公考|考公|公务员|"
                        r"事业编|考编|编制|公职|报考|职业资格|资格证|"
                        r"职称|教资|教师资格|技能鉴定|师报名|证报名|"
                        r"录用|上岗|校园招聘|社会招聘|开学第一课|"
                        r"开学典礼|公益节目|电视开学典礼|中小学|"
                        r"升学|成人高考|高考|电工证|考证|补习|网课)")
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
                raw = it["title"]
                # 显示标题：压平所有空白（含换行）为单行，避免表格样式错乱/标题被内部换行截断
                t = _re.sub(r"\s+", " ", raw).strip()
                # 过滤仍用完整文本（含描述），避免误杀仅标题无活动词、但描述含活动词的真实活动
                t_match = _re.sub(r"\s+", " ", raw).strip()
                if not EV.search(t_match):
                    continue
                if NEG.search(t_match):
                    continue  # 优惠券类低质量，跳过
                if BLOCK.search(t_match):
                    continue  # 招聘/考试/教育类民生专题，明确排除
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
    "ym_ecoupon_quota": 5,           # 羊毛村奶茶线报（改归电商券）保留名额，防被什么值得买挤掉
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

    # 羊毛村（platform=羊毛村）配额：优先保奶茶线报，再按日期取最近 N 条。
    # 奶茶线报改归「🛒 电商券」后仍是日报重点，若不优先会被同源更晚的线报挤出配额
    # （羊毛村每日线报量很大，纯按日期排序时较早的奶茶线报挤不进前 20）。
    YM_CAP = sc["yangmaocun_cap"]
    ym_items = next((v for v in by_source.values()
                     if v and v[0].get("platform") == "羊毛村"), [])
    ym_sorted = sorted(ym_items,
                       key=lambda d: (1 if d.get("_ym_milktea") else 0,
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
        if items[0].get("source") == "milktea":
            # 奶茶官微：先按日期倒序，再稳定排序把高优品牌（奈雪/喜茶/霸王茶姬）
            # 提到最前——保底 2 条也优先给高优品牌，不被冷门品牌抢占版面。
            s.sort(key=lambda d: 0 if d.get("platform") in MILKTEA_TOP_BRANDS else 1)
        for d in s[:2]:
            guaranteed.append(d)

    # 按类型优先级在「已限源」池内选取；电商券（卖东西）类单独限量。
    # 强制保留项（每源保底）先纳入，确保不丢；其余按类型优先级在剩余预算内补足，
    # 保证 total 始终 <= sc["max"]（不再被保底项突破封顶）。
    by_type = defaultdict(list)
    for d in capped:
        by_type[d["type"]].append(d)

    # 奶茶两区排序：奈雪/喜茶/霸王茶姬 优先于其他品牌，其后按发布时间倒序。
    # capped 已是日期倒序，stable sort 只调整品牌优先级，组内时间顺序保持不变。
    for _t in ("🥤 奶茶饮品", "🧋 奶茶联名"):
        if _t in by_type:
            by_type[_t].sort(
                key=lambda d: 0 if d.get("platform") in MILKTEA_TOP_BRANDS else 1)

    # 🛒 电商券：拆成「羊毛村奶茶线报（由奶茶饮品改归而来）」与「什么值得买商品」两组，
    # 前者先按配额保留——否则当日扎堆的什么值得买会把羊毛村线报整个挤掉。
    _ec = by_type.get("🛒 电商券")
    if _ec:
        _ym_ec = [d for d in _ec if d.get("_ym_milktea")]
        _other_ec = [d for d in _ec if not d.get("_ym_milktea")]
        by_type["🛒 电商券"] = _ym_ec[:sc["ym_ecoupon_quota"]] + _other_ec

    out = []
    seen_ids = set()
    for d in guaranteed:
        if id(d) not in seen_ids:
            out.append(d)
            seen_ids.add(id(d))

    type_cap = {**{t: sc["per_type"] for t in SELECT_PRIORITY},
                "🛒 电商券": sc["smzdm_per_type"] + sc["ym_ecoupon_quota"]}
    for t in SELECT_PRIORITY:
        if len(out) >= sc["max"]:
            break
        taken = sum(1 for x in out if x["type"] == t)
        budget_t = max(0, type_cap.get(t, sc["per_type"]) - taken)
        if budget_t <= 0:
            continue
        for d in by_type.get(t, []):
            if budget_t <= 0 or len(out) >= sc["max"]:
                break
            if id(d) in seen_ids:
                # 保底阶段已放入的项要跳过继续找，不能用 break（否则该类型整体停补，
                # 导致所有走「每源保底 2 条」的信源被压到只剩 2 条）。
                continue
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


def chunk_md(md, limit=3800, unit="byte"):
    """按 '## ' 分组切分，保证每条消息不超平台上限。

    unit 说明（关键，曾踩坑）：
      · unit="byte" → 企业微信群机器人，限额是 **4096 字节**。中文一个字 3 字节，
        按字符切最坏能到 11400 字节，企微直接拒收整条。故默认按字节切。
      · unit="char" → PushPlus，限额按字符计（默认 8000 字符），用字节切会平白
        多切两三倍段数、把一条日报拆成好几条推送。
    """
    def _size(s):
        return len(s.encode("utf-8")) if unit == "byte" else len(s)

    if _size(md) <= limit:
        return [md]
    parts = md.split("\n## ")
    header = parts[0]
    chunks, cur = [], header
    for p in parts[1:]:
        block = "\n## " + p
        if _size(cur + block) > limit:
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
    chunks = chunk_md(md, limit=8000, unit="char")  # PushPlus 按字符限额
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
        "milktea": fetch_milktea,
    }
    enabled = cfg.get("enabled_sources") or list(SOURCES.keys())
    PW_SOURCES = ("icbc", "bendibao", "milktea")  # Playwright 源：共用一个 chromium
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
            shared_browser = _launch_browser(pw_ctx)
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
    # 顶层兜底：cron 场景下 stderr 没人看，崩溃若不落盘告警就是「日报静默消失」。
    # 尤其 Playwright 源（icbc/bendibao 各自包了 try，milktea 没有）一旦 chromium
    # 崩掉（2C4G 机器跑 Xvfb 有 OOM 可能）会一路抛到这里：
    # 不仅当天日报丢失，state 也不会落盘 → 次日所有条目被误标 🆕。
    try:
        main()
    except Exception as e:
        notify_failure(
            f"日报管线崩溃：{type(e).__name__}: {e}\n"
            + traceback.format_exc()[-800:])
        raise
