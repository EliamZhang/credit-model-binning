import unittest

import pandas as pd

import binning


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


if __name__ == "__main__":
    unittest.main()
