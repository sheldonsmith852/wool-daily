#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py 单元测试（纯函数层）。运行：python test_pipeline.py"""
import datetime as _dt
import unittest

import pipeline as P


class TestClassify(unittest.TestCase):
    def test_tea(self):
        self.assertEqual(P.classify({"title": "喜茶买一送一", "detail": ""}),
                         "🥤 奶茶饮品")

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
