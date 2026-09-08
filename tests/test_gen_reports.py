# -*- coding: utf-8 -*-
"""报告生成器参数化接口测试：Excel 定位、md 命名与上下文组装。

注意：out/ 的 Excel 会随干净房重跑整体换日期，测试一律从 resolve_xlsx 现取
最新文件日期，不固化具体日期（防止重跑后测试失效）。
"""
import re
import unittest

import scr._gen_new_reports as gen


def latest_date(prefix: str) -> str:
    """取 out/{prefix}_*.xlsx 最新文件的日期。"""
    return re.match(r".*_(\d{8})\.xlsx$", gen.resolve_xlsx(prefix, None).name).group(1)


class ResolveXlsxTests(unittest.TestCase):
    def test_exact_date(self):
        d = latest_date("binning_new_mlt_strategy_report")
        path = gen.resolve_xlsx("binning_new_mlt_strategy_report", d)
        self.assertTrue(path.exists())
        self.assertEqual(path, gen.resolve_xlsx("binning_new_mlt_strategy_report", None))

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
        # 缺省日期：cross_xlsx 取最新交叉 Excel，单模型 Excel 被锚定到不晚于交叉日期的
        # 最新文件（交叉重算边界一致性），ctx.date 回填为 xlsx_a 的日期。
        ctx = gen.build_context("new", "new_mlt", "new_worthiness")
        cross = gen.resolve_xlsx("binning_new_cross_strategy_report", None)
        self.assertEqual(ctx.cross_xlsx, cross)
        cross_date = latest_date("binning_new_cross_strategy_report")
        for xlsx, prefix in (
            (ctx.xlsx_a, "binning_new_mlt_strategy_report"),
            (ctx.xlsx_b, "binning_new_worthiness_strategy_report"),
        ):
            d = re.match(r".*_(\d{8})\.xlsx$", xlsx.name).group(1)
            self.assertLessEqual(d, cross_date)
            self.assertTrue(gen.resolve_xlsx(prefix, d).exists())
        self.assertEqual(ctx.date, re.match(r".*_(\d{8})\.xlsx$", ctx.xlsx_a.name).group(1))

    def test_build_context_date_from_file(self):
        # date 缺省时按 xlsx_a 实际解析到的日期回填。
        ctx = gen.build_context("new", "new_mlt", date=None)
        expected = latest_date("binning_new_mlt_strategy_report")
        self.assertEqual(ctx.date, expected)
        self.assertEqual(ctx.xlsx_a.name, f"binning_new_mlt_strategy_report_{expected}.xlsx")

    def test_resolve_md_path_names(self):
        ctx = gen.build_context("new", "new_mlt", "new_worthiness")
        self.assertEqual(gen.resolve_md_path(ctx, "single_a").name, "分箱_新客_mlt_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "single_b").name, "分箱_新客_价值_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "cross").name, "交叉_新客_mlt_价值.md")

    def test_resolve_md_path_laoke(self):
        # laoke 交叉 Excel 日期随重跑变化，缺省走 glob 最新。
        ctx = gen.build_context("laoke", "mlt", "worthiness")
        self.assertEqual(gen.resolve_md_path(ctx, "single_a").name, "分箱_老客_mlt_笔数.md")
        self.assertEqual(gen.resolve_md_path(ctx, "cross").name, "交叉_老客_mlt_价值.md")
        self.assertEqual(ctx.cross_xlsx.name, gen.resolve_xlsx("binning_cross_strategy_report", None).name)


if __name__ == "__main__":
    unittest.main()
