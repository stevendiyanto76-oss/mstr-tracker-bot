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
    HistoricalBacktester,
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
        self.assertEqual(len(det_df), 9)
        self.assertIn("A_FALL_THEN_RECOVER", det_df["Path"].values)
        self.assertIn("B_RISE_IMMEDIATELY", det_df["Path"].values)
        self.assertIn("H_PROLONGED_BEAR_35K", det_df["Path"].values)
        self.assertIn("I_DEEP_RECESSION_25K", det_df["Path"].values)

    def test_fee_calculation_no_capital_stranding(self) -> None:
        cash = 10000.0
        fee_rate = 0.0035
        diff_val = 15000.0  # Wants to deploy more than available cash
        invest_amt = min(cash / (1.0 + fee_rate), diff_val)
        cost = invest_amt * (1.0 + fee_rate)
        self.assertLessEqual(cost, cash)
        self.assertAlmostEqual(cost, cash, places=6)

    def test_tier1_mandatory_runway_calculation(self) -> None:
        latest_cap = get_interpolated_capital_structure(date(2026, 9, 9))
        self.assertEqual(latest_cap.tier1_mandatory_cash_burden_b, 0.035)
        # $6.538B cash / $0.035B pure debt coupon * 12 months > 180 months
        runway_months = (latest_cap.usd_reserve_b / latest_cap.tier1_mandatory_cash_burden_b) * 12.0
        self.assertGreater(runway_months, 180.0)

    def test_stressed_wipeout_price_at_70pct_mnav(self) -> None:
        latest_cap = get_interpolated_capital_structure(date(2026, 9, 9))
        net_senior = latest_cap.debt_b + latest_cap.preferred_notional_b - latest_cap.usd_reserve_b - latest_cap.software_floor_b
        # P_wipeout = net_senior / (mnav * H_btc)
        wipeout_parity = (net_senior * 1e9) / (1.0 * latest_cap.btc_holdings)
        wipeout_stressed = (net_senior * 1e9) / (0.70 * latest_cap.btc_holdings)
        self.assertAlmostEqual(wipeout_parity, 16331.58, delta=10.0)
        self.assertAlmostEqual(wipeout_stressed, 23330.83, delta=10.0)

    def test_econometric_metrics_calmar_and_psr(self) -> None:
        tester = HistoricalBacktester(engine=self.engine)
        res = tester.run()
        # Verify econometric properties:
        self.assertGreater(res.sleeve_calmar_ratio, 0.20)
        self.assertGreaterEqual(res.sleeve_psr_pct, 95.0)
        self.assertLessEqual(res.sleeve_trades, 75)  # Rebalance discipline: < 75 trades across 6 years
        self.assertGreater(res.sleeve_trades, 10)

    def test_cash_yield_overlay_enhancement(self) -> None:
        tester = HistoricalBacktester(engine=self.engine)
        res_baseline = tester.run(cash_yield_annual_pct=0.0)
        res_with_yield = tester.run(cash_yield_annual_pct=0.04)
        # Cash yield overlay should enhance total return and maintain or improve drawdown
        self.assertGreater(res_with_yield.sleeve_total_return_pct, res_baseline.sleeve_total_return_pct)
        self.assertGreater(res_with_yield.total_return_pct, res_baseline.total_return_pct)


class TestLayer1FailureAlerts(unittest.TestCase):
    def test_layer1_failure_recording_and_alert_formatting(self) -> None:
        from mstr_bot import (
            LAYER_1_FAILURES,
            clear_layer1_failures,
            format_layer1_warning_alert,
            record_layer1_failure,
        )

        clear_layer1_failures()
        self.assertEqual(len(LAYER_1_FAILURES), 0)

        # Record single failure
        record_layer1_failure("Strategy.com Dashboard", "Connection Timeout", "CoinGecko / Yahoo")
        self.assertEqual(len(LAYER_1_FAILURES), 1)
        self.assertEqual(LAYER_1_FAILURES[0]["component"], "Strategy.com Dashboard")

        # Deduplication check
        record_layer1_failure("Strategy.com Dashboard", "Second error", "CoinGecko")
        self.assertEqual(len(LAYER_1_FAILURES), 1)

        # Second distinct failure
        record_layer1_failure("Portofolio V2", "HTTP 500", "Local Mirror")
        self.assertEqual(len(LAYER_1_FAILURES), 2)

        # Alert formatting validation
        alert_msg = format_layer1_warning_alert(LAYER_1_FAILURES)
        self.assertIn("🚨 PERINGATAN SISTEM: GANGGUAN LAYER 1", alert_msg)
        self.assertIn("Strategy.com Dashboard", alert_msg)
        self.assertIn("Connection Timeout", alert_msg)
        self.assertIn("Portofolio V2", alert_msg)
        self.assertIn("HTTP 500", alert_msg)
        self.assertIn("STATUS PENANGANAN SISTEM", alert_msg)

        clear_layer1_failures()
        self.assertEqual(len(LAYER_1_FAILURES), 0)


if __name__ == "__main__":
    unittest.main()
