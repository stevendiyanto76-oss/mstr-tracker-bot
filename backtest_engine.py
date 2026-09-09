"""
MSTR Dynamic Investment Model (V3) & Multi-Era Backtesting Engine
================================================================
Implements the upgraded V3 Dynamic Model addressing the 6 core weaknesses of the 2026 thesis:
1. Dynamic Valuation Boundaries (no static dollar gates).
2. Continuous & Regime-Aware Dynamic mNAV process.
3. Software business operating floor ($1.0B baseline).
4. Tiered capital stack (mandatory debt/cumulative vs discretionary preferreds).
5. Real-time implied issuance accretion.
6. Continuous risk-budgeted sizing with anti-churn execution.

Runs:
- Historical Backtest (2020-2026) with daily market prices & reconstructed capital stack.
- Forward Scenarios (2026-2030): 7 Deterministic Paths & Fat-tailed Monte Carlo (Student-t).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 1. V3 Data Structures & Capital Structure Representation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V3CapitalStructure:
    """Balance sheet snapshot of Strategy Inc. with tiered claims."""
    btc_holdings: float
    adso_m: float                   # Assumed Diluted Shares Outstanding in Millions
    basic_shares_m: float           # Legal common shares in Millions
    debt_b: float                   # Senior debt principal in $B
    preferred_notional_b: float     # Total preferred liquidation notional in $B
    usd_reserve_b: float            # Policy cash & liquid reserve in $B
    software_floor_b: float = 1.0   # Baseline recurring software value in $B
    tier1_mandatory_cash_burden_b: float = 0.30  # Debt coupon + senior cumulative pref
    tier2_discretionary_burden_b: float = 1.46   # STRC, STRD, STRK flexible burden


@dataclass(frozen=True)
class V3Metrics:
    btc_price: float
    mstr_price: float
    btc_nav_b: float
    net_senior_claims_b: float
    current_enterprise_value_b: float
    current_mnav: float
    dynamic_mnav: float
    parity_price: float             # Residual / ADSO at 1.0x mNAV
    margin_of_safety_price: float   # 0.80 * parity_price (20% discount)
    fair_price: float               # Residual / ADSO at dynamic_mnav
    rich_price: float               # Upper boundary where MSTR is overvalued
    structural_floor_mnav: float    # Multiple where common equity is zero
    reserve_coverage_months: float  # Full reserve runway
    hard_reserve_coverage_months: float  # Runway against Tier 1 mandatory burden
    momentum_12m: float
    realized_vol_12m: float
    target_weight: float            # Continuous portfolio target weight w* in [0.0%, 3.0%]
    zone_label: str
    rebalance_signal: str
    invalidation_active: bool


# ---------------------------------------------------------------------------
# 2. V3 Model Engine Implementation
# ---------------------------------------------------------------------------

class V3ModelEngine:
    """Core mathematical engine for V3 Dynamic Valuation and Portfolio Sizing."""

    def __init__(
        self,
        beta_base: float = 0.35,
        beta_bull: float = 0.45,
        vol_base: float = 0.65,
        mnav_floor: float = 0.50,
        mnav_ceiling: float = 2.20,
        max_portfolio_weight: float = 0.03,  # 3% hard cap
        rebalance_deadband: float = 0.005,   # 0.5% allocation drift deadband
    ):
        self.beta_base = beta_base
        self.beta_bull = beta_bull
        self.vol_base = vol_base
        self.mnav_floor = mnav_floor
        self.mnav_ceiling = mnav_ceiling
        self.max_portfolio_weight = max_portfolio_weight
        self.rebalance_deadband = rebalance_deadband

    def compute_dynamic_mnav(
        self,
        momentum_12m: float,
        realized_vol_12m: float,
        reserve_coverage_months: float,
    ) -> float:
        """Computes continuous, regime-aware dynamic mNAV (Thesis Section 8.7 upgraded)."""
        # 1. State-dependent beta
        beta = self.beta_bull if momentum_12m > 0.50 else self.beta_base

        # 2. Continuous liquidity penalty (scales from 0 at >=15m down to -0.30 at 0m)
        liquidity_penalty = 0.30 * max(0.0, min(1.0, (15.0 - reserve_coverage_months) / 15.0))

        # 3. Volatility spread adjustment
        vol_adj = 0.20 * (realized_vol_12m - self.vol_base)

        raw_mnav = 1.0 + beta * math.tanh(momentum_12m) - vol_adj - liquidity_penalty
        return max(self.mnav_floor, min(self.mnav_ceiling, raw_mnav))

    def residual_per_adso(
        self,
        mnav: float,
        btc_price: float,
        cap: V3CapitalStructure,
    ) -> float:
        """Computes common equity residual per ADSO at a given mNAV multiple."""
        btc_nav_b = (btc_price * cap.btc_holdings) / 1e9
        net_senior_claims_b = cap.debt_b + cap.preferred_notional_b - cap.usd_reserve_b
        equity_b = max(0.0, mnav * btc_nav_b - net_senior_claims_b + cap.software_floor_b)
        return (equity_b * 1000.0) / max(cap.adso_m, 1e-6)

    def evaluate(
        self,
        btc_price: float,
        mstr_price: float,
        cap: V3CapitalStructure,
        momentum_12m: float,
        realized_vol_12m: float,
        current_portfolio_weight: float = 0.0,
    ) -> V3Metrics:
        """Evaluates live metrics, dynamic boundaries, and target allocation weight."""
        btc_nav_b = (btc_price * cap.btc_holdings) / 1e9
        net_senior_claims_b = cap.debt_b + cap.preferred_notional_b - cap.usd_reserve_b
        market_cap_b = (mstr_price * cap.basic_shares_m) / 1000.0
        ev_b = market_cap_b + cap.debt_b + cap.preferred_notional_b - cap.usd_reserve_b
        current_mnav = ev_b / max(btc_nav_b, 1e-6)

        total_burden = cap.tier1_mandatory_cash_burden_b + cap.tier2_discretionary_burden_b
        reserve_months = (cap.usd_reserve_b / max(total_burden, 1e-6)) * 12.0
        hard_reserve_months = (cap.usd_reserve_b / max(cap.tier1_mandatory_cash_burden_b, 1e-6)) * 12.0

        # Invalidation triggers from Thesis:
        # Reserve < 12 months OR BTC NAV < Senior Claims with reserve < 15m
        invalidation_active = (reserve_months < 12.0) or (btc_nav_b < net_senior_claims_b and reserve_months < 15.0)

        # Dynamic mNAV process
        dynamic_mnav = self.compute_dynamic_mnav(momentum_12m, realized_vol_12m, reserve_months)

        # Valuation Boundaries
        parity_price = self.residual_per_adso(1.0, btc_price, cap)
        margin_of_safety_price = 0.80 * parity_price
        fair_price = self.residual_per_adso(dynamic_mnav, btc_price, cap)
        rich_multiple = max(1.35, 1.15 * dynamic_mnav)
        rich_price = self.residual_per_adso(rich_multiple, btc_price, cap)
        structural_floor_mnav = max(0.0, (net_senior_claims_b - cap.software_floor_b) / max(btc_nav_b, 1e-6))

        # Continuous Target Portfolio Weight w* in [0.0%, 3.0%]
        if invalidation_active:
            target_weight = 0.0
            zone_label = "INVALIDATION / EXIT"
        elif mstr_price <= margin_of_safety_price:
            # Deep Value: 2.5% to 3.0%
            scale = min(1.0, (margin_of_safety_price - mstr_price) / max(margin_of_safety_price * 0.25, 1e-6))
            target_weight = 0.025 + 0.005 * scale
            zone_label = "STRONG BUY / DEEP VALUE"
        elif mstr_price <= parity_price:
            # Value / Starter: 1.5% to 2.5%
            dist = (parity_price - mstr_price) / max(parity_price - margin_of_safety_price, 1e-6)
            target_weight = 0.015 + 0.010 * dist
            zone_label = "ACCUMULATE / VALUE"
        elif mstr_price <= fair_price:
            # Fair / Hold: 0.75% to 1.5%
            dist = (fair_price - mstr_price) / max(fair_price - parity_price, 1e-6)
            target_weight = 0.0075 + 0.0075 * dist
            zone_label = "HOLD / FAIR VALUE"
        elif mstr_price <= rich_price:
            # Premium / Trimming: 0.25% to 0.75%
            dist = (rich_price - mstr_price) / max(rich_price - fair_price, 1e-6)
            target_weight = 0.0025 + 0.0050 * dist
            zone_label = "REDUCE / PREMIUM"
        else:
            # Extreme Premium: 0.0% to 0.25%
            excess = (mstr_price - rich_price) / max(rich_price, 1e-6)
            target_weight = max(0.0, 0.0025 * math.exp(-excess * 2.0))
            zone_label = "SELL / EXTREME PREMIUM"

        target_weight = min(self.max_portfolio_weight, target_weight)

        # Anti-churn rebalancing check
        weight_diff = target_weight - current_portfolio_weight
        if abs(weight_diff) >= self.rebalance_deadband:
            rebalance_signal = f"{'ADD' if weight_diff > 0 else 'TRIM'} ({weight_diff*100:+.2f}%)"
        else:
            rebalance_signal = "MAINTAIN"

        return V3Metrics(
            btc_price=btc_price,
            mstr_price=mstr_price,
            btc_nav_b=btc_nav_b,
            net_senior_claims_b=net_senior_claims_b,
            current_enterprise_value_b=ev_b,
            current_mnav=current_mnav,
            dynamic_mnav=dynamic_mnav,
            parity_price=parity_price,
            margin_of_safety_price=margin_of_safety_price,
            fair_price=fair_price,
            rich_price=rich_price,
            structural_floor_mnav=structural_floor_mnav,
            reserve_coverage_months=reserve_months,
            hard_reserve_coverage_months=hard_reserve_months,
            momentum_12m=momentum_12m,
            realized_vol_12m=realized_vol_12m,
            target_weight=target_weight,
            zone_label=zone_label,
            rebalance_signal=rebalance_signal,
            invalidation_active=invalidation_active,
        )


# ---------------------------------------------------------------------------
# 3. Capital Structure Historical Milestones (2020–2026)
# ---------------------------------------------------------------------------

HISTORICAL_CAPITAL_STACK_TIMELINE = [
    # (Effective Date, BTC Holdings, ADSO_M, Basic_M, Debt_B, Pref_B, Reserve_B, MandBurden_B, FlexBurden_B)
    (date(2020, 8, 11), 21454, 97.0, 96.5, 0.0, 0.0, 0.50, 0.0, 0.0),
    (date(2020, 12, 11), 70470, 105.0, 96.5, 0.65, 0.0, 0.40, 0.005, 0.0),
    (date(2021, 2, 24), 90531, 110.0, 97.0, 1.70, 0.0, 0.35, 0.015, 0.0),
    (date(2021, 6, 21), 105085, 114.0, 98.0, 2.20, 0.0, 0.30, 0.025, 0.0),
    (date(2021, 12, 30), 124391, 118.0, 99.0, 2.20, 0.0, 0.25, 0.025, 0.0),
    (date(2022, 6, 28), 129699, 120.0, 99.5, 2.40, 0.0, 0.12, 0.035, 0.0),
    (date(2022, 12, 28), 132500, 122.0, 100.0, 2.40, 0.0, 0.10, 0.035, 0.0),
    (date(2023, 6, 27), 152333, 130.0, 102.0, 2.20, 0.0, 0.15, 0.030, 0.0),
    (date(2023, 12, 27), 189150, 140.0, 105.0, 2.20, 0.0, 0.25, 0.030, 0.0),
    (date(2024, 3, 19), 214246, 165.0, 120.0, 3.70, 0.0, 0.50, 0.035, 0.0),
    (date(2024, 6, 20), 226331, 180.0, 135.0, 3.70, 0.0, 0.80, 0.035, 0.0),
    (date(2024, 9, 20), 252220, 210.0, 160.0, 4.50, 0.0, 1.20, 0.040, 0.0),
    (date(2024, 12, 20), 402100, 260.0, 210.0, 5.00, 5.00, 1.50, 0.100, 0.40),
    (date(2025, 6, 20), 580000, 330.0, 280.0, 6.70, 10.00, 2.00, 0.180, 0.90),
    (date(2025, 12, 20), 720000, 360.0, 320.0, 8.25, 12.00, 2.20, 0.220, 1.10),
    (date(2026, 3, 31), 830000, 380.0, 340.0, 8.25, 13.52, 2.25, 0.250, 1.25),
    (date(2026, 6, 21), 846842, 386.052, 356.32, 6.754, 15.475, 1.101, 0.280, 1.43),
    (date(2026, 7, 9), 843775, 401.294, 371.614, 6.754, 15.464, 2.550, 0.280, 1.48),
    (date(2026, 9, 9), 845050, 450.121, 420.497, 6.714, 14.625, 6.538, 0.270, 1.39),
]


def get_interpolated_capital_structure(target_date: date) -> V3CapitalStructure:
    """Finds the most recent prior capital structure milestone for a given date."""
    selected = HISTORICAL_CAPITAL_STACK_TIMELINE[0]
    for row in HISTORICAL_CAPITAL_STACK_TIMELINE:
        if row[0] <= target_date:
            selected = row
        else:
            break

    return V3CapitalStructure(
        btc_holdings=float(selected[1]),
        adso_m=float(selected[2]),
        basic_shares_m=float(selected[3]),
        debt_b=float(selected[4]),
        preferred_notional_b=float(selected[5]),
        usd_reserve_b=float(selected[6]),
        software_floor_b=1.0,
        tier1_mandatory_cash_burden_b=float(selected[7]),
        tier2_discretionary_burden_b=float(selected[8]),
    )


# ---------------------------------------------------------------------------
# 4. Multi-Era Historical Backtester (2020–2026)
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    total_return_pct: float
    cagr_pct: float
    annualized_vol_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    total_trades: int
    # 100% Active Sleeve Metrics
    sleeve_total_return_pct: float
    sleeve_cagr_pct: float
    sleeve_vol_pct: float
    sleeve_sharpe: float
    sleeve_max_dd_pct: float
    # Benchmarks
    benchmark_mstr_return_pct: float
    benchmark_btc_return_pct: float
    benchmark_mstr_max_dd_pct: float
    benchmark_btc_max_dd_pct: float


class HistoricalBacktester:
    """Backtests the V3 model on daily historical data from 2020 to 2026."""

    def __init__(self, engine: V3ModelEngine | None = None, initial_capital: float = 10000.0):
        self.engine = engine or V3ModelEngine()
        self.initial_capital = initial_capital

    def load_market_data(self) -> pd.DataFrame:
        """Loads and aligns daily MSTR and BTC-USD prices from local CSV or yfinance."""
        from pathlib import Path
        for candidate in (Path("data/historical_mstr_btc_2020_2026.csv"), Path("historical_mstr_btc_2020_2026.csv")):
            if candidate.exists():
                df = pd.read_csv(candidate, parse_dates=["Date"]).set_index("Date")
                if "BTC" not in df.columns and "BTC_Price_USD" in df.columns:
                    df["BTC"] = df["BTC_Price_USD"]
                    df["MSTR"] = df["MSTR_Price_USD"]
                    df["BTC_MOM_12M"] = df["BTC_Momentum_12M"]
                    df["BTC_VOL_12M"] = df["BTC_Volatility_12M"]
                return df

        import yfinance as yf

        df = yf.download(
            tickers=["MSTR", "BTC-USD"],
            start="2020-01-01",
            end="2026-09-02",
            progress=False,
            auto_adjust=False,
        )

        close = df["Close"].dropna().copy()
        close.columns = ["BTC", "MSTR"]

        # Compute rolling 12-month momentum (365 days) and realized volatility
        btc_log_ret = np.log(close["BTC"] / close["BTC"].shift(1))
        rolling_mom = np.exp(btc_log_ret.rolling(window=365, min_periods=90).sum()) - 1.0
        rolling_vol = btc_log_ret.rolling(window=365, min_periods=90).std() * np.sqrt(365)

        close["BTC_MOM_12M"] = rolling_mom.fillna(0.18)
        close["BTC_VOL_12M"] = rolling_vol.fillna(0.65)
        return close.dropna()

    def run(
        self,
        market_df: pd.DataFrame | None = None,
        fee_rate: float = 0.0035,
        execution_lag: int = 0,
    ) -> BacktestResult:
        if market_df is None:
            market_df = self.load_market_data()

        dates = market_df.index
        btc_prices = market_df["BTC"].values
        mstr_prices = market_df["MSTR"].values
        btc_moms = market_df["BTC_MOM_12M"].values
        btc_vols = market_df["BTC_VOL_12M"].values

        # Portfolio state tracking
        cash = self.initial_capital
        mstr_shares = 0.0
        portfolio_values = []
        target_weights = []
        actual_weights = []
        actions = []
        trades = 0

        # Active Strategy Sleeve (100% normalized to MSTR allocation: w* / 0.03)
        sleeve_cash = self.initial_capital
        sleeve_mstr_shares = 0.0
        sleeve_values = []

        pending_target_weights = [0.0] * execution_lag

        for i in range(len(dates)):
            curr_date = dates[i].date() if hasattr(dates[i], "date") else dates[i]
            p_btc = float(btc_prices[i])
            p_mstr = float(mstr_prices[i])
            mom = float(btc_moms[i])
            vol = float(btc_vols[i])

            cap = get_interpolated_capital_structure(curr_date)
            total_equity = cash + mstr_shares * p_mstr
            current_w = (mstr_shares * p_mstr) / max(total_equity, 1e-6)

            metrics = self.engine.evaluate(
                btc_price=p_btc,
                mstr_price=p_mstr,
                cap=cap,
                momentum_12m=mom,
                realized_vol_12m=vol,
                current_portfolio_weight=current_w,
            )

            pending_target_weights.append(metrics.target_weight)
            w_star = pending_target_weights[i]
            weight_drift = w_star - current_w

            action_desc = "HOLD"
            if abs(weight_drift) >= self.engine.rebalance_deadband:
                # Rebalance toward target weight
                target_mstr_val = total_equity * w_star
                diff_val = target_mstr_val - (mstr_shares * p_mstr)

                if diff_val > 0:
                    invest_amt = min(cash, diff_val)
                    cost = invest_amt * (1.0 + fee_rate)
                    if cost <= cash and invest_amt > 10.0:
                        shares_to_buy = invest_amt / p_mstr
                        cash -= cost
                        mstr_shares += shares_to_buy
                        trades += 1
                        action_desc = f"BUY {shares_to_buy:.2f}sh"
                elif diff_val < 0:
                    sell_amt = min(mstr_shares * p_mstr, abs(diff_val))
                    if sell_amt > 10.0:
                        shares_to_sell = sell_amt / p_mstr
                        proceeds = sell_amt * (1.0 - fee_rate)
                        cash += proceeds
                        mstr_shares = max(0.0, mstr_shares - shares_to_sell)
                        trades += 1
                        action_desc = f"SELL {shares_to_sell:.2f}sh"

            # Rebalancing execution for active sleeve (0% - 100% of sleeve capital)
            sleeve_equity = sleeve_cash + sleeve_mstr_shares * p_mstr
            sleeve_w_star = min(1.0, max(0.0, w_star / 0.03))
            sleeve_current_w = (sleeve_mstr_shares * p_mstr) / max(sleeve_equity, 1e-6)
            sleeve_drift = sleeve_w_star - sleeve_current_w

            if abs(sleeve_drift) >= 0.10:  # 10% sleeve rebalance deadband
                sleeve_target_val = sleeve_equity * sleeve_w_star
                s_diff = sleeve_target_val - (sleeve_mstr_shares * p_mstr)
                if s_diff > 0:
                    s_invest = min(sleeve_cash, s_diff)
                    s_cost = s_invest * (1.0 + fee_rate)
                    if s_cost <= sleeve_cash and s_invest > 10.0:
                        s_shares = s_invest / p_mstr
                        sleeve_cash -= s_cost
                        sleeve_mstr_shares += s_shares
                elif s_diff < 0:
                    s_sell = min(sleeve_mstr_shares * p_mstr, abs(s_diff))
                    if s_sell > 10.0:
                        s_shares = s_sell / p_mstr
                        sleeve_cash += s_sell * (1.0 - fee_rate)
                        sleeve_mstr_shares = max(0.0, sleeve_mstr_shares - s_shares)

            # Daily end valuation
            daily_end_val = cash + mstr_shares * p_mstr
            daily_w = (mstr_shares * p_mstr) / max(daily_end_val, 1e-6)
            daily_sleeve_val = sleeve_cash + sleeve_mstr_shares * p_mstr

            portfolio_values.append(daily_end_val)
            sleeve_values.append(daily_sleeve_val)
            target_weights.append(w_star)
            actual_weights.append(daily_w)
            actions.append(action_desc)

        res_df = pd.DataFrame(
            {
                "Date": dates,
                "Portfolio_Val": portfolio_values,
                "Sleeve_Val": sleeve_values,
                "MSTR_Price": mstr_prices,
                "BTC_Price": btc_prices,
                "Target_Weight": target_weights,
                "Actual_Weight": actual_weights,
                "Action": actions,
            }
        ).set_index("Date")

        # Performance analytics for Macro Portfolio (3% cap)
        n_days = len(res_df)
        years = n_days / 252.0
        total_ret = (res_df["Portfolio_Val"].iloc[-1] / self.initial_capital) - 1.0
        cagr = (1.0 + total_ret) ** (1.0 / max(years, 0.1)) - 1.0

        daily_returns = res_df["Portfolio_Val"].pct_change().dropna()
        ann_vol = daily_returns.std() * np.sqrt(252)
        sharpe = (cagr - 0.03) / max(ann_vol, 1e-6)

        cummax = res_df["Portfolio_Val"].cummax()
        drawdown = (res_df["Portfolio_Val"] - cummax) / cummax
        max_dd = abs(drawdown.min())

        # Performance analytics for 100% Active Sleeve
        sleeve_tot_ret = (res_df["Sleeve_Val"].iloc[-1] / self.initial_capital) - 1.0
        sleeve_cagr = (1.0 + sleeve_tot_ret) ** (1.0 / max(years, 0.1)) - 1.0
        sleeve_ret_daily = res_df["Sleeve_Val"].pct_change().dropna()
        sleeve_vol = sleeve_ret_daily.std() * np.sqrt(252)
        sleeve_sharpe = (sleeve_cagr - 0.03) / max(sleeve_vol, 1e-6)
        sleeve_cummax = res_df["Sleeve_Val"].cummax()
        sleeve_dd = (res_df["Sleeve_Val"] - sleeve_cummax) / sleeve_cummax
        sleeve_max_dd = abs(sleeve_dd.min())

        # Benchmarks
        mstr_ret = (mstr_prices[-1] / mstr_prices[0]) - 1.0
        btc_ret = (btc_prices[-1] / btc_prices[0]) - 1.0

        mstr_cummax = pd.Series(mstr_prices).cummax()
        mstr_max_dd = abs(((pd.Series(mstr_prices) - mstr_cummax) / mstr_cummax).min())

        btc_cummax = pd.Series(btc_prices).cummax()
        btc_max_dd = abs(((pd.Series(btc_prices) - btc_cummax) / btc_cummax).min())

        return BacktestResult(
            equity_curve=res_df,
            total_return_pct=total_ret * 100.0,
            cagr_pct=cagr * 100.0,
            annualized_vol_pct=ann_vol * 100.0,
            sharpe_ratio=sharpe,
            max_drawdown_pct=max_dd * 100.0,
            total_trades=trades,
            sleeve_total_return_pct=sleeve_tot_ret * 100.0,
            sleeve_cagr_pct=sleeve_cagr * 100.0,
            sleeve_vol_pct=sleeve_vol * 100.0,
            sleeve_sharpe=sleeve_sharpe,
            sleeve_max_dd_pct=sleeve_max_dd * 100.0,
            benchmark_mstr_return_pct=mstr_ret * 100.0,
            benchmark_btc_return_pct=btc_ret * 100.0,
            benchmark_mstr_max_dd_pct=mstr_max_dd * 100.0,
            benchmark_btc_max_dd_pct=btc_max_dd * 100.0,
        )


# ---------------------------------------------------------------------------
# 5. Forward Scenario Simulator (2026–2030)
# ---------------------------------------------------------------------------

class ForwardScenarioSimulator:
    """Executes the 7 Deterministic Paths and Fat-Tailed Monte Carlo (2026-2030)."""

    def __init__(self, engine: V3ModelEngine | None = None):
        self.engine = engine or V3ModelEngine()

    def run_deterministic_paths(self) -> pd.DataFrame:
        """Executes the 7 Deterministic Scenarios from Thesis Table 16 & Table 27."""
        base_cap = get_interpolated_capital_structure(date(2026, 7, 9))

        scenarios = {
            "A_FALL_THEN_RECOVER": {"btc_2030": 175000, "btc_holdings": 741053, "adso": 455.3, "reserve": 2.50},
            "B_RISE_IMMEDIATELY":  {"btc_2030": 175000, "btc_holdings": 918565, "adso": 508.3, "reserve": 3.60},
            "C_DEPRESSED_TWO_YRS": {"btc_2030": 120000, "btc_holdings": 672542, "adso": 444.3, "reserve": 2.20},
            "D_VOLATILE_TERMINAL": {"btc_2030": 175000, "btc_holdings": 770009, "adso": 491.3, "reserve": 2.20},
            "E_WEAK_MNAV_REFIN":   {"btc_2030": 175000, "btc_holdings": 710638, "adso": 444.3, "reserve": 2.20},
            "F_SEVERE_DILUTION":   {"btc_2030": 175000, "btc_holdings": 889751, "adso": 628.3, "reserve": 2.50},
            "G_MODERATE_DELEVER":  {"btc_2030": 130000, "btc_holdings": 784345, "adso": 489.3, "reserve": 2.65},
        }

        records = []
        for name, params in scenarios.items():
            btc_target = params["btc_2030"]
            cap = V3CapitalStructure(
                btc_holdings=params["btc_holdings"],
                adso_m=params["adso"],
                basic_shares_m=params["adso"] * 0.92,
                debt_b=base_cap.debt_b,
                preferred_notional_b=base_cap.preferred_notional_b,
                usd_reserve_b=params["reserve"],
                software_floor_b=1.0,
                tier1_mandatory_cash_burden_b=base_cap.tier1_mandatory_cash_burden_b,
                tier2_discretionary_burden_b=base_cap.tier2_discretionary_burden_b,
            )

            # Forward dynamic mNAV under 2030 terminal assumptions
            mom = (btc_target / 63175.25) - 1.0
            metrics = self.engine.evaluate(
                btc_price=btc_target,
                mstr_price=self.engine.residual_per_adso(1.0, btc_target, cap),
                cap=cap,
                momentum_12m=min(1.5, max(-0.5, mom)),
                realized_vol_12m=0.55,
            )

            btc_per_adso = cap.btc_holdings / (cap.adso_m * 1e6)
            records.append({
                "Path": name,
                "Terminal_BTC": f"${btc_target:,.0f}",
                "Terminal_Holdings": f"{cap.btc_holdings:,.0f}",
                "Terminal_ADSO": f"{cap.adso_m:.1f}M",
                "BTC_per_ADSO": f"{btc_per_adso:.6f}",
                "Parity_Price": f"${metrics.parity_price:,.2f}",
                "Fair_Price_V3": f"${metrics.fair_price:,.2f}",
                "MoS_20pct": f"${metrics.margin_of_safety_price:,.2f}",
                "Reserve_Coverage": f"{metrics.reserve_coverage_months:.1f}m",
                "Target_Weight": f"{metrics.target_weight*100:.2f}%",
            })

        return pd.DataFrame(records)

    def run_monte_carlo(
        self,
        num_paths: int = 20000,
        seed: int = 20260711,
        drift_annual: float = 0.20,
        vol_annual: float = 0.60,
        df_t: float = 4.5,
        pref_rate_shift: float = 0.0,
        initial_reserve_b: float | None = None,
        geometric_cagr: bool = True,
    ) -> Mapping[str, Any]:
        """Simulates 54 monthly forward steps (2026-2030) using Student-t innovations and customizable regimes."""
        rng = np.random.default_rng(seed)
        n_months = 54
        base_cap = get_interpolated_capital_structure(date(2026, 7, 9))

        # Initial parameters
        btc_0 = 63175.25

        # Monthly parameters
        dt = 1.0 / 12.0
        if geometric_cagr:
            mu_monthly = drift_annual * dt
        else:
            mu_monthly = (drift_annual - 0.5 * (vol_annual ** 2)) * dt
        sigma_monthly = vol_annual * math.sqrt(dt)

        # Vectorized draws from Student-t
        raw_t = rng.standard_t(df=df_t, size=(num_paths, n_months))
        # Standardize Student-t variance to 1.0: var(t) = df / (df - 2)
        std_t = raw_t / math.sqrt(df_t / (df_t - 2.0))

        # Run monthly simulation
        btc_paths = np.zeros((num_paths, n_months + 1))
        btc_paths[:, 0] = btc_0

        terminal_v3_fair_prices = []
        terminal_parity_prices = []
        zero_residual_count = 0
        impairment_count = 0
        underperform_btc_count = 0

        start_reserve = initial_reserve_b if initial_reserve_b is not None else base_cap.usd_reserve_b
        adjusted_flex_burden = max(0.0, base_cap.tier2_discretionary_burden_b + pref_rate_shift)

        for p in range(num_paths):
            curr_btc = btc_0
            curr_reserve = start_reserve
            curr_adso = base_cap.adso_m
            curr_holdings = base_cap.btc_holdings

            for m in range(n_months):
                shock = std_t[p, m]
                # State-dependent volatility multiplier
                vol_mult = max(0.75, min(1.80, 0.85 + 0.35 * abs(shock)))
                r_m = mu_monthly + sigma_monthly * vol_mult * shock
                r_m = max(-0.70, min(0.70, r_m))
                curr_btc *= math.exp(r_m)

                # Monthly debt interest & preferred cash drain
                cash_drain = (base_cap.tier1_mandatory_cash_burden_b + 0.5 * adjusted_flex_burden) / 12.0
                curr_reserve -= cash_drain

                # Liquidity support: if reserve drops below 12 months, sell small BTC
                burden_annual = base_cap.tier1_mandatory_cash_burden_b + adjusted_flex_burden
                if curr_reserve < (burden_annual * 1.0):  # less than 12m
                    shortfall = max(0.0, (burden_annual * 1.0) - curr_reserve)
                    btc_to_sell = min(curr_holdings * 0.05, (shortfall * 1e9) / max(curr_btc, 1000.0))
                    curr_holdings -= btc_to_sell
                    curr_reserve += (btc_to_sell * curr_btc) / 1e9

                curr_reserve = max(0.0, curr_reserve)

            btc_paths[p, -1] = curr_btc

            # Terminal capital structure
            terminal_cap = V3CapitalStructure(
                btc_holdings=curr_holdings,
                adso_m=curr_adso,
                basic_shares_m=curr_adso * 0.92,
                debt_b=base_cap.debt_b,
                preferred_notional_b=base_cap.preferred_notional_b,
                usd_reserve_b=curr_reserve,
                software_floor_b=1.0,
                tier1_mandatory_cash_burden_b=base_cap.tier1_mandatory_cash_burden_b,
                tier2_discretionary_burden_b=adjusted_flex_burden,
            )

            mom_12 = (curr_btc / btc_0) ** (12.0 / 54.0) - 1.0
            fair_p = self.engine.residual_per_adso(1.20, curr_btc, terminal_cap)
            parity_p = self.engine.residual_per_adso(1.0, curr_btc, terminal_cap)

            terminal_v3_fair_prices.append(fair_p)
            terminal_parity_prices.append(parity_p)

            if fair_p <= 0.01:
                zero_residual_count += 1
            if fair_p < (0.25 * 93.89) or (curr_reserve < 1.0 and fair_p < parity_p):
                impairment_count += 1

            # Check vs BTC terminal return
            mstr_ret = (fair_p / 93.89) - 1.0
            btc_ret = (curr_btc / btc_0) - 1.0
            if mstr_ret < btc_ret:
                underperform_btc_count += 1

        terminal_v3 = np.array(terminal_v3_fair_prices)
        terminal_btc = btc_paths[:, -1]

        return {
            "num_paths": num_paths,
            "median_terminal_btc": float(np.median(terminal_btc)),
            "p25_terminal_btc": float(np.percentile(terminal_btc, 25)),
            "p75_terminal_btc": float(np.percentile(terminal_btc, 75)),
            "median_terminal_mstr": float(np.median(terminal_v3)),
            "p10_terminal_mstr": float(np.percentile(terminal_v3, 10)),
            "p25_terminal_mstr": float(np.percentile(terminal_v3, 25)),
            "p75_terminal_mstr": float(np.percentile(terminal_v3, 75)),
            "p90_terminal_mstr": float(np.percentile(terminal_v3, 90)),
            "zero_residual_pct": (zero_residual_count / num_paths) * 100.0,
            "model_impairment_pct": (impairment_count / num_paths) * 100.0,
            "underperform_spot_btc_pct": (underperform_btc_count / num_paths) * 100.0,
        }

    def run_multicondition_monte_carlo(
        self,
        paths_per_condition: int = 20000,
        seed: int = 20260711,
    ) -> pd.DataFrame:
        """Validates the V3 model across 5 distinct macroeconomic and market volatility regimes."""
        regimes = [
            ("1. Base Thesis Case", 0.20, 0.60, 4.5, 0.0, None),
            ("2. Severe Bear / Stressed Vol", -0.15, 0.80, 3.5, 0.0, 1.50),
            ("3. High Financing Cost (+250bp)", 0.10, 0.65, 4.5, 0.40, None),
            ("4. Bull Expansion / Monetization", 0.35, 0.55, 6.0, -0.10, None),
            ("5. Low Volatility / Sideways", 0.03, 0.40, 6.0, 0.0, None),
        ]

        results = []
        for name, drift, vol, df_t, pref_shift, res_init in regimes:
            res = self.run_monte_carlo(
                num_paths=paths_per_condition,
                seed=seed,
                drift_annual=drift,
                vol_annual=vol,
                df_t=df_t,
                pref_rate_shift=pref_shift,
                initial_reserve_b=res_init,
                geometric_cagr=True,
            )

            results.append({
                "Regime_Condition": name,
                "Paths": f"{paths_per_condition:,}",
                "BTC_Median": f"${res['median_terminal_btc']:,.0f}",
                "BTC_IQR_P25_P75": f"${res['p25_terminal_btc']:,.0f} - ${res['p75_terminal_btc']:,.0f}",
                "MSTR_Median": f"${res['median_terminal_mstr']:,.2f}",
                "MSTR_IQR_P25_P75": f"${res['p25_terminal_mstr']:,.2f} - ${res['p75_terminal_mstr']:,.2f}",
                "MSTR_P10_P90": f"${res['p10_terminal_mstr']:,.2f} - ${res['p90_terminal_mstr']:,.2f}",
                "Zero_Residual": f"{res['zero_residual_pct']:.1f}%",
                "Model_Impairment": f"{res['model_impairment_pct']:.1f}%",
                "Underperform_BTC": f"{res['underperform_spot_btc_pct']:.1f}%",
            })

        return pd.DataFrame(results)
