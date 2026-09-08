# -*- coding: utf-8 -*-
"""配置完整性测试：datasets/models 注册表必填键与可组装性。"""
import unittest

import configs.datasets as datasets
import configs.models as models


class ConfigCompletenessTests(unittest.TestCase):
    def test_laoke_and_new_have_required_dataset_keys(self):
        for key, cfg in datasets.DATASETS.items():
            with self.subTest(key=key):
                for required in datasets.REQUIRED_DATASET_KEYS:
                    self.assertIn(required, cfg, f"数据集 {key} 缺少必填键: {required}")

    def test_models_have_required_keys(self):
        for key, cfg in models.MODELS.items():
            with self.subTest(key=key):
                for required in models.REQUIRED_MODEL_KEYS:
                    self.assertIn(required, cfg, f"模型 {key} 缺少必填键: {required}")

    def test_model_strategy_constraints_have_expected_keys(self):
        for key, cfg in models.MODELS.items():
            with self.subTest(key=key):
                sc = cfg["strategy_config"]
                self.assertIn("auto_constraints", sc)
                self.assertIn("accept_constraints", sc)

    def test_laoke_mlt_worthiness_combo_is_registered(self):
        # 老客 mlt × 价值模型 是历史验证过的组合，报告前缀需已登记。
        from scripts.cross_models import REPORT_PREFIXES

        self.assertIn(("laoke", "mlt", "worthiness", "matrix"), REPORT_PREFIXES)

    def test_report_meta_schema(self):
        # 报告生成器可选字段校验：登记了 report_meta 的模型必须有 report_name/md_name；
        # y_interest 必须含 raw_col/threshold；引用 key 必须存在。
        for key, cfg in models.MODELS.items():
            meta = cfg.get("report_meta")
            if meta is None:
                continue
            with self.subTest(model=key):
                self.assertIn("report_name", meta, f"模型 {key} report_meta 缺少 report_name")
                self.assertIn("md_name", meta, f"模型 {key} report_meta 缺少 md_name")
                if "y_interest" in meta:
                    for field in ("raw_col", "threshold"):
                        self.assertIn(field, meta["y_interest"], f"模型 {key} y_interest 缺少 {field}")

        for key, cfg in datasets.DATASETS.items():
            meta = cfg.get("report_meta")
            if meta is None:
                continue
            with self.subTest(dataset=key):
                for model_key in meta.get("models", []):
                    self.assertIn(model_key, models.MODELS, f"数据集 {key} report_meta 引用未登记模型 {model_key}")
                for pair in meta.get("cross_pairs", []):
                    self.assertIn(pair[0], models.MODELS, f"数据集 {key} cross_pairs 引用未登记模型 {pair[0]}")
                    self.assertIn(pair[1], models.MODELS, f"数据集 {key} cross_pairs 引用未登记模型 {pair[1]}")


if __name__ == "__main__":
    unittest.main()
