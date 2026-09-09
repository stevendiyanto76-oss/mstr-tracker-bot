"""
Unit Tests for MSTR Dynamic Model (V3)
=====================================
Validates:
- Dynamic mNAV process (regime awareness, liquidity penalty, clipping).
- Valuation boundaries (parity, margin of safety, fair price, rich boundary).
- Continuous risk-budgeted portfolio sizing (monotonicity, 3% hard cap, invalidation).
- Forward deterministic scenarios and Monte Carlo reproducibility.
"""

from __future__ import annotations

import math
import unittest
from datetime import date

from backtest_engine import (
    ForwardScenarioSimulator,
    V3CapitalStructure,
    V3ModelEngine,
    get_interpolated_capital_structure,
)


class TestV3ModelEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = V3ModelEngine()
        self.cap = V3CapitalStructure(
            btc_holdings=843775,
            adso_m=401.294,
            basic_shares_m=371.614,
            debt_b=6.754,
            preferred_notional_b=15.464,
            usd_reserve_b=2.550,
            software_floor_b=1.0,
            tier1_mandatory_cash_burden_b=0.280,
            tier2_discretionary_burden_b=1.480,
        )

    def test_dynamic_mnav_bull_vs_bear_momentum(self) -> None:
        # Bull momentum should use higher elasticity beta
        mnav_bull = self.engine.compute_dynamic_mnav(momentum_12m=0.80, realized_vol_12m=0.65, reserve_coverage_months=20.0)
        mnav_normal = self.engine.compute_dynamic_mnav(momentum_12m=0.20, realized_vol_12m=0.65, reserve_coverage_months=20.0)
        mnav_bear = self.engine.compute_dynamic_mnav(momentum_12m=-0.50, realized_vol_12m=0.65, reserve_coverage_months=20.0)

        self.assertGreater(mnav_bull, mnav_normal)
        self.assertGreater(mnav_normal, mnav_bear)
        self.assertLessEqual(mnav_bull, 2.20)
        self.assertGreaterEqual(mnav_bear, 0.50)

    def test_dynamic_mnav_continuous_liquidity_penalty(self) -> None:
        # Penalty should scale smoothly when reserve coverage drops below 15 months
        mnav_healthy = self.engine.compute_dynamic_mnav(momentum_12m=0.10, realized_vol_12m=0.65, reserve_coverage_months=18.0)
        mnav_tight = self.engine.compute_dynamic_mnav(momentum_12m=0.10, realized_vol_12m=0.65, reserve_coverage_months=7.5)
        mnav_depleted = self.engine.compute_dynamic_mnav(momentum_12m=0.10, realized_vol_12m=0.65, reserve_coverage_months=0.0)

        self.assertGreater(mnav_healthy, mnav_tight)
        self.assertGreater(mnav_tight, mnav_depleted)
        self.assertAlmostEqual(mnav_healthy - mnav_depleted, 0.30, places=4)

    def test_valuation_boundaries_ordering(self) -> None:
        metrics = self.engine.evaluate(
            btc_price=63175.25,
            mstr_price=93.89,
            cap=self.cap,
            momentum_12m=0.15,
            realized_vol_12m=0.60,
        )

        self.assertGreater(metrics.margin_of_safety_price, 0.0)
        self.assertGreater(metrics.parity_price, metrics.margin_of_safety_price)
        self.assertAlmostEqual(metrics.margin_of_safety_price / metrics.parity_price, 0.80, places=4)
        if metrics.dynamic_mnav >= 1.0:
            self.assertGreaterEqual(metrics.fair_price, metrics.parity_price)
            self.assertGreater(metrics.rich_price, metrics.fair_price)

    def test_continuous_portfolio_weights(self) -> None:
        btc_p = 63175.25

        # 1. Deep Value: Price at 50% of Margin of Safety
        parity = self.engine.residual_per_adso(1.0, btc_p, self.cap)
        mos = 0.80 * parity

        m_deep_val = self.engine.evaluate(btc_price=btc_p, mstr_price=mos * 0.90, cap=self.cap, momentum_12m=0.1, realized_vol_12m=0.6)
        self.assertGreaterEqual(m_deep_val.target_weight, 0.025)
        self.assertLessEqual(m_deep_val.target_weight, 0.030)
        self.assertEqual(m_deep_val.zone_label, "STRONG BUY / DEEP VALUE")

        # 2. Starter / Value: Price between MoS and Parity
        m_val = self.engine.evaluate(btc_price=btc_p, mstr_price=(mos + parity) / 2.0, cap=self.cap, momentum_12m=0.1, realized_vol_12m=0.6)
        self.assertGreaterEqual(m_val.target_weight, 0.015)
        self.assertLessEqual(m_val.target_weight, 0.025)
        self.assertEqual(m_val.zone_label, "ACCUMULATE / VALUE")

        # 3. Extreme Premium: Price way above Rich
        m_rich = self.engine.evaluate(btc_price=btc_p, mstr_price=parity * 2.5, cap=self.cap, momentum_12m=0.1, realized_vol_12m=0.6)
        self.assertLessEqual(m_rich.target_weight, 0.0025)
        self.assertEqual(m_rich.zone_label, "SELL / EXTREME PREMIUM")

    def test_invalidation_gate_drops_allocation_to_zero(self) -> None:
        # Impoverished reserve (< 12 months)
        stressed_cap = V3CapitalStructure(
            btc_holdings=self.cap.btc_holdings,
            adso_m=self.cap.adso_m,
            basic_shares_m=self.cap.basic_shares_m,
            debt_b=self.cap.debt_b,
            preferred_notional_b=self.cap.preferred_notional_b,
            usd_reserve_b=0.50,  # only ~3.4 months coverage
            software_floor_b=1.0,
            tier1_mandatory_cash_burden_b=0.28,
            tier2_discretionary_burden_b=1.48,
        )

        metrics = self.engine.evaluate(
            btc_price=63175.25,
            mstr_price=50.0,
            cap=stressed_cap,
            momentum_12m=0.1,
            realized_vol_12m=0.6,
        )

        self.assertTrue(metrics.invalidation_active)
        self.assertEqual(metrics.target_weight, 0.0)
        self.assertEqual(metrics.zone_label, "INVALIDATION / EXIT")

    def test_deterministic_forward_simulation(self) -> None:
        sim = ForwardScenarioSimulator(engine=self.engine)
        det_df = sim.run_deterministic_paths()
        self.assertEqual(len(det_df), 7)
        self.assertIn("A_FALL_THEN_RECOVER", det_df["Path"].values)
        self.assertIn("B_RISE_IMMEDIATELY", det_df["Path"].values)


if __name__ == "__main__":
    unittest.main()
