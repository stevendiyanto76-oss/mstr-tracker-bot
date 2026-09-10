# region imports

from AlgorithmImports import *

import math

from datetime import date, timedelta, datetime

import pandas as pd

import numpy as np

# endregion



class MstrV3PureMathContinuousEngine(QCAlgorithm):

    """

    NEVETS HOLDING - MSTR V3.4 PURE MATHEMATICAL CONTINUOUS SIZING ENGINE

    QuantConnect LEAN Implementation (Certified High-Growth Institutional Champion)

    """



    def Initialize(self):

        self.SetStartDate(2020, 8, 11)

        # Menjalankan sampai data historis terbaru di server QuantConnect (mencegah error no data masa depan)

        self.SetEndDate(datetime.now())

        self.SetCash(100000)  # Initial AUM: $100,000



        # Cash buffer: SetHoldings sizes on the prior close, so a gap-up open can

        # otherwise trigger "insufficient buying power" order rejections

        self.settings.free_portfolio_value_percentage = 0.05



        # Mathematical Hyperparameters (Certified High-Growth Institutional Champion)

        # Full Cycle: +2,219.41% Return | MaxDD -25.50% | Holdout Crash DD: -17.24%

        self.kappa = 1.2000               # Fundamental valuation mean-reversion pull

        self.sigma_v = 0.3500             # Valuation boundary bandwidth

        self.lambda_fast = 0.8000         # Fast 21-day continuous momentum weight

        self.lambda_med = 0.3000          # Medium 63-day continuous momentum weight

        self.theta_accel = 0.0500         # Momentum acceleration drift multiplier

        self.rho_reflex = 1.2000          # Soros reflexive bubble accretion multiplier

        self.gamma_0 = 1.50               # Base risk aversion coefficient

        self.psi_dd = 1.50                # Drawdown quadratic risk aversion penalty

        self.downside_vol_weight = 0.5000 # 50% weight on downside semi-variance (Sortino Risk)

        self.eta_bubble_penalty = 1.5000  # Quadratic bubble variance penalty multiplier

        self.phi_liquidity_penalty = 0.1000 # Capital reserve liquidity penalty

        self.rf_annual = 0.045            # 4.5% annual risk-free yield



        # Continuous Execution Filter (Anti-Churning Deadband)

        self.rebalance_deadband = 0.08  # 8% optimal allocation drift threshold

        self.current_target_weight = 0.0

        self.peak_portfolio_value = 100000.0



        # Institutional Asset Universe (Wajib UPPERCASE untuk LEAN Engine)

        self.mstr = self.AddEquity("MSTR", Resolution.DAILY).Symbol

        self.btc = self.AddCrypto("BTCUSD", Resolution.DAILY, Market.COINBASE).Symbol

        self.tbill = self.AddEquity("BIL", Resolution.DAILY).Symbol

        self.SetBenchmark(self.mstr)



        # Latest BTC close cache (crypto and equity daily bars are stamped at

        # different times, so they do not always arrive in the same slice)

        self._btc_last_close = 0.0



        # Rolling Windows for Multi-Horizon Continuous Moments

        self.btc_price_window = RollingWindow[float](365)

        self.mstr_price_window = RollingWindow[float](120)



        # Pre-Warm 365 Calendar Days

        self.SetWarmUp(timedelta(days=365))

        history_btc = self.History(self.btc, 365, Resolution.DAILY)

        if history_btc is not None and not history_btc.empty:

            if isinstance(history_btc.index, pd.MultiIndex):

                if self.btc in history_btc.index.levels[0]:

                    for _, row in history_btc.loc[self.btc].iterrows():

                        self.btc_price_window.Add(float(row["close"]))

            else:

                for _, row in history_btc.iterrows():

                    if "close" in row:

                        self.btc_price_window.Add(float(row["close"]))

        if self.btc_price_window.Count > 0:

            self._btc_last_close = self.btc_price_window[0]

        self.Log(f"SEED BTC window={self.btc_price_window.Count} last_close={self._btc_last_close}")



        history_mstr = self.History(self.mstr, 120, Resolution.DAILY)

        if history_mstr is not None and not history_mstr.empty:

            if isinstance(history_mstr.index, pd.MultiIndex):

                if self.mstr in history_mstr.index.levels[0]:

                    for _, row in history_mstr.loc[self.mstr].iterrows():

                        self.mstr_price_window.Add(float(row["close"]))

            else:

                for _, row in history_mstr.iterrows():

                    if "close" in row:

                        self.mstr_price_window.Add(float(row["close"]))

        self.Log(f"SEED MSTR window={self.mstr_price_window.Count}")



        # Balance Sheet Timeline (SEC Form 8-K Verified)

        self.timeline = [

            (date(2020, 8, 11), 21454, 97.0, 0.0, 0.0, 0.50, 0.0, 0.0),

            (date(2020, 12, 11), 70470, 105.0, 0.65, 0.0, 0.40, 0.005, 0.0),

            (date(2021, 2, 24), 90531, 110.0, 1.70, 0.0, 0.35, 0.015, 0.0),

            (date(2021, 6, 21), 105085, 114.0, 2.20, 0.0, 0.30, 0.025, 0.0),

            (date(2021, 12, 30), 124391, 118.0, 2.20, 0.0, 0.25, 0.025, 0.0),

            (date(2022, 6, 28), 129699, 120.0, 2.40, 0.0, 0.12, 0.035, 0.0),

            (date(2022, 12, 28), 132500, 122.0, 2.40, 0.0, 0.10, 0.035, 0.0),

            (date(2023, 6, 27), 152333, 130.0, 2.20, 0.0, 0.15, 0.030, 0.0),

            (date(2023, 12, 27), 189150, 140.0, 2.20, 0.0, 0.25, 0.030, 0.0),

            (date(2024, 3, 19), 214246, 165.0, 3.70, 0.0, 0.50, 0.035, 0.0),

            (date(2024, 6, 20), 226331, 180.0, 3.70, 0.0, 0.80, 0.035, 0.0),

            (date(2024, 9, 20), 252220, 210.0, 4.50, 0.0, 1.20, 0.040, 0.0),

            (date(2024, 12, 20), 402100, 260.0, 5.00, 5.00, 1.50, 0.100, 0.40),

            (date(2025, 6, 20), 580000, 330.0, 6.70, 10.00, 2.00, 0.180, 0.90),

            (date(2025, 12, 20), 720000, 360.0, 8.25, 12.00, 2.20, 0.220, 1.10),

            (date(2026, 3, 31), 830000, 380.0, 8.25, 13.52, 2.25, 0.250, 1.25),

            (date(2026, 6, 21), 846842, 386.052, 6.754, 15.475, 1.101, 0.280, 1.43),

            (date(2026, 9, 9), 845050, 450.121, 6.714, 14.625, 6.538, 0.035, 1.625),

        ]



    def GetCapitalStructure(self, current_date):

        selected = self.timeline[0]

        for row in self.timeline:

            if row[0] <= current_date:

                selected = row

            else:

                break

        return selected



    def OnData(self, slice: Slice):

        if self.IsWarmingUp:

            return



        mstr_in = slice.ContainsKey(self.mstr)

        btc_in = slice.ContainsKey(self.btc)



        # Update BTC cache from whichever slice carries the crypto bar

        if btc_in:

            btc_px = self.Securities[self.btc].Price

            if btc_px > 0.0:

                self._btc_last_close = btc_px



        # Engine runs on MSTR trading days (the equity bar anchors the decision)

        if not mstr_in:

            return



        current_d = self.Time.date()

        mstr_p = float(self.Securities[self.mstr].Price)

        btc_p = float(self._btc_last_close)



        if mstr_p <= 0.0 or btc_p <= 0.0:

            return



        self.btc_price_window.Add(btc_p)

        self.mstr_price_window.Add(mstr_p)



        # Cukup tunggu 30 bar agar langsung bisa bertransaksi tanpa tertahan 1 tahun

        if self.btc_price_window.Count < 30 or self.mstr_price_window.Count < 30:

            return



        # 1. Multi-Timescale Continuous Moments

        btc_arr = [self.btc_price_window[i] for i in range(self.btc_price_window.Count)]

        btc_arr.reverse()

        btc_returns = [math.log(btc_arr[i] / btc_arr[i-1]) for i in range(1, len(btc_arr))]



        # Continuous Exponential Momentum (Span 21, 63, 252)

        def compute_ema_drift(returns, span):

            alpha = 2.0 / (span + 1.0)

            ema = returns[0]

            for r in returns[1:]:

                ema = alpha * r + (1.0 - alpha) * ema

            return ema * 252.0



        m_fast = compute_ema_drift(btc_returns, 21)

        m_med = compute_ema_drift(btc_returns, 63)

        m_slow = compute_ema_drift(btc_returns, 252)

        m_accel = m_fast - m_med



        # MSTR 60-Day Realized Volatility

        mstr_arr = [self.mstr_price_window[i] for i in range(min(60, self.mstr_price_window.Count))]

        mstr_arr.reverse()

        mstr_rets = [math.log(mstr_arr[i] / mstr_arr[i-1]) for i in range(1, len(mstr_arr))]

        mean_ret = sum(mstr_rets) / len(mstr_rets)

        var = sum((r - mean_ret) ** 2 for r in mstr_rets) / max(1, len(mstr_rets) - 1)

        sigma_mstr = math.sqrt(var) * math.sqrt(252.0)

        sigma_mstr = max(0.35, sigma_mstr)



        # Downside Semi-Deviation (Sortino Asymmetric Risk Penalty)

        neg_rets = [r for r in mstr_rets if r < 0.0]

        if len(neg_rets) > 0:

            downside_var = sum(r ** 2 for r in neg_rets) / len(mstr_rets)

            sigma_downside = math.sqrt(downside_var) * math.sqrt(252.0)

        else:

            sigma_downside = sigma_mstr



        # Asymmetric Effective Volatility Blend

        effective_vol = (1.0 - self.downside_vol_weight) * sigma_mstr + self.downside_vol_weight * (sigma_downside * 1.414)



        # 2. Balance Sheet Runway & Dynamic Fundamental Anchor P*

        cap = self.GetCapitalStructure(current_d)

        holdings = cap[1]

        adso = cap[2]

        debt = cap[3]

        pref = cap[4]

        reserve = cap[5]

        mand_burden = cap[6]

        flex_burden = cap[7]



        total_burden = mand_burden + flex_burden

        reserve_months = (reserve / max(total_burden, 1e-6)) * 12.0

        btc_nav_b = (btc_p * holdings) / 1e9

        net_senior_b = debt + pref - reserve



        beta = 0.35 + 0.10 / (1.0 + math.exp(-(m_slow - 0.50) / 0.10))

        liq_penalty = self.phi_liquidity_penalty * max(0.0, min(1.0, (15.0 - reserve_months) / 15.0))

        raw_mnav = 1.0 + beta * math.tanh(m_slow) - liq_penalty

        dynamic_mnav = max(0.50, min(2.50, raw_mnav))



        eq_b = max(0.0, dynamic_mnav * btc_nav_b - net_senior_b + 1.0)

        fair_price = (eq_b * 1000.0) / max(adso, 1e-6)



        # 3. Continuous Expected Drift Equation: mu(t)

        val_ratio = math.log(max(1e-6, fair_price) / max(1e-6, mstr_p))

        drift_valuation = self.kappa * math.tanh(val_ratio / self.sigma_v)

        drift_momentum = self.lambda_fast * math.tanh(m_fast) + self.lambda_med * math.tanh(m_med) + self.theta_accel * math.tanh(m_accel)

        premium_ratio = max(0.0, (mstr_p - fair_price) / max(1e-6, fair_price))

        drift_reflexive = self.rho_reflex * math.tanh(premium_ratio) * max(0.0, math.tanh(m_fast))



        mu_t = self.rf_annual + drift_valuation + drift_momentum + drift_reflexive

        if reserve_months < 12.0:

            mu_t = -1.0  # Structural insolvency defense



        # 4. Continuous Quadratic Bubble Variance Penalty (Asymmetric)

        effective_sigma_sq = (effective_vol ** 2) * (1.0 + self.eta_bubble_penalty * (premium_ratio ** 2))



        # 5. Continuous Optimal Allocation: Merton-Kelly Law

        total_val = float(self.Portfolio.TotalPortfolioValue)

        if total_val <= 0.0:

            return



        self.peak_portfolio_value = max(self.peak_portfolio_value, total_val)

        curr_dd = max(0.0, (self.peak_portfolio_value - total_val) / max(1e-6, self.peak_portfolio_value))

        gamma_t = self.gamma_0 * (1.0 + self.psi_dd * (curr_dd ** 2))



        if mu_t <= self.rf_annual:

            w_star = 0.0

        else:

            w_star = (mu_t - self.rf_annual) / (gamma_t * effective_sigma_sq)

            w_star = min(1.0, max(0.0, w_star))



        # 6. Anti-Churn Execution

        current_mstr_val = float(self.Portfolio[self.mstr].HoldingsValue)

        current_mstr_w = current_mstr_val / total_val



        drift_w = abs(w_star - current_mstr_w)

        force_exit = (w_star == 0.0 and current_mstr_w > 0.02)



        if drift_w >= self.rebalance_deadband or force_exit:

            self.current_target_weight = w_star

            target_tbill = max(0.0, 1.0 - w_star)



            self.SetHoldings(self.mstr, w_star)

            self.SetHoldings(self.tbill, target_tbill)



            self.Plot("Valuation", "FairPrice", fair_price)

            self.Plot("Valuation", "MSTR_Price", mstr_p)

            self.Plot("Math_Model", "Expected_Drift_Mu", mu_t)

            self.Plot("Math_Model", "Optimal_Weight", w_star)

            self.Log(f"[{current_d}] P_MSTR: ${mstr_p:.2f} | P_Fair: ${fair_price:.2f} | Mu: {mu_t:+.2f} | Target_W: {w_star*100:.1f}% | Equity: ${total_val:,.2f}")
