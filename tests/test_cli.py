# -*- coding: utf-8 -*-
"""入口脚本 CLI 冒烟测试（不跑管线，只验证参数解析与配置解析）。"""
import unittest

from scripts import bin_model, cross_models


class CliSmokeTests(unittest.TestCase):
    def test_bin_model_default_args(self):
        args = bin_model.build_parser().parse_args([])
        self.assertEqual((args.dataset, args.model, args.metric), ("laoke", "mlt", "cnt"))

    def test_bin_model_invalid_model_rejected(self):
        with self.assertRaises(SystemExit):
            bin_model.build_parser().parse_args(["--model", "not_a_model"])

    def test_cross_models_argparse(self):
        with self.assertRaises(SystemExit):
            cross_models.build_parser().parse_args(["--mode", "bogus"])

    def test_resolve_report_paths_keep_historic_prefixes(self):
        path = bin_model.resolve_report_path("laoke", "mlt", "cnt")
        self.assertEqual(path.stem.rsplit("_", 1)[0], "binning_strategy_report")
        path_amt = bin_model.resolve_report_path("laoke", "mlt", "amt")
        self.assertEqual(path_amt.stem.rsplit("_", 1)[0], "binning_amt_strategy_report")
        path_wth = bin_model.resolve_report_path("laoke", "worthiness", "cnt")
        self.assertEqual(path_wth.stem.rsplit("_", 1)[0], "binning_worthiness_strategy_report")
        path_cross = cross_models.resolve_report_path("laoke", "mlt", "worthiness", "matrix")
        self.assertEqual(path_cross.stem.rsplit("_", 1)[0], "binning_cross_strategy_report")


if __name__ == "__main__":
    unittest.main()
