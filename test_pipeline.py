#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py 单元测试（纯函数层）。运行：python test_pipeline.py"""
import datetime as _dt
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
