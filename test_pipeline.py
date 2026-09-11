#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py 单元测试（纯函数层）。运行：python test_pipeline.py"""
import datetime as _dt
import re
import unittest

import pipeline as P


class TestClassify(unittest.TestCase):
    def test_tea(self):
        # 「🥤 奶茶饮品」只由微博官号（source=milktea）供内容
        self.assertEqual(
            P.classify({"title": "喜茶买一送一", "detail": "", "source": "milktea"}),
            "🥤 奶茶饮品")

    def test_tea_non_official_not_drink(self):
        # 非官号命中奶茶词不得进「🥤 奶茶饮品」区（如什么值得买「加多宝凉茶」）
        self.assertNotEqual(
            P.classify({"title": "加多宝凉茶", "detail": "", "source": "smzdm"}),
            "🥤 奶茶饮品")

    def test_yangmaocun_milktea_to_ecoupon(self):
        # 羊毛村奶茶线报改归「🛒 电商券」，并打上配额保护标记
        d = {"title": "瑞幸咖啡免费抽1万份饮品免单", "detail": "", "source": "ym2.cc"}
        self.assertEqual(P.classify(d), "🛒 电商券")
        self.assertTrue(d.get("_ym_milktea"))

    def test_force_type(self):
        self.assertEqual(
            P.classify({"title": "x", "detail": "", "_force_type": "🎟️ 深圳活动"}),
            "🎟️ 深圳活动")

    def test_food_before_pay(self):
        # 餐饮应在支付之前：避免 "肯德基 红包" 被误归为支付立减
        self.assertEqual(P.classify({"title": "肯德基 红包", "detail": ""}),
                         "🍜 餐饮美食")

    def test_smzdm_default_ecommerce(self):
        self.assertEqual(
            P.classify({"title": "随便什么", "detail": "", "source": "smzdm"}),
            "🛒 电商券")


class TestMilkteaGates(unittest.TestCase):
    """奶茶官微闸门：emoji 数字归一化 + 抽奖负向闸门（口径：抽不到我的不要）。"""

    @classmethod
    def setUpClass(cls):
        cfg = P.get_milktea_cfg()
        cls.deal = cfg["_deal_re"]
        cls.lot = cfg["_lottery_re"]

    def test_norm_emoji_digits(self):
        # 官微常把「买一送一」写成「买1️⃣送1️⃣」，必须等价于「买1送1」，
        # 否则整条会被价值闸门丢掉（霸王茶姬 2026-09-11 那条即如此漏抓）。
        self.assertEqual(P._norm_text("买1️⃣送1️⃣"), "买1送1")
        self.assertTrue(self.deal.search(P._norm_text("买1️⃣送1️⃣")))

    def test_norm_fullwidth_digits(self):
        self.assertEqual(P._norm_text("９月１１日"), "9月11日")

    def test_lottery_killed(self):
        for c in ("评论区揪5️⃣位朋友送奈雪30元福利券",
                  "【转+关】9月12日请喝30杯「奶麻薯新品」",
                  "微博官方唯一抽奖工具 @微博抽奖平台 对本次抽奖进行监督"):
            self.assertTrue(self.lot.search(P._norm_text(c)), c)

    def test_real_deal_not_killed(self):
        # 确定可得的正羊毛不得被抽奖闸门误杀
        for c in ("6000张新品免单券掉落",
                  "小马管家请大家0元喝瑞幸啦",
                  "明天，霸王茶姬全场饮品（含geelato），买1️⃣送1️⃣",
                  "霸王茶姬联名迪士尼公主轻因系列，第二波周边今日上线"):
            self.assertFalse(self.lot.search(P._norm_text(c)), c)

    def test_verdict_link_survives_lottery(self):
        # 官微的联名公告几乎都带「关注+转发抽N位」促互落款，抽奖闸门不得连坐整条联名。
        # 回归：奈雪×明日方舟终末地(9/11)、奈雪×豚豚崽(9/10) 两条联名曾因此被清空，
        # 表现为「奈雪没有联动」的假象。
        for c in ("奈雪×@明日方舟终末地 联名活动，9月23日正式上线！关注并转发，抽20位管理员喝联名茶饮",
                  "与豚豚崽一起解锁松弛～关注＋转发，揪5位朋友送全套萌物周边！联名蔬果酸奶昔"):
            ok, ftype, hit = P._milktea_verdict(c, self.deal, self.lot)
            self.assertTrue(ok, c)
            self.assertEqual(ftype, "🧋 奶茶联名", c)
            self.assertEqual(hit, "联名", c)  # 命中词须显示联动信号，不显示抽奖落款

    def test_verdict_pure_lottery_dropped(self):
        # 纯抽奖（要中奖才拿得到）仍须丢弃 —— 用户口径：根本抽不到我。
        for c in ("评论区揪5位朋友送奈雪30元福利券",
                  "评论区抽10位朋友喝「霸气小红杏酸奶冰」",
                  "北京地区请喝30杯「奶麻薯新品」"):
            ok, _, _ = P._milktea_verdict(c, self.deal, self.lot)
            self.assertFalse(ok, c)

    def test_verdict_new_product_dropped(self):
        # 纯上新没有羊毛价值（用户明确：上新这种对我没有意义）。
        for c in ("奈雪秋日特调「400次金桂米酿奶咖」，今日正式上线！400次现打咸芝酪…",
                  "奈雪法式佛卡夏上新啦！「多谷物菌菇火腿佛卡夏」热烤出炉，料多满足"):
            ok, _, _ = P._milktea_verdict(c, self.deal, self.lot)
            self.assertFalse(ok, c)

    def test_verdict_x_form_is_link(self):
        # 「A × B」形态：正文可能通篇没有「联名」二字（只有奈雪×豚豚崽这类写法），
        # 此时若不靠 × 识别，就会落到抽奖闸门被整条丢掉 —— 联名豁免形同虚设。
        for c in ("茶百道 ×《天官赐福》动画 9月12日10:00起正式开启",
                  "奈雪× @明日方舟终末地 9月23日正式上线！关注并转发，抽20位"):
            ok, ftype, hit = P._milktea_verdict(c, self.deal, self.lot)
            self.assertTrue(ok, c)
            self.assertEqual(ftype, "🧋 奶茶联名", c)
            self.assertEqual(hit, "联名×", c)  # 残缺片段须归一化为可读的联动信号

    def test_verdict_x_not_math(self):
        # 数字乘法/规格写法不得被当成联名（× 两侧须是「名字」字符）
        for c in ("整箱规格 500ml×2，限时特价", "满减叠加：2×3瓶更划算"):
            _, ftype, _ = P._milktea_verdict(c, self.deal, self.lot)
            self.assertNotEqual(ftype, "🧋 奶茶联名", c)

    def test_verdict_other_link_words(self):
        for c in ("古茗跨界合作，敦煌研究院主题杯套上线",
                  "瑞幸联合出品《时光代理人》主题杯"):
            ok, ftype, hit = P._milktea_verdict(c, self.deal, self.lot)
            self.assertTrue(ok, c)
            self.assertEqual(ftype, "🧋 奶茶联名", c)
            self.assertIn(hit, ("跨界", "联合出品"), c)

    def test_verdict_charity_not_link(self):
        # 公益合作不是联名：裸「携手/合作/联合」刻意不收，避免给联名区灌水
        for c in ("奈雪携手中国乡村发展基金会，捐赠100万元助力乡村儿童",
                  "霸王茶姬联合公益机构发起环保行动"):
            _, ftype, _ = P._milktea_verdict(c, self.deal, self.lot)
            self.assertNotEqual(ftype, "🧋 奶茶联名", c)

    def test_verdict_drink_section(self):
        ok, ftype, hit = P._milktea_verdict(
            "凭学生证认证【霸气学生卡】可得招牌饮品第2件半价券*1", self.deal, self.lot)
        self.assertTrue(ok)
        self.assertEqual(ftype, "🥤 奶茶饮品")

    def test_title_skips_lottery_tail(self):
        # 联名公告末尾常挂「关注＋转发，揪N位送周边」，标题须落在含品牌名的正文句，
        # 不能落在抽奖落款上（否则读者误以为这条只是抽奖）。
        brand_re = re.compile(r"奈雪的茶|奈雪")
        txt = ("豚式生活，自然「奈」么好！9月10日，来奈雪与豚豚崽一起解锁松弛～ "
               "✨关注＋转发，揪5位朋友送全套萌物周边！🥤联名蔬果酸奶昔：超能牛油果酸奶昔")
        t = P._pick_title(P._norm_text(txt), brand_re, self.deal, self.lot)
        self.assertNotIn("揪", t)
        self.assertIn("豚豚崽", t)

    def test_title_prefers_link_sentence(self):
        # 品牌名与联名词同句时优先选该句（活动正文，而非产品列表行）。
        brand_re = re.compile(r"奈雪的茶|奈雪")
        txt = ("亲爱的管理员：属于你的下午茶补给，即将送达。奈雪× @明日方舟终末地 联名活动，"
               "9月23日正式上线！关注 @奈雪的茶 并转发，抽20位管理员喝联名茶饮")
        t = P._pick_title(P._norm_text(txt), brand_re, self.deal, self.lot)
        self.assertIn("明日方舟", t)
        self.assertNotIn("抽", t)


class TestNormDate(unittest.TestCase):
    def test_cn(self):
        self.assertEqual(P.norm_date("2026年8月1日")[0], "2026-08-01")

    def test_abs(self):
        self.assertEqual(P.norm_date("2026-08-12")[0], "2026-08-12")

    def test_md(self):
        self.assertEqual(P.norm_date("8月12日")[0],
                         f"{_dt.date.today().year}-08-12")

    def test_rel(self):
        self.assertEqual(
            P.norm_date("3天前")[0],
            (_dt.date.today() - _dt.timedelta(days=3)).isoformat())

    def test_invalid(self):
        self.assertEqual(P.norm_date("无日期")[0], "")


class TestNormDateUrl(unittest.TestCase):
    def test_url_seg(self):
        self.assertEqual(P.norm_date_url("https://x.com/news/2026812/"),
                         "2026-08-12")

    def test_no_seg(self):
        self.assertEqual(P.norm_date_url("https://x.com/abc"), "")


class TestPruneSeen(unittest.TestCase):
    def test_prune(self):
        seen = {"a": "2020-01-01", "b": _dt.date.today().isoformat()}
        out = P.prune_seen(seen, 7)
        self.assertNotIn("a", out)
        self.assertIn("b", out)

    def test_keep_days_zero(self):
        seen = {"a": "2020-01-01"}
        self.assertEqual(P.prune_seen(seen, 0), seen)  # 不裁剪


class TestSelectDeals(unittest.TestCase):
    def _deal(self, platform, source, typ, date):
        return {"platform": platform, "source": source, "type": typ, "date": date}

    def test_total_cap(self):
        # 大量"其他"类，验证总量不超过 max（封顶不再被保底项突破）
        today = _dt.date.today().isoformat()
        deals = [self._deal("羊毛村", "ym2.cc", "📦 其他", today)
                 for _ in range(100)]
        out = P.select_deals(deals, 30)
        self.assertLessEqual(len(out), P.get_select_cfg()["max"])

    def test_guaranteed_kept(self):
        # 非羊毛村/非smzdm 源至少保留 2 条
        today = _dt.date.today().isoformat()
        deals = [self._deal("55信用卡", "55card.cn", "💰 支付立减", today)
                 for _ in range(5)]
        out = P.select_deals(deals, 30)
        self.assertGreaterEqual(len(out), 2)

    def test_yangmaocun_old_dropped(self):
        # 羊毛村无日期/超龄线报应被剔除
        old = (_dt.date.today() - _dt.timedelta(days=20)).isoformat()
        deals = [self._deal("羊毛村", "ym2.cc", "🥤 奶茶饮品", old),
                 self._deal("羊毛村", "ym2.cc", "🥤 奶茶饮品", "")]
        out = P.select_deals(deals, 30)
        self.assertEqual(len(out), 0)


class TestDedupSameEvent(unittest.TestCase):
    """官微「同一活动只留一条」：判据是品牌+联名对象专名，不是 url（同一活动是多条微博）。"""

    @staticmethod
    def _mk(platform, title, date, mid="1", source="milktea",
            ttype="🧋 奶茶联名"):
        return {"platform": platform, "source": source, "type": ttype,
                "title": title, "url": f"https://m.weibo.cn/status/{mid}",
                "date": date, "confidence": "🟢"}

    def test_three_posts_same_event(self):
        # 实测：茶百道 ×《天官赐福》一天发 3 条，标题/url 都不同，make_hash 全放过
        deals = [
            self._mk("茶百道", "#茶百道# #茶百道联名天官赐福# #茶百道联名# "
                     "#茶百道咖啡# 茶百道ChaPanda的微博视频", "2026-09-11", "3"),
            self._mk("茶百道", "9月12日起，茶百道 ×《天官赐福》动画联名 旗舰店同步上线",
                     "2026-09-11", "2"),
            self._mk("茶百道", "茶百道 ×《天官赐福》动画联名 9月12日10:00起正式开启",
                     "2026-09-11", "1"),
        ]
        out = P.dedup_same_event(deals)
        self.assertEqual(len(out), 1)
        self.assertIn("天官赐福", out[0]["title"])
        # 时间戳最大的那条恰是纯话题标签堆砌的视频帖，不能留它
        self.assertNotIn("微博视频", out[0]["title"])

    def test_cross_date_keeps_latest(self):
        # 霸王茶姬 × 迪士尼公主跨 09-07/09-09 两波：只保留最新进展
        deals = [
            self._mk("霸王茶姬", "霸王茶姬联名迪士尼公主轻因系列，第二波周边今日",
                     "2026-09-09"),
            self._mk("霸王茶姬", "一种很新的po图方式～ 美到心动的 "
                     "#霸王茶姬迪士尼公主联名# 更低咖啡因*，轻因*不扰眠 下午想喝",
                     "2026-09-07"),
        ]
        out = P.dedup_same_event(deals)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], "2026-09-09")

    def test_different_ip_kept(self):
        # 同品牌不同联名对象不得合并
        deals = [
            self._mk("奈雪的茶", "奈雪× @明日方舟终末地 联名活动，9月23日正式上线",
                     "2026-09-11"),
            self._mk("奈雪的茶", "9月10日，来奈雪与豚豚崽一起解锁松弛～", "2026-09-10"),
        ]
        self.assertEqual(len(P.dedup_same_event(deals)), 2)

    def test_other_source_untouched(self):
        # 二手源不动：羊毛村「活动帖」与「领取攻略」各有价值
        deals = [
            self._mk("羊毛村", "瑞幸咖啡免费抽1万份饮品免单", "2026-09-09",
                     source="ym2.cc", ttype="🛒 电商券"),
            self._mk("羊毛村", "Marvis-0元喝瑞幸｜电脑端详细领取攻略", "2026-09-09",
                     source="ym2.cc", ttype="🛒 电商券"),
        ]
        self.assertEqual(len(P.dedup_same_event(deals)), 2)

    def test_no_event_key_kept(self):
        # 提取不到专名时不参与合并（保守，宁可多留不可错合）
        deals = [
            self._mk("古茗茶饮", "秋日新品温暖上市，欢迎品尝", "2026-09-11"),
            self._mk("古茗茶饮", "门店装修公告", "2026-09-11"),
        ]
        self.assertEqual(len(P.dedup_same_event(deals)), 2)


class TestEndDate(unittest.TestCase):
    """银行活动的「截止日」不是发布日：存 date_end，展示「至MM-DD」且不占今天的位置。"""

    def test_pub_label_shows_end_date(self):
        d = {"date": "2026-09-12", "date_end": "2026-12-31"}
        self.assertEqual(P.pub_label(d), "至12-31")

    def test_pub_label_normal(self):
        today = _dt.date.today()
        d = {"date": today.isoformat()}
        self.assertEqual(P.pub_label(d), f"{today.isoformat()[5:]} · 今天")

    def test_pub_label_no_date(self):
        self.assertEqual(P.pub_label({}), "— · 日期未知")

    def test_end_date_not_shown_as_today(self):
        # 回归：早前未来日期会被 age_label 判成「今天」，显示成「12-31 · 今天」
        d = {"date": "2026-12-31"}
        self.assertEqual(P.age_label(d), "今天")          # 旧行为（仅 age_label 层）
        d2 = {"date": _dt.date.today().isoformat(), "date_end": "2026-12-31"}
        self.assertNotIn("今天", P.pub_label(d2))          # 新行为：不再冒充今天

    def test_end_date_sinks_in_sort(self):
        # 同日期下，带截止日的长期活动应排在今天真新闻之后，不抢版面
        today = _dt.date.today().isoformat()
        news = {"platform": "X", "source": "s", "title": "今日真新闻", "url": "a",
                "date": today, "type": "📦 其他", "confidence": "🟢"}
        long_run = {"platform": "工商银行", "source": "icbc", "title": "长期活动",
                    "url": "b", "date": today, "date_end": "2026-12-31",
                    "type": "💰 支付立减", "confidence": "🟢"}
        out = P.select_deals([long_run, news], 30)
        self.assertEqual([d["title"] for d in out], ["今日真新闻", "长期活动"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
