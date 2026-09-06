# -*- coding: utf-8 -*-
"""报告生成器参数化接口测试：Excel 定位、md 命名与上下文组装。"""
import unittest

import scr._gen_new_reports as gen


class ResolveXlsxTests(unittest.TestCase):
    def test_exact_date(self):
        path = gen.resolve_xlsx("binning_new_mlt_strategy_report", "20260901")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "binning_new_mlt_strategy_report_20260901.xlsx")

    def test_latest_wins(self):
        path = gen.resolve_xlsx("binning_new_mlt_strategy_report", None)
        self.assertRegex(path.name, r"^binning_new_mlt_strategy_report_\d{8}\.xlsx$")

    def test_missing_date_raises(self):
        with self.assertRaises(ValueError):
            gen.resolve_xlsx("binning_new_mlt_strategy_report", "19990101")

    def test_unknown_prefix_raises(self):
        with self.assertRaises(ValueError):
            gen.resolve_xlsx("no_such_prefix", None)


class ContextAndNamingTests(unittest.TestCase):
    def test_build_context_new(self):
        ctx = gen.build_context("new", "new_mlt", "new_worthiness", date="20260901")
        self.assertEqual(ctx.date, "20260901")
        self.assertEqual(ctx.xlsx_a.name, "binning_new_mlt_strategy_report_20260901.xlsx")
        self.assertEqual(ctx.xlsx_b.name, "binning_new_worthiness_strategy_report_20260901.xlsx")
        self.assertEqual(ctx.cross_xlsx.name, "binning_new_cross_strategy_report_20260901.xlsx")

    def test_build_context_date_from_file(self):
        ctx = gen.build_context("new", "new_mlt", date=None)
        self.assertEqual(ctx.date, "20260901")

    def test_resolve_md_path_names(self):
        ctx = gen.build_context("new", "new_mlt", "new_worthiness", date="20260901")
        self.assertEqual(gen.resolve_md_path(ctx, "single_a").name, "分箱_新客_mlt_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "single_b").name, "分箱_新客_价值_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "cross").name, "交叉_新客_mlt_价值.md")

    def test_resolve_md_path_laoke(self):
        # laoke 交叉 Excel 最新为 20260904，date 缺省走 glob 最新。
        ctx = gen.build_context("laoke", "mlt", "worthiness")
        self.assertEqual(gen.resolve_md_path(ctx, "single_a").name, "分箱_老客_mlt_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "cross").name, "交叉_老客_mlt_价值.md")
        self.assertEqual(ctx.cross_xlsx.name, "binning_cross_strategy_report_20260904.xlsx")


if __name__ == "__main__":
    unittest.main()
