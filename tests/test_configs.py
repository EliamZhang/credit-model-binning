# -*- coding: utf-8 -*-
"""配置完整性测试：datasets/models 注册表必填键与可组装性。"""
import unittest

import configs.datasets as datasets
import configs.models as models


class ConfigCompletenessTests(unittest.TestCase):
    def test_laoke_and_xinke_have_required_dataset_keys(self):
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
        self.assertIn(("laoke", "mlt", "worthiness", "cond"), REPORT_PREFIXES)


if __name__ == "__main__":
    unittest.main()
