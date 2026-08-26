import unittest

import pandas as pd

import binning_mlt_cnt as binning


class FunnelMetricTests(unittest.TestCase):
    def test_actual_funnel_uses_distinct_application_ids_and_business_statuses(self):
        data = pd.DataFrame(
            {
                "application_id": [1, 2, 3, 4, 5, 6, 6],
                "application_status": [
                    "0.Incomplete",
                    "2.3.Risk Declined",
                    "4.Funded",
                    "3.1.Approved Withdrawn",
                    "3.3.Conversion Declined",
                    "2.1.Submitted Withdrawn",
                    "2.1.Submitted Withdrawn",
                ],
                "assessment_status": [
                    "0.Incomplete",
                    "2.3.Auto Declined",
                    "4.Auto Approved Auto Funded",
                    "3.1.Manual Approved Auto Withdrawn",
                    "3.3.Manual Approved Manual Conversion Declined",
                    "2.1.Auto Withdrawn",
                    "2.1.Auto Withdrawn",
                ],
                "status": [
                    "Trashed",
                    "Declined",
                    "Active_Account",
                    "Cancelled",
                    "Declined",
                    "Cancelled",
                    "Cancelled",
                ],
            }
        )

        result = binning._actual_funnel_row(data, "Test")

        self.assertEqual(result["actual_apply_cnt"], 6)
        self.assertEqual(result["actual_completed_application_cnt"], 5)
        self.assertEqual(result["actual_approved_application_cnt"], 3)
        self.assertEqual(result["actual_auto_approved_application_cnt"], 1)
        self.assertEqual(result["actual_manual_approved_application_cnt"], 2)
        self.assertEqual(result["actual_deal_sample_cnt"], 1)
        self.assertAlmostEqual(result["actual_completion_rate"], 5 / 6)
        self.assertAlmostEqual(result["actual_approval_rate"], 3 / 5)
        self.assertAlmostEqual(result["actual_auto_approval_rate"], 1 / 5)
        self.assertAlmostEqual(result["actual_manual_approval_rate"], 2 / 5)
        self.assertAlmostEqual(result["actual_auto_approval_share"], 1 / 3)
        self.assertAlmostEqual(result["actual_manual_approval_share"], 2 / 3)
        self.assertAlmostEqual(result["actual_deal_rate"], 1 / 3)

    def test_strategy_estimated_flow_is_kept_separate_from_actual_funnel(self):
        train = pd.DataFrame(
            {
                "application_id": [1, 2, 3, 4],
                binning.SCORE_COL: [0.1, 0.2, 0.3, 0.4],
            }
        )
        oot = pd.DataFrame(
            {
                "application_id": [5, 6],
                binning.SCORE_COL: [0.15, 0.35],
            }
        )
        strategy_plan = pd.DataFrame(
            [
                {
                    "status": "OK",
                    "auto_pass_threshold": 0.2,
                    "reject_threshold": 0.3,
                }
            ]
        )

        result = binning.build_strategy_estimated_flow_report(
            train,
            oot,
            strategy_plan,
        ).set_index("sample_group")

        self.assertAlmostEqual(
            result.loc["Train", "strategy_estimated_auto_pass_rate"],
            0.5,
        )
        self.assertAlmostEqual(
            result.loc["Train", "strategy_estimated_manual_review_rate"],
            0.25,
        )
        self.assertAlmostEqual(
            result.loc["Train", "strategy_estimated_total_accept_rate"],
            0.75,
        )
        self.assertAlmostEqual(
            result.loc["Train", "strategy_estimated_reject_rate"],
            0.25,
        )
        self.assertAlmostEqual(
            result.loc["OOT", "strategy_estimated_auto_pass_rate"],
            0.5,
        )
        self.assertAlmostEqual(
            result.loc["OOT", "strategy_estimated_reject_rate"],
            0.5,
        )

    def test_actual_funnel_can_be_drilled_down_by_final_bin(self):
        data = pd.DataFrame(
            {
                "application_id": [1, 2, 3, 4],
                binning.FINAL_BIN_COL: ["A", "A", "B", "B"],
                "bin_order": [1, 1, 2, 2],
                "application_status": [
                    "4.Funded",
                    "2.3.Risk Declined",
                    "3.1.Approved Withdrawn",
                    "0.Incomplete",
                ],
                "assessment_status": [
                    "4.Auto Approved Auto Funded",
                    "2.3.Auto Declined",
                    "3.1.Manual Approved Auto Withdrawn",
                    "0.Incomplete",
                ],
                "status": ["Active_Account", "Declined", "Cancelled", "Trashed"],
            }
        )

        result = binning.build_bin_actual_funnel_report(data).set_index(
            binning.FINAL_BIN_COL
        )

        self.assertAlmostEqual(result.loc["A", "actual_completion_rate"], 1.0)
        self.assertAlmostEqual(result.loc["A", "actual_approval_rate"], 0.5)
        self.assertAlmostEqual(result.loc["A", "actual_auto_approval_rate"], 0.5)
        self.assertAlmostEqual(result.loc["B", "actual_completion_rate"], 0.5)
        self.assertAlmostEqual(result.loc["B", "actual_manual_approval_rate"], 1.0)

    def test_drop_incomplete_applications_removes_incomplete_only(self):
        data = pd.DataFrame(
            {
                "application_id": [1, 2, 3, 4],
                "application_status": [
                    "0.Incomplete",
                    "4.Funded",
                    "1.In Progress",
                    None,
                ],
            }
        )

        result = binning.drop_incomplete_applications(data)

        self.assertEqual(result["application_id"].tolist(), [2, 4])

    def test_bin_model_diagnostics_reconcile_to_iv(self):
        stats = pd.DataFrame(
            {
                "bin_order": [1, 2, 3],
                "1m30p_cnt_bad": [1, 3, 8],
                "1m30p_cnt_good": [20, 15, 10],
                "3m30p_cnt_bad": [2, 5, 10],
                "3m30p_cnt_good": [18, 14, 8],
            }
        )

        result = binning.add_bin_model_diagnostics(stats)

        expected_1m_iv = binning.calc_iv_from_stats(
            stats,
            bad_col="1m30p_cnt_bad",
            good_col="1m30p_cnt_good",
        )
        expected_3m_iv = binning.calc_iv_from_stats(
            stats,
            bad_col="3m30p_cnt_bad",
            good_col="3m30p_cnt_good",
        )
        self.assertAlmostEqual(result["1m30p_iv_component"].sum(), expected_1m_iv)
        self.assertAlmostEqual(result["3m30p_iv_component"].sum(), expected_3m_iv)
        self.assertTrue(result["1m30p_ks_curve"].between(0, 1).all())
        self.assertTrue(result["3m30p_ks_curve"].between(0, 1).all())

    def test_bin_lift_is_bin_rate_over_sample_group_overall(self):
        stats = pd.DataFrame(
            {
                "bin_order": [1, 2],
                "1m30p_cnt_mature": [100, 300],
                "1m30p_cnt_bad": [1, 6],
                "3m30p_cnt_mature": [200, 400],
                "3m30p_cnt_bad": [4, 12],
                "1m30p_amt_exposure": [10000, 30000],
                "1m30p_amt_bad": [50, 450],
                "3m30p_amt_exposure": [20000, 40000],
                "3m30p_amt_bad": [200, 1000],
            }
        )

        result = binning.add_bin_lift(stats)

        self.assertEqual(result["bin_order"].tolist(), [1, 2])
        overall = {
            "1m30p_cnt": 7 / 400,
            "3m30p_cnt": 16 / 600,
            "1m30p_amt": 500 / 40000,
            "3m30p_amt": 1200 / 60000,
        }
        rates = {
            "1m30p_cnt": [(1 / 100), (6 / 300)],
            "3m30p_cnt": [(4 / 200), (12 / 400)],
            "1m30p_amt": [(50 / 10000), (450 / 30000)],
            "3m30p_amt": [(200 / 20000), (1000 / 40000)],
        }
        for prefix, expected_overall in overall.items():
            for bin_idx, bin_rate in enumerate(rates[prefix]):
                self.assertAlmostEqual(
                    result[f"{prefix}_lift"].iloc[bin_idx],
                    bin_rate / expected_overall,
                )

    def test_bin_lift_overall_row_is_identity(self):
        stats = pd.DataFrame(
            {
                "bin_order": [1, 2, 0],
                "1m30p_cnt_mature": [100, 300, 400],
                "1m30p_cnt_bad": [1, 6, 7],
                "3m30p_cnt_mature": [200, 400, 600],
                "3m30p_cnt_bad": [4, 12, 16],
                "1m30p_amt_exposure": [10000, 30000, 40000],
                "1m30p_amt_bad": [50, 450, 500],
                "3m30p_amt_exposure": [20000, 40000, 60000],
                "3m30p_amt_bad": [200, 1000, 1200],
            }
        )

        result = binning.add_bin_lift(stats)

        self.assertEqual(result["bin_order"].tolist(), [0, 1, 2])
        self.assertAlmostEqual(result["1m30p_cnt_lift"].iloc[0], 1.0)
        self.assertAlmostEqual(result["3m30p_cnt_lift"].iloc[0], 1.0)
        self.assertAlmostEqual(result["1m30p_amt_lift"].iloc[0], 1.0)
        self.assertAlmostEqual(result["3m30p_amt_lift"].iloc[0], 1.0)


    def test_refine_ranges_under_share_cap_splits_overlarge_bin_and_remerges(self):
        stats = self._share_cap_stats()
        # (2,4)=45% 与 (5,6)=45% 均超 35% 上限：低风险侧优先拆分为 5 箱，
        # 再合并代价最低的可行相邻对（仅 (1,1)+(2,2) 合并后仍不超过上限）回到 4 档。
        result = binning.refine_ranges_under_share_cap(
            [(1, 1), (2, 4), (5, 6)],
            stats,
            protected_boundaries=set(),
            extreme_boundaries=set(),
            max_share=0.35,
            target_bin_count=4,
        )

        self.assertEqual(result, [(1, 2), (3, 4), (5, 5), (6, 6)])

    def test_refine_ranges_under_share_cap_keeps_compliant_ranges_unchanged(self):
        stats = self._share_cap_stats()
        # 各箱占比均不超过上限时原样返回。
        result = binning.refine_ranges_under_share_cap(
            [(1, 2), (3, 3), (4, 4)],
            stats,
            protected_boundaries=set(),
            extreme_boundaries=set(),
            max_share=0.5,
            target_bin_count=3,
        )

        self.assertEqual(result, [(1, 2), (3, 3), (4, 4)])

    def test_refine_ranges_under_share_cap_returns_none_when_single_bin_unsplittable(self):
        stats = self._share_cap_stats()
        # (2,5)=70% 超 10% 上限，但任一可行拆点的左子箱都 ≥20%，拆不出合规子箱。
        result = binning.refine_ranges_under_share_cap(
            [(1, 1), (2, 5), (6, 6)],
            stats,
            protected_boundaries=set(),
            extreme_boundaries=set(),
            max_share=0.1,
            target_bin_count=3,
        )

        self.assertIsNone(result)

    def test_refine_ranges_under_share_cap_returns_none_when_all_merges_blocked(self):
        stats = self._share_cap_stats()
        # 拆分后唯一可行的合并对 (1,1)+(2,2) 跨 extreme 边界 1，被拦截后无法回到目标档数。
        result = binning.refine_ranges_under_share_cap(
            [(1, 1), (2, 4), (5, 6)],
            stats,
            protected_boundaries=set(),
            extreme_boundaries={1},
            max_share=0.35,
            target_bin_count=4,
        )

        self.assertIsNone(result)

    @staticmethod
    def _share_cap_stats():
        return pd.DataFrame(
            {
                "bin_order": list(range(1, 7)),
                "n": [10, 20, 15, 10, 25, 20],
                "principal_amt": [1000, 2000, 1500, 1000, 2500, 2000],
                "score_left": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
                "score_right": [0.1, 0.2, 0.3, 0.4, 0.5, 1.0],
                "score_min": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
                "score_max": [0.1, 0.2, 0.3, 0.4, 0.5, 1.0],
                "score_mean": [0.05, 0.15, 0.25, 0.35, 0.45, 0.75],
                "1m30p_cnt_mature": [90, 180, 140, 90, 220, 180],
                "1m30p_cnt_bad": [1, 4, 4, 4, 11, 11],
                "1m30p_amt_exposure": [9000, 18000, 14000, 9000, 22000, 18000],
                "1m30p_amt_bad": [100, 400, 400, 400, 1100, 1100],
                "3m30p_cnt_mature": [90, 180, 140, 90, 220, 180],
                "3m30p_cnt_bad": [1, 4, 4, 4, 11, 11],
                "3m30p_amt_exposure": [9000, 18000, 14000, 9000, 22000, 18000],
                "3m30p_amt_bad": [100, 400, 400, 400, 1100, 1100],
            }
        )


if __name__ == "__main__":
    unittest.main()
