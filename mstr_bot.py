from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.strategy.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
MSTR_KPI_URL = "https://api.strategy.com/btc/mstrKpiData"
BITCOIN_KPI_URL = "https://api.strategy.com/btc/bitcoinKpis"
SHARES_URL = "https://www.strategy.com/shares"
PURCHASES_URL = "https://www.strategy.com/purchases"
DEBT_URL = "https://www.strategy.com/debt"
DEFAULT_STATE_FILE = Path("mstr_decision_engine_v2_state.json")
V2_SNAPSHOT_URL = "https://mstr-challenge-v2-production.nevets-steven.workers.dev/api/v2/snapshot"
LOCAL_OVERVIEW_PATH = Path("data/public/challenge_overview.json")
NORMAL_ACTIONS = ("STRONG BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL")


class StrategyDataError(RuntimeError):
    """Raised when Strategy data is missing, malformed, or unusable."""


@dataclass(frozen=True)
class DebtInstrument:
    amount_b: float
    effective_date: date
    maturity_date: date | None = None
    put_date: date | None = None


@dataclass(frozen=True)
class LatestPurchaseMetrics:
    as_of_date: date
    average_btc_cost: float
    btc_holdings: float | None
    diluted_shares_m: float | None
    btc_yield_qtd_pct: float | None
    btc_yield_ytd_pct: float


@dataclass(frozen=True)
class SourceMetadata:
    mstr_as_of: datetime | None = None
    btc_as_of: datetime | None = None
    shares_as_of: date | None = None
    purchase_as_of: date | None = None


@dataclass(frozen=True)
class StrategySnapshot:
    snapshot_date: date
    btc_price: float
    mstr_price: float
    btc_holdings: float
    average_btc_cost: float
    basic_shares_m: float
    diluted_shares_m: float
    btc_yield_ytd_pct: float
    market_cap_b: float
    enterprise_value_b: float
    debt_b: float
    preferred_b: float
    usd_reserve_b: float
    usd_div_coverage_months: float
    btc_div_coverage_years: float
    annual_dividends_b: float
    debt_instruments: tuple[DebtInstrument, ...]
    reported_metrics: Mapping[str, float] = field(default_factory=dict, compare=False)
    source_metadata: SourceMetadata = field(default_factory=SourceMetadata, compare=False)


@dataclass(frozen=True)
class FinancialMetrics:
    btc_nav_b: float
    basic_market_cap_b: float
    diluted_market_cap_b: float
    usd_reserve_b: float
    btc_per_share: float
    nav_per_basic_share: float
    nav_per_diluted_share: float
    dilution: float
    drawdown: float
    cost_premium: float
    unrealized_pl_b: float
    net_leverage: float
    amplification: float
    annual_fixed_charges_b: float
    coverage_implied_fixed_charges_b: float
    btc_div_coverage_calculated: float
    mnav_basic: float
    mnav_diluted: float
    current_ev_nav: float
    data_quality: float
    data_quality_checks: Mapping[str, float]


@dataclass(frozen=True)
class ZoneResult:
    fair_ev_nav: float
    fair_price: float
    uncertainty_band: float
    risk_score: float
    structural_floor: float
    strong_buy_mnav: float
    accumulate_mnav: float
    hold_mnav: float
    reduce_mnav: float
    strong_buy_price: float
    accumulate_price: float
    hold_price: float
    reduce_price: float
    maturity_pressure: float
    liquidity_score: float
    accretion_score: float
    tail_coverage_score: float


@dataclass(frozen=True)
class GateResult:
    data_invalid: bool
    strong_buy_blocked: bool
    distress: bool
    debt_under_12m_b: float


@dataclass(frozen=True)
class DecisionResult:
    action: str
    raw_action: str
    reason: str
    hysteresis_applied: bool = False


@dataclass(frozen=True)
class EngineRun:
    snapshot: StrategySnapshot
    metrics: FinancialMetrics
    zones: ZoneResult
    gates: GateResult
    decision: DecisionResult
    reused_zones: bool


def clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _parse_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^0-9.+-]", "", text.strip("()"))
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise StrategyDataError(f"Invalid numeric field {field_name}: {value!r}") from exc
    return -number if negative else number


def _require_number(value: Any, field_name: str, *, positive: bool) -> float:
    number = _parse_number(value, field_name)
    if number is None:
        raise StrategyDataError(f"Missing required Strategy field: {field_name}")
    if positive and number <= 0:
        raise StrategyDataError(f"Strategy field must be positive: {field_name}")
    return number


def _money_to_b(value: Any, field_name: str, default_unit: str) -> float | None:
    number = _parse_number(value, field_name)
    if number is None:
        return None
    if default_unit == "usd":
        return number / 1e9
    if default_unit == "m":
        return number / 1000
    if default_unit == "b":
        return number
    return number / 1e9 if abs(number) >= 10_000_000 else number / 1000 if abs(number) >= 1_000 else number


def _require_money_b(value: Any, field_name: str, default_unit: str, *, positive: bool) -> float:
    number = _money_to_b(value, field_name, default_unit)
    if number is None:
        raise StrategyDataError(f"Missing required Strategy field: {field_name}")
    if positive and number <= 0:
        raise StrategyDataError(f"Strategy field must be positive: {field_name}")
    return number


def _parse_date(value: Any, field_name: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise StrategyDataError(f"Invalid date field {field_name}: {value!r}") from exc


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise StrategyDataError(f"Invalid datetime field {field_name}: {value!r}")


def _normalize_shares_to_m(value: Any, field_name: str) -> float:
    shares = _require_number(value, field_name, positive=True)
    return shares / 1_000_000 if shares >= 10_000_000 else shares / 1_000 if shares >= 10_000 else shares


def _http_get_text(url: str) -> str:
    # 1. Primary: curl_cffi with Chrome TLS impersonation (bypasses Akamai/Cloudflare bot blocking)
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore

        response = cffi_requests.get(url, impersonate="chrome120", headers=HEADERS, timeout=25)
        response.raise_for_status()
        return response.text
    except ImportError:
        pass
    except Exception:
        pass

    # 2. Secondary fallback: standard requests / urllib
    try:
        import requests  # type: ignore

        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        return response.text
    except ImportError:
        request = Request(url, headers=HEADERS)
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        raise StrategyDataError(f"Failed to fetch Strategy source {url}: {exc}") from exc


def _http_get_json(url: str) -> Any:
    try:
        return json.loads(_http_get_text(url))
    except json.JSONDecodeError as exc:
        raise StrategyDataError(f"Strategy source returned invalid JSON: {url}") from exc


def _next_data_payload(url: str) -> Mapping[str, Any]:
    match = re.search(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", _http_get_text(url), re.DOTALL | re.IGNORECASE)
    if not match:
        raise StrategyDataError(f"Strategy page has no __NEXT_DATA__ payload: {url}")
    try:
        return json.loads(html_lib.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise StrategyDataError(f"Strategy page has invalid __NEXT_DATA__ payload: {url}") from exc


def _page_rows(payload: Mapping[str, Any], preferred_key: str, required_key: str) -> list[Mapping[str, Any]]:
    rows = payload.get("props", {}).get("pageProps", {}).get(preferred_key)
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, Mapping)]
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, list) and current and all(isinstance(row, Mapping) for row in current):
            if any(required_key in row for row in current):
                return list(current)
            stack.extend(current)
    raise StrategyDataError(f"Strategy page missing rows for {preferred_key}")


def fetch_live_btc_price_with_fallbacks() -> tuple[float, str]:
    """3-layer fallback for real-time Bitcoin spot price: Strategy.com -> CoinGecko -> Blockchain.info -> Yahoo."""
    # Layer 1: Strategy.com
    try:
        btc_payload = _http_get_json(BITCOIN_KPI_URL)
        if isinstance(btc_payload, Mapping) and isinstance(btc_payload.get("results"), Mapping):
            price = float(btc_payload["results"].get("ufPrice") or btc_payload["results"].get("latestPrice") or 0)
            if price > 0:
                return price, "Strategy.com (Primary)"
    except Exception:
        pass

    # Layer 2: CoinGecko Free Public API
    try:
        req = Request("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", headers=HEADERS)
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["bitcoin"]["usd"])
            if price > 0:
                return price, "CoinGecko (Fallback 1)"
    except Exception:
        pass

    # Layer 3: Blockchain.info Ticker Free API
    try:
        req = Request("https://blockchain.info/ticker", headers=HEADERS)
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["USD"]["last"])
            if price > 0:
                return price, "Blockchain.info (Fallback 2)"
    except Exception:
        pass

    # Layer 4 (Safety Net): Yahoo Finance BTC-USD
    try:
        req = Request("https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1d&interval=1d", headers=HEADERS)
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if price > 0:
                return price, "Yahoo Finance (Fallback 3)"
    except Exception:
        pass

    return 79500.0, "Safety Baseline"


def fetch_live_mstr_price_with_fallbacks() -> tuple[float, str]:
    """3-layer fallback for real-time MSTR stock price: Strategy.com -> Yahoo Query 1 -> Yahoo Query 2."""
    # Layer 1: Strategy.com
    try:
        mstr_payload = _http_get_json(MSTR_KPI_URL)
        if isinstance(mstr_payload, list) and mstr_payload:
            price = float(mstr_payload[0].get("ufPrice") or mstr_payload[0].get("price") or 0)
            if price > 0:
                return price, "Strategy.com (Primary)"
    except Exception:
        pass

    # Layer 2: Yahoo Finance Query 1
    try:
        req = Request("https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=1d&interval=1d", headers=HEADERS)
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if price > 0:
                return price, "Yahoo Finance Q1 (Fallback 1)"
    except Exception:
        pass

    # Layer 3: Yahoo Finance Query 2
    try:
        req = Request("https://query2.finance.yahoo.com/v8/finance/chart/MSTR?range=1d&interval=1d", headers=HEADERS)
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if price > 0:
                return price, "Yahoo Finance Q2 (Fallback 2)"
    except Exception:
        pass

    return 136.52, "Safety Baseline"


def fetch_dashboard_data(state_path: Path | None = DEFAULT_STATE_FILE) -> Mapping[str, Any]:
    # 1. Try primary Strategy.com dashboard
    try:
        mstr_payload, btc_payload = _http_get_json(MSTR_KPI_URL), _http_get_json(BITCOIN_KPI_URL)
        if isinstance(mstr_payload, list) and mstr_payload and isinstance(btc_payload, Mapping) and isinstance(btc_payload.get("results"), Mapping):
            return {"mstr": mstr_payload[0], "btc": btc_payload["results"], "btc_timestamp": btc_payload.get("timestamp")}
    except Exception as exc:
        print(f"::warning::Strategy.com dashboard fetch failed ({exc}). Activating multi-layer fallbacks.", file=sys.stderr)

    # 2. Resilient Fallback: Multi-layer free APIs + cached state fundamentals
    live_btc_price, btc_src = fetch_live_btc_price_with_fallbacks()
    live_mstr_price, mstr_src = fetch_live_mstr_price_with_fallbacks()
    print(f"::notice::Fallback active: BTC from {btc_src} (${live_btc_price:,.2f}), MSTR from {mstr_src} (${live_mstr_price:,.2f})", file=sys.stderr)

    cached_fingerprint = _load_cached_fingerprint(state_path)
    basic_shares_m = float(cached_fingerprint.get("basic_shares_m", 420.497))
    debt_b = float(cached_fingerprint.get("debt_b", 6.714))
    pref_b = float(cached_fingerprint.get("preferred_b", 14.625))
    reserve_b = float(cached_fingerprint.get("usd_reserve_b", 6.538))
    btc_holdings = float(cached_fingerprint.get("btc_holdings", 845050.0))
    usd_coverage = float(cached_fingerprint.get("usd_div_coverage_months", 47.21))
    btc_coverage = float(cached_fingerprint.get("btc_div_coverage_years", 40.44))
    annual_divs = float(cached_fingerprint.get("annual_dividends_b", 1.662))

    market_cap_b = live_mstr_price * basic_shares_m / 1000.0
    enterprise_value_b = market_cap_b + debt_b + pref_b - reserve_b
    btc_nav_b = (btc_holdings * live_btc_price) / 1e9

    mstr_synthetic = {
        "ufPrice": live_mstr_price,
        "price": live_mstr_price,
        "marketCap": market_cap_b * 1000.0,
        "entVal": enterprise_value_b * 1000.0,
        "debt": debt_b * 1000.0,
        "pref": pref_b * 1000.0,
    }
    btc_synthetic = {
        "ufPrice": live_btc_price,
        "latestPrice": live_btc_price,
        "btcHoldings": btc_holdings,
        "btcNavNumber": btc_nav_b * 1000.0,
        "usdMonthsOfDividends": usd_coverage,
        "btcYearsOfDividends": btc_coverage,
        "totalAnnualDividends": annual_divs * 1e9,
        "debtByBN": (debt_b / btc_nav_b) * 100.0 if btc_nav_b > 0 else 0.0,
        "debtPrefByBN": ((debt_b + pref_b) / btc_nav_b) * 100.0 if btc_nav_b > 0 else 0.0,
    }
    return {"mstr": mstr_synthetic, "btc": btc_synthetic, "btc_timestamp": datetime.now(timezone.utc).isoformat()}


def fetch_shares_data() -> Mapping[str, Any]:
    latest = max(_page_rows(_next_data_payload(SHARES_URL), "shares", "basic_shares_outstanding"), key=lambda row: str(row.get("date", "")))
    return {
        "shares_as_of": _parse_date(latest.get("date"), "shares.date"),
        "basic_shares_m": _normalize_shares_to_m(latest.get("basic_shares_outstanding"), "basic_shares_outstanding"),
        "diluted_shares_m": _normalize_shares_to_m(latest.get("assumed_diluted_shares_outstanding"), "assumed_diluted_shares_outstanding"),
    }


def fetch_latest_average_btc_cost() -> LatestPurchaseMetrics:
    rows = _page_rows(_next_data_payload(PURCHASES_URL), "bitcoinData", "date_of_purchase")
    latest = max(rows, key=lambda row: str(row.get("date_of_purchase", "")))
    as_of_date = _parse_date(latest.get("date_of_purchase"), "date_of_purchase")
    if as_of_date is None:
        raise StrategyDataError("Latest purchase record missing date_of_purchase")
    return LatestPurchaseMetrics(
        as_of_date=as_of_date,
        average_btc_cost=_require_number(latest.get("average_price"), "average_price", positive=True),
        btc_holdings=_parse_number(latest.get("btc_holdings"), "purchase.btc_holdings"),
        diluted_shares_m=_normalize_shares_to_m(latest.get("assumed_diluted_shares_outstanding"), "purchase.assumed_diluted_shares_outstanding")
        if latest.get("assumed_diluted_shares_outstanding") is not None
        else None,
        btc_yield_qtd_pct=_parse_number(latest.get("btc_yield_qtd"), "btc_yield_qtd"),
        btc_yield_ytd_pct=_require_number(latest.get("btc_yield_ytd"), "btc_yield_ytd", positive=False),
    )


def fetch_debt_instruments() -> tuple[DebtInstrument, ...]:
    instruments: list[DebtInstrument] = []
    for row in _page_rows(_next_data_payload(DEBT_URL), "convertData", "notional"):
        amount_b = _require_money_b(row.get("notional"), "notional", "usd", positive=True)
        maturity_date, put_date = _parse_date(row.get("maturity_date"), "maturity_date"), _parse_date(row.get("put_date"), "put_date")
        effective_candidates = [candidate for candidate in (put_date, maturity_date) if candidate is not None]
        if not effective_candidates:
            raise StrategyDataError("Debt instrument missing maturity_date and put_date")
        instruments.append(DebtInstrument(amount_b, min(effective_candidates), maturity_date, put_date))
    return tuple(instruments)


def _wib_today() -> date:
    return datetime.now(timezone(timedelta(hours=7))).date()


def _load_cached_fingerprint(state_path: Path | None) -> Mapping[str, Any]:
    if not state_path or not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping) and isinstance(payload.get("fingerprint"), Mapping):
            return payload["fingerprint"]
    except Exception:
        pass
    return {}


def fetch_strategy_snapshot(
    snapshot_date: date | None = None,
    state_path: Path | None = DEFAULT_STATE_FILE,
) -> StrategySnapshot:
    dashboard = fetch_dashboard_data(state_path=state_path)
    cached_fingerprint = _load_cached_fingerprint(state_path)

    try:
        shares = fetch_shares_data()
    except Exception as exc:
        if cached_fingerprint.get("basic_shares_m") and cached_fingerprint.get("diluted_shares_m"):
            print(f"::warning::Failed to fetch shares data from strategy.com ({exc}). Using Layer 2 cached state fundamentals.")
            shares = {
                "shares_as_of": None,
                "basic_shares_m": float(cached_fingerprint["basic_shares_m"]),
                "diluted_shares_m": float(cached_fingerprint["diluted_shares_m"]),
            }
        else:
            print(f"::warning::Failed to fetch shares data ({exc}) and no cache. Using Layer 3 SEC baseline fundamentals.")
            shares = {
                "shares_as_of": None,
                "basic_shares_m": 420.497,
                "diluted_shares_m": 450.121,
            }

    try:
        latest_purchase = fetch_latest_average_btc_cost()
    except Exception as exc:
        if cached_fingerprint.get("average_btc_cost"):
            print(f"::warning::Failed to fetch purchase data from strategy.com ({exc}). Using Layer 2 cached state fundamentals.")
            latest_purchase = LatestPurchaseMetrics(
                as_of_date=snapshot_date or _wib_today(),
                average_btc_cost=float(cached_fingerprint["average_btc_cost"]),
                btc_holdings=float(cached_fingerprint.get("btc_holdings", 845050.0)),
                diluted_shares_m=float(cached_fingerprint.get("diluted_shares_m", 450.121)),
                btc_yield_qtd_pct=None,
                btc_yield_ytd_pct=float(cached_fingerprint.get("btc_yield_ytd_pct", -3.7)),
            )
        else:
            print(f"::warning::Failed to fetch purchase data ({exc}) and no cache. Using Layer 3 SEC purchase baseline.")
            latest_purchase = LatestPurchaseMetrics(
                as_of_date=snapshot_date or _wib_today(),
                average_btc_cost=75412.0,
                btc_holdings=845050.0,
                diluted_shares_m=450.121,
                btc_yield_qtd_pct=-11.8,
                btc_yield_ytd_pct=-3.7,
            )

    try:
        debt_instruments = fetch_debt_instruments()
    except Exception as exc:
        raw_schedule = cached_fingerprint.get("debt_schedule", [])
        if raw_schedule:
            print(f"::warning::Failed to fetch debt instruments from strategy.com ({exc}). Using Layer 2 cached debt schedule.")
            debt_instruments = tuple(
                DebtInstrument(
                    amount_b=float(item["amount_b"]),
                    effective_date=_parse_date(item["effective_date"], "effective_date") or _wib_today(),
                    maturity_date=_parse_date(item.get("maturity_date"), "maturity_date") if item.get("maturity_date") else None,
                    put_date=_parse_date(item.get("put_date"), "put_date") if item.get("put_date") else None,
                )
                for item in raw_schedule
            )
        else:
            print(f"::warning::Failed to fetch debt instruments ({exc}) and no cache. Using Layer 3 SEC baseline schedule.")
            debt_instruments = (
                DebtInstrument(1.01, date(2027, 9, 16), date(2028, 9, 16), date(2027, 9, 16)),
                DebtInstrument(1.50, date(2028, 6, 2), date(2029, 12, 2), date(2028, 6, 2)),
                DebtInstrument(2.00, date(2028, 3, 2), date(2030, 3, 2), date(2028, 3, 2)),
                DebtInstrument(0.80, date(2028, 9, 16), date(2030, 3, 16), date(2028, 9, 16)),
                DebtInstrument(0.60375, date(2028, 9, 16), date(2031, 3, 16), date(2028, 9, 16)),
                DebtInstrument(0.80, date(2029, 6, 16), date(2032, 6, 16), date(2029, 6, 16)),
            )

    mstr, btc = dashboard["mstr"], dashboard["btc"]
    market_cap_b = _require_money_b(mstr.get("marketCap"), "marketCap", "m", positive=True)
    enterprise_value_b = _require_money_b(mstr.get("entVal"), "entVal", "m", positive=True)
    debt_b = _require_money_b(mstr.get("debt"), "debt", "m", positive=False)
    preferred_b = _require_money_b(mstr.get("pref"), "pref", "m", positive=False)
    btc_holdings = _require_number(btc.get("btcHoldings"), "btc_holdings", positive=True)
    reported_metrics = {
        "btc_nav_b": _money_to_b(btc.get("btcNavNumber"), "btcNavNumber", "m"),
        "btc_per_share": (_parse_number(btc.get("satsPerShare"), "satsPerShare") or 0) / 1e8 if btc.get("satsPerShare") is not None else None,
        "net_leverage": (_parse_number(btc.get("debtByBN"), "debtByBN") or 0) / 100 if btc.get("debtByBN") is not None else None,
        "amplification": (_parse_number(btc.get("debtPrefByBN"), "debtPrefByBN") or 0) / 100 if btc.get("debtPrefByBN") is not None else None,
    }
    snapshot = StrategySnapshot(
        snapshot_date=snapshot_date or _wib_today(),
        btc_price=_require_number(btc.get("ufPrice"), "btc_price", positive=True),
        mstr_price=_require_number(mstr.get("ufPrice"), "mstr_price", positive=True),
        btc_holdings=btc_holdings,
        average_btc_cost=latest_purchase.average_btc_cost,
        basic_shares_m=shares["basic_shares_m"],
        diluted_shares_m=shares["diluted_shares_m"],
        btc_yield_ytd_pct=latest_purchase.btc_yield_ytd_pct,
        market_cap_b=market_cap_b,
        enterprise_value_b=enterprise_value_b,
        debt_b=debt_b,
        preferred_b=preferred_b,
        usd_reserve_b=market_cap_b + debt_b + preferred_b - enterprise_value_b,
        usd_div_coverage_months=_require_number(btc.get("usdMonthsOfDividends"), "usdMonthsOfDividends", positive=False),
        btc_div_coverage_years=_require_number(btc.get("btcYearsOfDividends"), "btcYearsOfDividends", positive=False),
        annual_dividends_b=_require_money_b(btc.get("totalAnnualDividends"), "totalAnnualDividends", "usd", positive=False),
        debt_instruments=debt_instruments,
        reported_metrics={key: value for key, value in reported_metrics.items() if value is not None},
        source_metadata=SourceMetadata(
            mstr_as_of=_parse_datetime(mstr.get("timeStampUtc") or mstr.get("timeStamp"), "mstr.timestamp"),
            btc_as_of=_parse_datetime(dashboard.get("btc_timestamp"), "btc.timestamp"),
            shares_as_of=shares.get("shares_as_of"),
            purchase_as_of=latest_purchase.as_of_date,
        ),
    )
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: StrategySnapshot) -> None:
    for field_name in (
        "btc_price",
        "mstr_price",
        "btc_holdings",
        "average_btc_cost",
        "basic_shares_m",
        "diluted_shares_m",
        "market_cap_b",
        "enterprise_value_b",
    ):
        value = getattr(snapshot, field_name)
        if value is None:
            raise StrategyDataError(f"Missing required Strategy field: {field_name}")
        if value <= 0:
            raise StrategyDataError(f"Strategy field must be positive: {field_name}")
    for field_name in ("debt_b", "preferred_b", "usd_reserve_b", "usd_div_coverage_months", "btc_div_coverage_years", "annual_dividends_b"):
        if getattr(snapshot, field_name) is None:
            raise StrategyDataError(f"Missing required Strategy field: {field_name}")


def _relative_error(calculated: float, reported: float | None) -> float | None:
    return None if reported is None or not math.isfinite(calculated) or not math.isfinite(reported) else abs(calculated - reported) / max(abs(reported), 1e-12)


def _data_quality(snapshot: StrategySnapshot, values: Mapping[str, float]) -> tuple[float, Mapping[str, float]]:
    reported = dict(snapshot.reported_metrics)
    reported.setdefault("btc_div_coverage_calculated", snapshot.btc_div_coverage_years)
    allowed_checks = (
        "btc_nav_b",
        "btc_per_share",
        "dilution",
        "drawdown",
        "cost_premium",
        "unrealized_pl_b",
        "net_leverage",
        "amplification",
        "btc_div_coverage_calculated",
    )
    calculated = {key: values[key] for key in allowed_checks}
    checks = {key: error for key, value in calculated.items() if (error := _relative_error(value, reported.get(key))) is not None}
    average_error = sum(checks.values()) / len(checks) if checks else 0.0
    return clip(1 - 4 * average_error, 0, 1), checks


def calculate_financial_metrics(snapshot: StrategySnapshot) -> FinancialMetrics:
    validate_snapshot(snapshot)
    btc_nav_b = snapshot.btc_price * snapshot.btc_holdings / 1e9
    basic_market_cap_b = snapshot.mstr_price * snapshot.basic_shares_m / 1000
    diluted_market_cap_b = snapshot.mstr_price * snapshot.diluted_shares_m / 1000
    usd_reserve_b = round(snapshot.market_cap_b + snapshot.debt_b + snapshot.preferred_b - snapshot.enterprise_value_b, 12)
    btc_per_share = snapshot.btc_holdings / (snapshot.diluted_shares_m * 1e6)
    dilution = (snapshot.diluted_shares_m - snapshot.basic_shares_m) / snapshot.basic_shares_m
    drawdown = snapshot.btc_price / snapshot.average_btc_cost - 1
    annual_fixed_charges_b = snapshot.annual_dividends_b
    coverage_implied_fixed_charges_b = usd_reserve_b * 12 / snapshot.usd_div_coverage_months if snapshot.usd_div_coverage_months > 0 else math.inf
    btc_div_coverage_calculated = btc_nav_b / annual_fixed_charges_b if annual_fixed_charges_b > 0 and math.isfinite(annual_fixed_charges_b) else 0.0
    values = {
        "btc_nav_b": btc_nav_b,
        "basic_market_cap_b": basic_market_cap_b,
        "diluted_market_cap_b": diluted_market_cap_b,
        "btc_per_share": btc_per_share,
        "dilution": dilution,
        "drawdown": drawdown,
        "cost_premium": snapshot.average_btc_cost / snapshot.btc_price - 1,
        "unrealized_pl_b": snapshot.btc_holdings * (snapshot.btc_price - snapshot.average_btc_cost) / 1e9,
        "net_leverage": (snapshot.debt_b - usd_reserve_b) / btc_nav_b,
        "amplification": (snapshot.debt_b + snapshot.preferred_b) / btc_nav_b,
        "btc_div_coverage_calculated": btc_div_coverage_calculated,
    }
    data_quality, checks = _data_quality(snapshot, values)
    return FinancialMetrics(
        btc_nav_b,
        basic_market_cap_b,
        diluted_market_cap_b,
        usd_reserve_b,
        btc_per_share,
        btc_nav_b * 1000 / snapshot.basic_shares_m,
        btc_nav_b * 1000 / snapshot.diluted_shares_m,
        dilution,
        drawdown,
        values["cost_premium"],
        values["unrealized_pl_b"],
        values["net_leverage"],
        values["amplification"],
        annual_fixed_charges_b,
        coverage_implied_fixed_charges_b,
        btc_div_coverage_calculated,
        basic_market_cap_b / btc_nav_b,
        diluted_market_cap_b / btc_nav_b,
        snapshot.enterprise_value_b / btc_nav_b,
        data_quality,
        checks,
    )


def fetch_btc_12m_metrics() -> tuple[float, float]:
    """3-layer fallback for trailing 12-month BTC momentum and realized volatility."""
    # Layer 1 & 2: Yahoo Finance Query 1 & Query 2
    for url in (
        "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1y&interval=1d",
        "https://query2.finance.yahoo.com/v8/finance/chart/BTC-USD?range=1y&interval=1d",
    ):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                quote = payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                closes = [float(x) for x in quote if x is not None]
                if len(closes) >= 90:
                    log_ret = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                    mom = math.exp(sum(log_ret)) - 1.0
                    vol = (sum((r - mean(log_ret)) ** 2 for r in log_ret) / (len(log_ret) - 1)) ** 0.5 * math.sqrt(365)
                    return mom, vol
        except Exception:
            continue

    # Layer 3: Local historical dataset
    csv_path = Path("data/historical_mstr_btc_2020_2026.csv")
    if csv_path.exists():
        try:
            import csv

            with csv_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                closes = [float(row["btc_close"]) for row in reader if row.get("btc_close")]
                if len(closes) >= 90:
                    recent = closes[-365:] if len(closes) >= 365 else closes
                    log_ret = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
                    mom = math.exp(sum(log_ret)) - 1.0
                    vol = (sum((r - mean(log_ret)) ** 2 for r in log_ret) / (len(log_ret) - 1)) ** 0.5 * math.sqrt(365)
                    return mom, vol
        except Exception:
            pass

    # Layer 4: Mathematical statistical baseline
    return 0.18, 0.65


def calculate_zones(snapshot: StrategySnapshot, metrics: FinancialMetrics) -> ZoneResult:
    momentum_12m, realized_vol_12m = fetch_btc_12m_metrics()

    # 1. State-dependent beta (0.45 in bull momentum > 50%, else 0.35)
    beta = 0.45 if momentum_12m > 0.50 else 0.35

    # 2. Continuous liquidity penalty (scales from 0 at >=15m down to -0.30 at 0m)
    liquidity_penalty = 0.30 * clip((15.0 - snapshot.usd_div_coverage_months) / 15.0, 0.0, 1.0)

    # 3. Volatility spread adjustment (relative to base 65%)
    vol_adj = 0.20 * (realized_vol_12m - 0.65)

    # 4. Continuous, regime-aware dynamic mNAV (Thesis Section 8.7 upgraded to 2.20x ceiling)
    raw_dynamic_mnav = 1.0 + beta * math.tanh(momentum_12m) - vol_adj - liquidity_penalty
    fair_ev_nav = clip(raw_dynamic_mnav, 0.50, 2.20)

    # 5. Software business operating floor ($1.0B baseline)
    software_floor_b = 1.0
    net_senior_claims_b = snapshot.debt_b + snapshot.preferred_b - metrics.usd_reserve_b
    structural_floor = max(0.0, (net_senior_claims_b - software_floor_b) / max(metrics.btc_nav_b, 1e-6))

    # Helper for Residual / ADSO valuation bridge using diluted shares (ADSO)
    def price_from_ev_nav(multiple: float) -> float:
        equity_b = max(0.0, multiple * metrics.btc_nav_b - snapshot.debt_b - snapshot.preferred_b + metrics.usd_reserve_b + software_floor_b)
        return equity_b * 1000.0 / max(snapshot.diluted_shares_m, 1e-6)

    fair_price = price_from_ev_nav(fair_ev_nav)

    # Sub-scores for thesis reporting & backward-compatible audit contracts
    days_elapsed = (snapshot.snapshot_date - date(snapshot.snapshot_date.year, 1, 1)).days + 1
    ytd_yield = snapshot.btc_yield_ytd_pct / 100
    annualized_yield = -1.0 if ytd_yield <= -1 else (1 + ytd_yield) ** (365 / max(days_elapsed, 1)) - 1
    accretion_score = math.tanh(annualized_yield / 0.25)
    liquidity_score = clip((snapshot.usd_div_coverage_months - 3) / 15, 0, 1)
    maturity_pressure = (
        0.0
        if snapshot.debt_b <= 0
        else sum(
            (instrument.amount_b / snapshot.debt_b) * math.exp(-max((instrument.effective_date - snapshot.snapshot_date).days / 365.25, 0.10) / 2.5)
            for instrument in snapshot.debt_instruments
        )
    )
    maturity_pressure = clip(maturity_pressure, 0, 1)
    tail_coverage_score = clip(math.log(max(snapshot.btc_div_coverage_years, 1e-9) / 5) / math.log(40 / 5), 0, 1)
    risk_score = clip(
        0.20 * (1 - liquidity_score)
        + 0.15 * maturity_pressure
        + 0.35 * (1.0 - clip(fair_ev_nav / 1.50, 0, 1)),
        0,
        1,
    )
    uncertainty_band = clip(0.10 + 0.18 * risk_score + 0.06 * (1 - metrics.data_quality), 0.10, 0.35)

    # Full Dynamic Uncertainty & Equilibrium Dispersion Bands
    strong_buy_mnav = max(structural_floor + 0.10, fair_ev_nav - 1.50 * uncertainty_band)
    accumulate_mnav = fair_ev_nav
    hold_mnav = max(accumulate_mnav + 0.02, fair_ev_nav + 1.75 * uncertainty_band)
    reduce_mnav = max(hold_mnav + 0.02, fair_ev_nav + 3.00 * uncertainty_band)

    strong_buy_price = price_from_ev_nav(strong_buy_mnav)
    accumulate_price = price_from_ev_nav(accumulate_mnav)
    hold_price = price_from_ev_nav(hold_mnav)
    reduce_price = price_from_ev_nav(reduce_mnav)

    return ZoneResult(
        fair_ev_nav=fair_ev_nav,
        fair_price=fair_price,
        uncertainty_band=uncertainty_band,
        risk_score=risk_score,
        structural_floor=structural_floor,
        strong_buy_mnav=strong_buy_mnav,
        accumulate_mnav=accumulate_mnav,
        hold_mnav=hold_mnav,
        reduce_mnav=reduce_mnav,
        strong_buy_price=strong_buy_price,
        accumulate_price=accumulate_price,
        hold_price=hold_price,
        reduce_price=reduce_price,
        maturity_pressure=maturity_pressure,
        liquidity_score=liquidity_score,
        accretion_score=accretion_score,
        tail_coverage_score=tail_coverage_score,
    )


def calculate_gates(snapshot: StrategySnapshot, metrics: FinancialMetrics, zones: ZoneResult) -> GateResult:
    debt_under_12m_b = sum(
        instrument.amount_b for instrument in snapshot.debt_instruments if instrument.effective_date <= snapshot.snapshot_date + timedelta(days=365)
    )
    return GateResult(
        metrics.data_quality < 0.75,
        metrics.data_quality < 0.90
        or snapshot.usd_div_coverage_months < 6
        or snapshot.btc_div_coverage_years < 10
        or zones.structural_floor >= 0.70
        or debt_under_12m_b > metrics.usd_reserve_b,
        snapshot.usd_div_coverage_months < 3 or snapshot.btc_div_coverage_years < 5 or zones.structural_floor >= 0.85,
        debt_under_12m_b,
    )


def _raw_classification(mstr_price: float, zones: ZoneResult, gates: GateResult) -> tuple[str, str]:
    if gates.data_invalid:
        return "MODEL INVALID", "Data quality is below 75%."
    if gates.distress:
        return "DISTRESS / SPECIAL SITUATION", "Coverage or structural floor triggered distress gates."
    if mstr_price <= zones.strong_buy_price:
        return (
            ("ACCUMULATE", "Strong Buy valuation is present, but hard gates block Strong Buy.")
            if gates.strong_buy_blocked
            else ("STRONG BUY", "Price is below the Strong Buy boundary.")
        )
    if mstr_price <= zones.accumulate_price:
        return "ACCUMULATE", "Price is inside the Accumulate zone."
    if mstr_price <= zones.hold_price:
        return "HOLD", "Price is inside the Hold zone."
    if mstr_price <= zones.reduce_price:
        return "REDUCE", "Price is inside the Reduce zone."
    return "SELL", "Price is above the Reduce ceiling."


def _transition_boundary_price(lower_action: str, upper_action: str, zones: ZoneResult) -> float:
    return {
        ("STRONG BUY", "ACCUMULATE"): zones.strong_buy_price,
        ("ACCUMULATE", "HOLD"): zones.accumulate_price,
        ("HOLD", "REDUCE"): zones.hold_price,
        ("REDUCE", "SELL"): zones.reduce_price,
    }[(lower_action, upper_action)]


def classify_price(mstr_price: float, zones: ZoneResult, gates: GateResult, previous_action: str | None = None, hysteresis_pct: float = 0.02) -> DecisionResult:
    raw_action, reason = _raw_classification(mstr_price, zones, gates)
    if raw_action not in NORMAL_ACTIONS or previous_action not in NORMAL_ACTIONS:
        return DecisionResult(raw_action, raw_action, reason)
    raw_index, previous_index = NORMAL_ACTIONS.index(raw_action), NORMAL_ACTIONS.index(previous_action)
    if raw_index == previous_index:
        return DecisionResult(raw_action, raw_action, reason)
    direction = 1 if raw_index > previous_index else -1
    next_index = previous_index + direction
    lower, upper = NORMAL_ACTIONS[min(previous_index, next_index)], NORMAL_ACTIONS[max(previous_index, next_index)]
    boundary = _transition_boundary_price(lower, upper, zones)
    allowed = mstr_price > boundary * (1 + hysteresis_pct) if direction > 0 else mstr_price < boundary * (1 - hysteresis_pct)
    if allowed:
        return DecisionResult(NORMAL_ACTIONS[next_index], raw_action, reason)
    return DecisionResult(previous_action, raw_action, f"2% hysteresis buffer retained prior action: {previous_action}.", True)


def fundamental_fingerprint(snapshot: StrategySnapshot) -> Mapping[str, Any]:
    debt_schedule = [
        {
            "amount_b": round(instrument.amount_b, 6),
            "effective_date": instrument.effective_date.isoformat(),
            "maturity_date": instrument.maturity_date.isoformat() if instrument.maturity_date else None,
            "put_date": instrument.put_date.isoformat() if instrument.put_date else None,
        }
        for instrument in sorted(snapshot.debt_instruments, key=lambda item: (item.effective_date, item.amount_b))
    ]
    return {
        "btc_price": round(snapshot.btc_price, 6),
        "btc_holdings": round(snapshot.btc_holdings, 6),
        "average_btc_cost": round(snapshot.average_btc_cost, 6),
        "basic_shares_m": round(snapshot.basic_shares_m, 6),
        "diluted_shares_m": round(snapshot.diluted_shares_m, 6),
        "btc_yield_ytd_pct": round(snapshot.btc_yield_ytd_pct, 6),
        "debt_b": round(snapshot.debt_b, 6),
        "preferred_b": round(snapshot.preferred_b, 6),
        "usd_reserve_b": round(snapshot.usd_reserve_b, 6),
        "usd_div_coverage_months": round(snapshot.usd_div_coverage_months, 6),
        "btc_div_coverage_years": round(snapshot.btc_div_coverage_years, 6),
        "annual_dividends_b": round(snapshot.annual_dividends_b, 6),
        "debt_schedule": debt_schedule,
    }


def fingerprint_hash(snapshot: StrategySnapshot) -> str:
    return hashlib.sha256(json.dumps(fundamental_fingerprint(snapshot), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_state(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, snapshot: StrategySnapshot, zones: ZoneResult, action: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint_hash": fingerprint_hash(snapshot),
        "fingerprint": fundamental_fingerprint(snapshot),
        "zones": asdict(zones),
        "last_action": action,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def evaluate_snapshot(snapshot: StrategySnapshot, state_path: Path | None = DEFAULT_STATE_FILE) -> EngineRun:
    metrics, state, current_hash = calculate_financial_metrics(snapshot), load_state(state_path) if state_path else {}, fingerprint_hash(snapshot)
    reused_zones = state.get("fingerprint_hash") == current_hash and isinstance(state.get("zones"), Mapping)
    zones = (
        ZoneResult(**{name: float(state["zones"][name]) for name in ZoneResult.__dataclass_fields__}) if reused_zones else calculate_zones(snapshot, metrics)
    )
    gates = calculate_gates(snapshot, metrics, zones)
    previous_action = state.get("last_action") if isinstance(state.get("last_action"), str) else None
    decision = classify_price(snapshot.mstr_price, zones, gates, previous_action=previous_action)
    if state_path:
        save_state(state_path, snapshot, zones, decision.action)
    return EngineRun(snapshot, metrics, zones, gates, decision, reused_zones)


def maturity_buckets(snapshot: StrategySnapshot) -> Mapping[str, float]:
    buckets = {"<12M": 0.0, "2028": 0.0, "2029": 0.0, "2030": 0.0, "2031+": 0.0}
    cutoff = snapshot.snapshot_date + timedelta(days=365)
    for instrument in snapshot.debt_instruments:
        if instrument.maturity_date is None:
            continue
        if instrument.maturity_date <= cutoff:
            buckets["<12M"] += instrument.amount_b
            continue
        year = instrument.maturity_date.year
        if str(year) in buckets:
            buckets[str(year)] += instrument.amount_b
        elif year >= 2031:
            buckets["2031+"] += instrument.amount_b
    return buckets


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_b(value: float) -> str:
    return f"${value:,.2f}B"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:,.1f}%"


def _fmt_optional(value: float | None, suffix: str = "", decimals: int = 2) -> str:
    return "N/A" if value is None else f"{value:,.{decimals}f}{suffix}"


def _fmt_as_of(value: datetime | date | None) -> str:
    return value.strftime("%d %b") if value else "N/A"


def adaptive_btc_scenario_levels(current_btc_price: float) -> list[float]:
    levels = {
        round(current_btc_price * 0.80 / 5000) * 5000,
        round(current_btc_price * 0.90 / 5000) * 5000,
        round(current_btc_price / 5000) * 5000,
        round(current_btc_price * 1.20 / 5000) * 5000,
        round(current_btc_price * 1.50 / 5000) * 5000,
    }
    return sorted(list(levels))

def calculate_adaptive_btc_scenario(
    snapshot: StrategySnapshot,
    live_metrics: FinancialMetrics,
    scenario_btc_price: float,
) -> tuple[FinancialMetrics, ZoneResult]:
    scenario_snapshot = replace(snapshot, btc_price=scenario_btc_price)
    scenario_metrics = calculate_financial_metrics(scenario_snapshot)
    scenario_snapshot = replace(
        scenario_snapshot,
        btc_div_coverage_years=scenario_metrics.btc_div_coverage_calculated,
    )
    scenario_metrics = calculate_financial_metrics(scenario_snapshot)
    
    # Data quality is preserved from live, not hypothetical scenario
    scenario_metrics = replace(
        scenario_metrics,
        data_quality=live_metrics.data_quality,
        data_quality_checks=live_metrics.data_quality_checks,
    )
    
    scenario_zones = calculate_zones(scenario_snapshot, scenario_metrics)
    return scenario_metrics, scenario_zones


def _audit_pair(calculated: float, reported: float | None, suffix: str = "") -> str:
    if reported is None:
        return f"calculated {calculated:,.6f}{suffix} | reported N/A"
    diff = calculated - reported
    rel = abs(diff) / max(abs(reported), 1e-12)
    return f"calculated {calculated:,.6f}{suffix} | reported {reported:,.6f}{suffix} | diff {diff:,.6f}{suffix} ({rel:.3%})"


def format_source_audit() -> str:
    dashboard = fetch_dashboard_data()
    shares = fetch_shares_data()
    latest_purchase = fetch_latest_average_btc_cost()
    debt_instruments = fetch_debt_instruments()
    snapshot = fetch_strategy_snapshot()
    metrics = calculate_financial_metrics(snapshot)
    mstr, btc = dashboard["mstr"], dashboard["btc"]
    tracked_convertible_notional_b = sum(instrument.amount_b for instrument in debt_instruments)
    untracked_debt_b = max(snapshot.debt_b - tracked_convertible_notional_b, 0)
    purchase_holdings_delta = None if latest_purchase.btc_holdings is None else latest_purchase.btc_holdings - snapshot.btc_holdings
    purchase_diluted_delta = None if latest_purchase.diluted_shares_m is None else latest_purchase.diluted_shares_m - shares["diluted_shares_m"]
    return f"""
SOURCE AUDIT

MSTR API
As of: {_fmt_as_of(snapshot.source_metadata.mstr_as_of)}
Price: {_fmt_usd(snapshot.mstr_price)}
Market cap: {_fmt_b(snapshot.market_cap_b)}
Enterprise value: {_fmt_b(snapshot.enterprise_value_b)}
Debt: {_fmt_b(snapshot.debt_b)}
Preferred: {_fmt_b(snapshot.preferred_b)}

BTC API
As of: {_fmt_as_of(snapshot.source_metadata.btc_as_of)}
BTC price: {_fmt_usd(snapshot.btc_price)}
Holdings: {snapshot.btc_holdings:,.0f}
BTC NAV: {_fmt_b(_money_to_b(btc.get("btcNavNumber"), "btcNavNumber", "m") or 0)}
BTC/share: {snapshot.reported_metrics.get("btc_per_share", 0):.8f}
Net leverage: {_fmt_pct(snapshot.reported_metrics.get("net_leverage", 0))}
Amplification: {_fmt_pct(snapshot.reported_metrics.get("amplification", 0))}
BTC coverage: {snapshot.btc_div_coverage_years:.2f} years
USD coverage: {snapshot.usd_div_coverage_months:.2f} months
Annual fixed charges: {_fmt_b(snapshot.annual_dividends_b)}

SHARES
As of: {_fmt_as_of(shares.get("shares_as_of"))}
Basic shares: {shares["basic_shares_m"]:,.3f}M
Diluted shares: {shares["diluted_shares_m"]:,.3f}M

PURCHASES
As of: {_fmt_as_of(latest_purchase.as_of_date)}
Average cost: {_fmt_usd(latest_purchase.average_btc_cost)}
Holdings: {_fmt_optional(latest_purchase.btc_holdings, "", 0)}
Diluted shares: {_fmt_optional(latest_purchase.diluted_shares_m, "M", 3)}
BTC Yield QTD: {_fmt_optional(latest_purchase.btc_yield_qtd_pct, "%", 1)}
BTC Yield YTD: {latest_purchase.btc_yield_ytd_pct:.1f}%

RECONCILIATION
Calculated BTC NAV vs reported: {_audit_pair(metrics.btc_nav_b, snapshot.reported_metrics.get("btc_nav_b"), "B")}
Calculated market cap vs reported: {_audit_pair(metrics.basic_market_cap_b, snapshot.market_cap_b, "B")}
Calculated BTC/share vs reported: {_audit_pair(metrics.btc_per_share, snapshot.reported_metrics.get("btc_per_share"))}
Calculated reserve: {_fmt_b(metrics.usd_reserve_b)}
Calculated net leverage vs reported: {_audit_pair(metrics.net_leverage, snapshot.reported_metrics.get("net_leverage"))}
Calculated amplification vs reported: {_audit_pair(metrics.amplification, snapshot.reported_metrics.get("amplification"))}
Calculated coverage vs reported: {_audit_pair(metrics.btc_div_coverage_calculated, snapshot.btc_div_coverage_years)}
Coverage-implied charges vs direct annual charges: {_audit_pair(metrics.coverage_implied_fixed_charges_b, metrics.annual_fixed_charges_b, "B")}
Latest purchase holdings vs dashboard holdings: {_fmt_optional(purchase_holdings_delta, " BTC", 0)}
Latest purchase diluted shares vs shares page: {_fmt_optional(purchase_diluted_delta, "M", 3)}
Tracked convertible debt vs aggregate debt: tracked {_fmt_b(tracked_convertible_notional_b)} | aggregate {_fmt_b(snapshot.debt_b)} | untracked {_fmt_b(untracked_debt_b)}
Raw MSTR timeStampUtc: {mstr.get("timeStampUtc")}
Raw BTC timestamp: {dashboard.get("btc_timestamp")}
""".strip()


def format_telegram_report(run: EngineRun, now: datetime | None = None) -> str:
    snapshot, metrics, zones, buckets = run.snapshot, run.metrics, run.zones, maturity_buckets(run.snapshot)
    source = snapshot.source_metadata
    timestamp = (now or datetime.now(timezone(timedelta(hours=7)))).astimezone(timezone(timedelta(hours=7))).strftime("%d %b %Y | %H:%M WIB")
    levels = adaptive_btc_scenario_levels(snapshot.btc_price)
    scenarios = {
        btc_price: calculate_adaptive_btc_scenario(snapshot, metrics, btc_price)[1].fair_price
        for btc_price in levels
    }
    findings = [
        f"MSTR trades at {_fmt_usd(snapshot.mstr_price)} versus fair price {_fmt_usd(zones.fair_price)}.",
        f"Current EV/NAV is {metrics.current_ev_nav:.2f}x versus dynamic fair EV/NAV {zones.fair_ev_nav:.2f}x.",
        f"Risk score is {_fmt_pct(zones.risk_score)} with data quality {_fmt_pct(metrics.data_quality)}.",
        (
            "Strong Buy is blocked by hard gates; Accumulate is the maximum low-price action."
            if run.gates.strong_buy_blocked
            else "Distress gates override normal valuation zones." if run.gates.distress else "No hard gate blocks the normal valuation ladder."
        ),
    ]
    return f"""
🏦 NEVETS HOLDING | MSTR DECISION ENGINE
📅 {timestamp}
Asset: MSTR | Benchmark: BTC
━━━━━━━━━━━━━━━━━

📈 MARKET SNAPSHOT
BTC Spot: {_fmt_usd(snapshot.btc_price)}
MSTR Last: {_fmt_usd(snapshot.mstr_price)}
Basic MC: {_fmt_b(metrics.basic_market_cap_b)}
Diluted MC: {_fmt_b(metrics.diluted_market_cap_b)}
EV: {_fmt_b(snapshot.enterprise_value_b)}

₿ TREASURY POSITION
BTC Holdings: {snapshot.btc_holdings:,.0f} BTC
Avg Cost Basis: {_fmt_usd(snapshot.average_btc_cost)}
BTC NAV: {_fmt_b(metrics.btc_nav_b)}
Unrealized P/L: {_fmt_b(metrics.unrealized_pl_b)}
USD Reserve: {_fmt_b(metrics.usd_reserve_b)}
Cost Premium: {_fmt_pct(metrics.cost_premium)}
Drawdown: {_fmt_pct(metrics.drawdown)}

💎 SHAREHOLDER VALUE
BTC/Share: {metrics.btc_per_share:.8f}
NAV/Basic Share: {_fmt_usd(metrics.nav_per_basic_share)}
NAV/Diluted Share: {_fmt_usd(metrics.nav_per_diluted_share)}
BTC Yield YTD: {snapshot.btc_yield_ytd_pct:.1f}%

⚖️ VALUATION
mNAV Basic: {metrics.mnav_basic:.2f}x
mNAV Diluted: {metrics.mnav_diluted:.2f}x
Current EV/NAV: {metrics.current_ev_nav:.2f}x
Dynamic Fair EV/NAV: {zones.fair_ev_nav:.2f}x
Fair Price: {_fmt_usd(zones.fair_price)}

🛡 RISK & COVERAGE
Debt: {_fmt_b(snapshot.debt_b)}
Preferred: {_fmt_b(snapshot.preferred_b)}
Net Leverage: {_fmt_pct(metrics.net_leverage)}
Amplification: {_fmt_pct(metrics.amplification)}
BTC Div Cov: {snapshot.btc_div_coverage_years:.2f} years
USD Div Cov: {snapshot.usd_div_coverage_months:.2f} months
Risk Score: {_fmt_pct(zones.risk_score)}
Data Quality: {_fmt_pct(metrics.data_quality)}

⏳ CONVERTIBLE DEBT MATURITY
<12M: {_fmt_b(buckets["<12M"])}
2028: {_fmt_b(buckets["2028"])} | 2029: {_fmt_b(buckets["2029"])}
2030: {_fmt_b(buckets["2030"])} | 2031+: {_fmt_b(buckets["2031+"])}

🏛 CAPITAL STRUCTURE
Basic Shares: {snapshot.basic_shares_m:,.2f}M
Diluted Shares: {snapshot.diluted_shares_m:,.2f}M
Dilution Gap: {snapshot.diluted_shares_m - snapshot.basic_shares_m:,.2f}M
Dilution: {_fmt_pct(metrics.dilution)}
Annual Fixed Charges: {_fmt_b(metrics.annual_fixed_charges_b)}

📊 BTC SCENARIOS
{chr(10).join(f"BTC ${int(btc_price):,} → Estimated Fair MSTR: {_fmt_usd(fair_price)}" for btc_price, fair_price in scenarios.items())}

🎯 ADAPTIVE PRICE ZONES
Strong Buy: ≤ {_fmt_usd(zones.strong_buy_price)}
Accumulate: {_fmt_usd(zones.strong_buy_price)} – {_fmt_usd(zones.accumulate_price)}
Hold: {_fmt_usd(zones.accumulate_price)} – {_fmt_usd(zones.hold_price)}
Reduce: {_fmt_usd(zones.hold_price)} – {_fmt_usd(zones.reduce_price)}
Sell: > {_fmt_usd(zones.reduce_price)}

📝 EXECUTIVE SUMMARY
- {findings[0]}
- {findings[1]}
- {findings[2]}
- {findings[3]}
Final action: {run.decision.action}

Internal use only
""".strip()


def send_telegram_message(message: str) -> bool:
    if not (bot_token := os.getenv("TELEGRAM_BOT_TOKEN")) or not (chat_id := os.getenv("TELEGRAM_CHAT_ID")):
        return False
    try:
        import requests  # type: ignore

        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True}, timeout=15
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"Telegram send failed: {exc}", file=sys.stderr)
        return False


def fetch_v2_portfolio_snapshot() -> dict[str, Any] | None:
    """3-layer fallback for authoritative portfolio snapshot: Cloudflare V2 -> Local Mirror -> Verified Baseline."""
    # Layer 1: Live Cloudflare V2 Worker API
    try:
        import requests  # type: ignore

        response = requests.get(V2_SNAPSHOT_URL, timeout=10)
        if response.ok:
            payload = response.json()
            if isinstance(payload, dict):
                overview = payload.get("documents", {}).get("overview")
                if isinstance(overview, dict) and "portfolio" in overview:
                    return overview
    except Exception as exc:
        print(f"::notice::Remote V2 snapshot fetch failed ({exc}), trying Layer 2 local mirror...", file=sys.stderr)

    # Layer 2: Local public audit mirror
    if LOCAL_OVERVIEW_PATH.exists():
        try:
            return json.loads(LOCAL_OVERVIEW_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"::warning::Layer 2 local mirror read failed: {exc}, using Layer 3 baseline...", file=sys.stderr)

    # Layer 3: Verified baseline fallback
    return {
        "portfolio": {
            "cash_usd": 588.77,
            "cash_idr": 0.0,
            "mstr_quantity": 2.1,
            "mstr_average_cost": 81.99,
            "mstr_cost_basis": 172.18,
            "net_contributions_usd": 760.96,
        },
        "market": {"usd_idr": 17600.0},
    }


def format_portfolio_recommendation(
    run: EngineRun, overview: Mapping[str, Any], now: datetime | None = None
) -> str:
    portfolio = overview.get("portfolio", {})
    market = overview.get("market", {})

    cash_usd = float(portfolio.get("cash_usd", 0) or 0)
    cash_idr = float(portfolio.get("cash_idr", 0) or 0)
    mstr_qty = float(portfolio.get("mstr_quantity", 0) or 0)
    mstr_avg_cost = float(portfolio.get("mstr_average_cost", 0) or 0)
    mstr_cost_basis = float(portfolio.get("mstr_cost_basis", 0) or 0)
    net_contributions = float(portfolio.get("net_contributions_usd", 0) or 0)

    # Use live MSTR price from current EngineRun snapshot
    mstr_price = run.snapshot.mstr_price
    mstr_market_val = mstr_qty * mstr_price
    unrealized_pl = mstr_market_val - mstr_cost_basis
    unrealized_pct = (unrealized_pl / mstr_cost_basis * 100) if mstr_cost_basis > 0 else 0.0

    total_usd = cash_usd + mstr_market_val
    usd_idr = float(market.get("usd_idr", 0) or 17600)
    total_idr = total_usd * usd_idr

    cash_alloc = (cash_usd / total_usd * 100) if total_usd > 0 else 0.0
    mstr_alloc = (mstr_market_val / total_usd * 100) if total_usd > 0 else 0.0

    total_return_pct = (
        ((total_usd - net_contributions) / net_contributions * 100)
        if net_contributions > 0
        else 0.0
    )

    action = run.decision.action
    zones = run.zones
    fair_price = zones.fair_price
    strong_buy_price = zones.strong_buy_price
    accumulate_price = zones.accumulate_price
    hold_price = zones.hold_price
    reduce_price = zones.reduce_price

    pl_sign = "+" if unrealized_pl >= 0 else ""
    ret_sign = "+" if total_return_pct >= 0 else ""
    timestamp = (now or datetime.now(timezone(timedelta(hours=7)))).astimezone(
        timezone(timedelta(hours=7))
    ).strftime("%d %b %Y | %H:%M WIB")

    if action == "STRONG BUY":
        val_analysis = (
            f"Harga MSTR ({_fmt_usd(mstr_price)}) terdiskon sangat dalam (≤ {_fmt_usd(strong_buy_price)}), "
            f"jauh di bawah Fair Price ({_fmt_usd(fair_price)})."
        )
        port_analysis = (
            f"Saldo kas USD Anda: {_fmt_usd(cash_usd)} ({cash_alloc:.1f}% portofolio). "
            f"Posisi MSTR saat ini: {mstr_qty:g} saham."
        )
        if cash_usd >= mstr_price:
            max_shares = int(cash_usd // mstr_price)
            suggested = max(1, min(max_shares, max(1, int(cash_usd * 0.4 // mstr_price))))
            advice_lines = [
                "1. Peluang Emas:\nSangat disarankan akumulasi agresif mumpung harga di level diskon ekstrem.",
                f"2. Alokasi Kas:\nAlokasikan 25% – 50% kas USD ({_fmt_usd(cash_usd)}). Disarankan beli {suggested} saham.",
                "3. Cadangan Amunisi:\nSisakan kas cadangan untuk mengantisipasi volatilitas lanjutan.",
            ]
            shortcuts = [
                f"👉 Beli Diskon:\n/buy_mstr {suggested} {mstr_price:,.2f}",
                "👉 Cek Portofolio:\n/portofolio",
            ]
        else:
            advice_lines = [
                f"1. Valuasi Murah:\nHarga sangat menarik, namun saldo kas USD ({_fmt_usd(cash_usd)}) belum cukup untuk 1 saham penuh.",
                "2. Tambah Amunisi:\nPertimbangkan setoran kas baru untuk memanfaatkan momentum diskon ini.",
            ]
            shortcuts = [
                "👉 Setor Kas:\n/deposit USD 100",
                "👉 Cek Saldo:\n/cash",
            ]

    elif action == "ACCUMULATE":
        val_analysis = (
            f"Harga MSTR ({_fmt_usd(mstr_price)}) berada di zona diskon akumulasi "
            f"(${_fmt_usd(strong_buy_price)} – {_fmt_usd(accumulate_price)}), di bawah Fair Price ({_fmt_usd(fair_price)})."
        )
        port_analysis = (
            f"Saldo kas USD tersedia: {_fmt_usd(cash_usd)} ({cash_alloc:.1f}%). "
            f"Posisi MSTR saat ini: {mstr_qty:g} saham."
        )
        if cash_usd >= mstr_price:
            advice_lines = [
                "1. Momentum DCA:\nSaat yang tepat untuk cicil beli bertahap (Dollar Cost Averaging).",
                f"2. Beli Bertahap:\nDisarankan beli 1 saham di harga ini ({_fmt_usd(mstr_price)}) tanpa menghabiskan seluruh kas cadangan.",
                "3. Simpan Sisa Kas:\nSimpan sisa kas untuk kesempatan akumulasi berikutnya jika ada koreksi lebih dalam.",
            ]
            shortcuts = [
                f"👉 Beli Bertahap:\n/buy_mstr 1 {mstr_price:,.2f}",
                "👉 Cek Riwayat:\n/history 10",
            ]
        else:
            advice_lines = [
                f"1. Zona Akumulasi:\nHarga menarik untuk DCA, namun kas USD ({_fmt_usd(cash_usd)}) terbatas.",
                "2. Tambah Amunisi:\nPertimbangkan deposit kas baru untuk menambah amunisi beli.",
            ]
            shortcuts = [
                "👉 Setor Kas:\n/deposit USD 50",
                "👉 Cek Saldo:\n/cash",
            ]

    elif action == "HOLD":
        val_analysis = (
            f"Harga MSTR ({_fmt_usd(mstr_price)}) berada di zona wajar / fair value "
            f"({_fmt_usd(accumulate_price)} – {_fmt_usd(hold_price)})."
        )
        port_analysis = (
            f"Posisi MSTR {mstr_qty:g} saham mencatat P&L {pl_sign}{unrealized_pct:.2f}% ({pl_sign}{_fmt_usd(unrealized_pl)}). "
            f"Cadangan kas USD: {_fmt_usd(cash_usd)} ({cash_alloc:.1f}%)."
        )
        advice_lines = [
            "1. Wait & See:\nPertahankan posisi yang ada, tidak perlu aksi tergesa-gesa.",
            f"2. Disiplin Valuasi:\nJangan FOMO beli baru di atas zona akumulasi (> {_fmt_usd(accumulate_price)}).",
            f"3. Belum Saatnya TP:\nBelum perlu take profit sebelum harga memasuki zona reduce (≥ {_fmt_usd(hold_price)}).",
            f"4. Jaga Kas:\nBiarkan kas USD ({_fmt_usd(cash_usd)}) tetap siap sebagai amunisi.",
        ]
        shortcuts = [
            "👉 Status Challenge:\n/challenge_status",
            "👉 Cek Portofolio:\n/portofolio",
        ]

    elif action == "REDUCE":
        val_analysis = (
            f"Harga MSTR ({_fmt_usd(mstr_price)}) berada di atas Fair Price ({_fmt_usd(fair_price)}) "
            f"dan memasuki zona REDUCE ({_fmt_usd(hold_price)} – {_fmt_usd(reduce_price)})."
        )
        port_analysis = (
            f"Posisi MSTR Anda {mstr_qty:g} saham sudah mencetak laba {pl_sign}{unrealized_pct:.2f}% ({pl_sign}{_fmt_usd(unrealized_pl)}). "
            f"Porsi MSTR saat ini {mstr_alloc:.1f}%, kas USD {_fmt_usd(cash_usd)} ({cash_alloc:.1f}%)."
        )
        if mstr_qty >= 1.0:
            advice_lines = [
                "1. Opsi Profit Taking:\nDisarankan merealisasikan laba bertahap (jual 0.5 – 1.0 saham) untuk mengamankan cuan ke kas USD.",
                f"2. Opsi Long-Term:\nBoleh tetap hold jika fokus horizon jangka panjang, karena porsi kas Anda ({cash_alloc:.1f}%) masih cukup tebal.",
                f"3. Pembelian Baru:\nDilarang menambah beli di level ini. Tunggu harga kembali ke zona Accumulate (≤ {_fmt_usd(accumulate_price)}).",
            ]
            shortcuts = [
                f"👉 Jual 1 Saham:\n/sell_mstr 1 {mstr_price:,.2f}",
                f"👉 Jual 0.5 Saham:\n/sell_mstr 0.5 {mstr_price:,.2f}",
                "👉 Cek Saldo Kas:\n/cash",
            ]
        elif mstr_qty > 0:
            advice_lines = [
                f"1. Kunci Keuntungan:\nPosisi MSTR Anda ({mstr_qty:g} saham) sudah profit {pl_sign}{unrealized_pct:.2f}%.",
                "2. Opsi Fleksibel:\nAnda bisa kunci sebagian/seluruh laba atau tetap hold karena ukuran posisi relatif kecil.",
                "3. Jangan FOMO:\nHindari membeli lagi di atas harga wajar.",
            ]
            shortcuts = [
                f"👉 Jual Posisi:\n/sell_mstr {mstr_qty:g} {mstr_price:,.2f}",
                "👉 Cek Portofolio:\n/portofolio",
            ]
        else:
            advice_lines = [
                "1. Belum Ada Posisi:\nAnda belum memiliki posisi saham MSTR.",
                "2. Hindari Masuk:\nHindari masuk di harga saat ini karena risiko valuasi sedang tinggi.",
                f"3. Tunggu Diskon:\nTunggu momentum diskon di zona Accumulate (≤ {_fmt_usd(accumulate_price)}).",
            ]
            shortcuts = [
                "👉 Cek Saldo Kas:\n/cash",
                "👉 Status Challenge:\n/challenge_status",
            ]

    else:  # SELL
        val_analysis = (
            f"Harga MSTR ({_fmt_usd(mstr_price)}) sudah overvalued ekstrem (> {_fmt_usd(reduce_price)}), "
            f"jauh melampaui valuasi wajar aset dasarnya."
        )
        port_analysis = (
            f"Posisi MSTR {mstr_qty:g} saham mencatat P&L {pl_sign}{unrealized_pct:.2f}%. "
            f"Nilai pasar saham: {_fmt_usd(mstr_market_val)}."
        )
        if mstr_qty > 0:
            sell_qty = max(1.0, round(mstr_qty * 0.7, 1))
            advice_lines = [
                "1. De-risking Prioritas:\nSangat disarankan menjual sebagian besar atau seluruh posisi MSTR.",
                "2. Amankan Cuan:\nAmankan profit maksimal dan pindahkan aset ke kas USD yang aman.",
                "3. Disiplin Risiko:\nJangan tergiur FOMO; risiko koreksi tajam sangat tinggi.",
            ]
            shortcuts = [
                f"👉 Jual Sebagian:\n/sell_mstr {sell_qty:g} {mstr_price:,.2f}",
                f"👉 Jual Semua:\n/sell_mstr {mstr_qty:g} {mstr_price:,.2f}",
            ]
        else:
            advice_lines = [
                "1. Sikap Defensif:\nTetap di kas USD. Pasar sedang di puncak euforia.",
                "2. Sabar Menunggu:\nTunggu koreksi sehat sebelum mempertimbangkan posisi baru.",
            ]
            shortcuts = [
                "👉 Cek Saldo Kas:\n/cash",
            ]

    if run.gates.strong_buy_blocked:
        advice_lines.append("⚠️ Catatan Risiko:\nStrong Buy dibatasi oleh gate fundamental; pertahankan kehati-hatian.")
    elif run.gates.distress:
        advice_lines.append("🚨 Catatan Risiko:\nDistress gate aktif; risiko struktural tinggi terdeteksi.")

    advice_block = "\n\n".join(advice_lines)
    shortcut_block = "\n\n".join(shortcuts)

    return f"""
💼 PORTOFOLIO & REKOMENDASI
📅 {timestamp}
Asset: MSTR | Challenge V2
━━━━━━━━━━━━━━━━━

📊 POSISI PORTOFOLIO SAAT INI
💵 Kas USD: {_fmt_usd(cash_usd)}
🇮🇩 Kas IDR: Rp {cash_idr:,.0f}
📈 Posisi MSTR: {mstr_qty:g} saham
🏷 Avg Beli: {_fmt_usd(mstr_avg_cost)} / saham
💰 Nilai Pasar: {_fmt_usd(mstr_market_val)}
📊 Floating P/L: {pl_sign}{_fmt_usd(unrealized_pl)} ({pl_sign}{unrealized_pct:.2f}%)
💼 Total Nilai: {_fmt_usd(total_usd)}
   (~Rp {total_idr:,.0f})
📊 Total Return: {ret_sign}{total_return_pct:.2f}%
   (Modal Bersih: {_fmt_usd(net_contributions)})
⚖️ Alokasi: {cash_alloc:.1f}% Kas | {mstr_alloc:.1f}% MSTR

━━━━━━━━━━━━━━━━━
🎯 REKOMENDASI TINDAKAN
Status Valuasi: 🏷 {action}
Harga MSTR: {_fmt_usd(mstr_price)}
Fair Price: {_fmt_usd(fair_price)}

• Analisis Valuasi:
{val_analysis}

• Kondisi Portofolio Anda:
{port_analysis}

• Saran Aksi Hari Ini:
{advice_block}

━━━━━━━━━━━━━━━━━
⚡ REKOMENDASI PERINTAH BOT
{shortcut_block}
""".strip()


def golden_snapshot(mstr_price: float = 112.53) -> StrategySnapshot:
    basic_shares_m, debt_b, preferred_b, usd_reserve_b = 356.320, 6.754, 15.475, 1.101
    market_cap_b = 40.097
    enterprise_value_b = 61.225
    instruments = (
        DebtInstrument(1.01, date(2027, 9, 16), date(2028, 9, 16), date(2027, 9, 16)),
        DebtInstrument(1.50, date(2028, 6, 2), date(2029, 12, 2), date(2028, 6, 2)),
        DebtInstrument(2.00, date(2028, 3, 2), date(2030, 3, 2), date(2028, 3, 2)),
        DebtInstrument(0.80, date(2028, 9, 16), date(2030, 3, 16), date(2028, 9, 16)),
        DebtInstrument(0.60375, date(2028, 9, 16), date(2031, 3, 16), date(2028, 9, 16)),
        DebtInstrument(0.80, date(2029, 6, 16), date(2032, 6, 16), date(2029, 6, 16)),
    )
    return StrategySnapshot(
        date(2026, 6, 21),
        64122.76,
        mstr_price,
        846842,
        75656,
        basic_shares_m,
        386.052,
        12.5,
        market_cap_b,
        enterprise_value_b,
        debt_b,
        preferred_b,
        usd_reserve_b,
        7.713,
        31.73,
        1.711372472,
        instruments,
        source_metadata=SourceMetadata(
            mstr_as_of=datetime(2026, 6, 18, 20, 0),
            btc_as_of=datetime(2026, 6, 21, 8, 33),
            shares_as_of=date(2026, 6, 14),
            purchase_as_of=date(2026, 6, 15),
        ),
    )


def _apply_market_price(snapshot: StrategySnapshot, mstr_price: float) -> StrategySnapshot:
    market_cap_b = mstr_price * snapshot.basic_shares_m / 1000
    return replace(
        snapshot,
        mstr_price=mstr_price,
        market_cap_b=market_cap_b,
        enterprise_value_b=market_cap_b + snapshot.debt_b + snapshot.preferred_b - snapshot.usd_reserve_b,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MSTR Decision Engine v2")
    parser.add_argument("--dry-run", action="store_true", help="Render report without sending Telegram.")
    parser.add_argument("--sample", action="store_true", help="Use the deterministic golden sample snapshot.")
    parser.add_argument("--audit-live", action="store_true", help="Print live source values and reconciliation without changing state.")
    parser.add_argument("--no-portfolio", action="store_true", help="Skip the personalized portfolio recommendation message.")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    return parser.parse_args(argv)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    try:
        if args.audit_live:
            print(format_source_audit())
            return 0
        run = evaluate_snapshot(
            golden_snapshot() if args.sample else fetch_strategy_snapshot(state_path=args.state_file),
            state_path=args.state_file,
        )
        # Message 1: The standard detailed snapshot report (completely untouched)
        report = format_telegram_report(run)
        print(report)

        # Message 2: Personalized Portfolio Update & Recommendation
        portfolio_report = None
        if not args.no_portfolio:
            overview = fetch_v2_portfolio_snapshot()
            if overview:
                portfolio_report = format_portfolio_recommendation(run, overview)
                print("\n" + "=" * 40 + "\n")
                print(portfolio_report)

        if args.dry_run:
            return 0
        if not send_telegram_message(report):
            print("Telegram credentials unavailable; report rendered but not sent.", file=sys.stderr)
        elif portfolio_report:
            import time

            time.sleep(1)
            send_telegram_message(portfolio_report)
        return 0
    except Exception as exc:
        message = f"MSTR Decision Engine error: {exc}"
        print(message, file=sys.stderr)
        if not args.dry_run:
            send_telegram_message(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
