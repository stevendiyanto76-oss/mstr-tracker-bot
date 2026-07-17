from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


getcontext().prec = 40

EVENT_SCHEMA_VERSION = 2
CONFIG_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1
PUBLIC_SCHEMA_VERSION = 1

DATA_DIR = Path("data")
CONFIG_FILE = DATA_DIR / "challenge_config.json"
EVENT_FILE = DATA_DIR / "challenge_events.jsonl"
SNAPSHOT_FILE = DATA_DIR / "challenge_snapshots.jsonl"
DISCLOSURE_HISTORY_FILE = DATA_DIR / "mstr_disclosure_history.jsonl"
PUBLIC_DIR = DATA_DIR / "public"
NEW_YORK = ZoneInfo("America/New_York")

PUBLIC_FILES = {
    "overview": PUBLIC_DIR / "challenge_overview.json",
    "transactions": PUBLIC_DIR / "challenge_transactions.json",
    "performance": PUBLIC_DIR / "challenge_performance.json",
    "thesis": PUBLIC_DIR / "challenge_thesis.json",
    "audit": PUBLIC_DIR / "challenge_audit.json",
    "health": PUBLIC_DIR / "challenge_health.json",
}

DEFAULT_CHALLENGE_ID = "mstr-live-thesis-v1"
DEFAULT_CHALLENGE_NAME = "MSTR Live Thesis Challenge"
VALID_STATUSES = {"prelaunch", "active", "paused", "completed"}
VALID_CURRENCIES = {"USD", "IDR"}
EVENT_TYPES = {
    "CHALLENGE_INIT",
    "DEPOSIT",
    "WITHDRAWAL",
    "FX_CONVERSION",
    "BUY",
    "SELL",
    "FEE",
    "TAX",
    "RESET",
    "UNDO",
}
MUTATING_EVENT_TYPES = EVENT_TYPES - {"UNDO"}
EVENT_ID_RE = re.compile(r"^CH-([0-9]{6})$")
DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
UTC = timezone.utc
WIB = ZoneInfo("Asia/Jakarta")
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
USD_QUANTUM = Decimal("0.00000001")
IDR_QUANTUM = Decimal("0.01")
MSTR_QUANTUM = Decimal("0.000001")

DISCLOSURE_FINGERPRINT_FIELDS = (
    "btc_holdings",
    "basic_shares_m",
    "diluted_shares_m",
    "usd_reserve_b",
    "usd_div_coverage_months",
    "debt_b",
    "preferred_b",
)

FORBIDDEN_PUBLIC_KEYS = {
    "chat_id",
    "telegram_update_id",
    "telegram_message_id",
    "bot_token",
    "token",
    "secret",
    "email",
    "private_source",
    "broker",
    "local_path",
}
FORBIDDEN_PUBLIC_TEXT = (
    "chat_id",
    "telegram_update_id",
    "telegram_message_id",
    "bot_token",
    "private_source",
)

THESIS_DOCUMENT = {
    "title": "MSTR 2026-2030: Beyond the Bitcoin Proxy",
    "subtitle": "An Independent Analysis of Strategy Inc.'s Capital Structure and Common Equity",
    "author": "Steven Diyanto",
    "version": "1.0",
    "valuation_date": "2026-07-09",
    "information_current_through": "2026-07-11",
    "first_published": "2026-07-13",
    "doi": "10.5281/zenodo.21331182",
    "doi_url": "https://doi.org/10.5281/zenodo.21331182",
    "language": "English",
}

THESIS_RULES = {
    "tracking": {
        "allocation": "0%-1%",
        "requirements": ["monitoring evidence only", "no leverage"],
        "source_pages": [5, 87, 88],
    },
    "starter": {
        "allocation": "1%-2%",
        "requirements": [
            "mstr_price_lte_85",
            "reserve_coverage_gte_15_months",
            "btc_per_adso_stabilizing",
            "no_red_dashboard_indicator",
        ],
        "source_pages": [5, 10, 87, 88],
    },
    "strategic": {
        "allocation": "2%-3%",
        "requirements": [
            "supportive_valuation",
            "three_non_deteriorating_bps_disclosures",
            "reserve_coverage_gt_18_months",
            "preferred_yields_normalizing",
            "no_invalidation_trigger",
        ],
        "source_pages": [5, 10, 87, 88],
    },
    "stronger_value": {
        "allocation": "within 3% hard cap",
        "requirements": ["mstr_price_lte_75", "no_major_red_indicator"],
        "source_pages": [5, 87, 88],
    },
    "invalidation": {
        "requirements": [
            "reserve_coverage_lt_12_months",
            "repeated_obligation_funded_btc_sales",
            "structural_residual_value_per_adso_damage",
            "repeated_btc_per_adso_deterioration",
            "destructive_refinancing_or_dilution",
        ],
        "response": "immediate review, reduction, or exit",
        "source_pages": [10, 87, 88, 91],
    },
    "hard_cap": {"allocation": "3%", "leverage": "prohibited", "source_pages": [5, 87, 88]},
}


class ChallengeError(RuntimeError):
    """Base class for challenge failures."""


class ChallengeValidationError(ChallengeError):
    """Raised for invalid owner input or an unavailable operation."""


class ChallengeIntegrityError(ChallengeError):
    """Raised when persisted challenge data violates replay invariants."""


@dataclass
class ChallengePosition:
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO

    def copy(self) -> "ChallengePosition":
        return ChallengePosition(self.quantity, self.average_cost)


@dataclass
class ChallengeState:
    initialized: bool = False
    cash: dict[str, Decimal] = field(default_factory=lambda: {"USD": ZERO, "IDR": ZERO})
    position: ChallengePosition = field(default_factory=ChallengePosition)
    realized_pl_usd: Decimal = ZERO
    fees: dict[str, Decimal] = field(default_factory=lambda: {"USD": ZERO, "IDR": ZERO})
    taxes: dict[str, Decimal] = field(default_factory=lambda: {"USD": ZERO, "IDR": ZERO})
    net_contributions_usd: Decimal = ZERO
    contribution_data_complete: bool = True
    active_events: list[dict[str, Any]] = field(default_factory=list)
    undone_event_ids: set[str] = field(default_factory=set)

    def copy(self) -> "ChallengeState":
        return ChallengeState(
            initialized=self.initialized,
            cash=dict(self.cash),
            position=self.position.copy(),
            realized_pl_usd=self.realized_pl_usd,
            fees=dict(self.fees),
            taxes=dict(self.taxes),
            net_contributions_usd=self.net_contributions_usd,
            contribution_data_complete=self.contribution_data_complete,
            active_events=[dict(item) for item in self.active_events],
            undone_event_ids=set(self.undone_event_ids),
        )


@dataclass(frozen=True)
class MarketInputs:
    mstr_price: Decimal | None = None
    btc_price: Decimal | None = None
    usd_idr: Decimal | None = None
    mstr_as_of: str | None = None
    btc_as_of: str | None = None
    fx_as_of: str | None = None
    mstr_source: str | None = None
    btc_source: str | None = None
    fx_source: str | None = None
    fetched_at: str | None = None
    freshness: str = "unavailable"
    market_status: str = "unknown"
    warnings: tuple[str, ...] = ()


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def us_equity_market_status(current_time: datetime, price_as_of: str | None = None) -> str:
    local_time = ensure_aware_utc(current_time).astimezone(NEW_YORK)
    if local_time.weekday() >= 5:
        return "closed"
    session_open = local_time.replace(hour=9, minute=30, second=0, microsecond=0)
    session_close = local_time.replace(hour=16, minute=0, second=0, microsecond=0)
    if not session_open <= local_time < session_close:
        return "closed"
    if price_as_of:
        try:
            source_time = datetime.fromisoformat(price_as_of.replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=UTC)
            if source_time.astimezone(NEW_YORK).date() < local_time.date():
                return "closed"
        except ValueError:
            pass
    return "open"


def iso_seconds(value: datetime) -> str:
    return ensure_aware_utc(value).isoformat(timespec="seconds")


def wib_iso_seconds(value: datetime) -> str:
    return ensure_aware_utc(value).astimezone(WIB).isoformat(timespec="seconds")


def parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ChallengeIntegrityError(f"{field_name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChallengeIntegrityError(f"{field_name} must be an ISO datetime string") from exc
    if parsed.tzinfo is None:
        raise ChallengeIntegrityError(f"{field_name} must include a timezone offset")
    return parsed


def decimal_plain(value: Decimal) -> str:
    if not value.is_finite():
        raise ChallengeIntegrityError("Decimal values must be finite")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def parse_decimal(
    value: Any,
    field_name: str,
    *,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ChallengeIntegrityError(f"{field_name} must be a decimal string")
    rendered = str(value)
    if not DECIMAL_RE.fullmatch(rendered):
        raise ChallengeIntegrityError(f"{field_name} must be a decimal string")
    try:
        result = Decimal(rendered)
    except InvalidOperation as exc:
        raise ChallengeIntegrityError(f"{field_name} must be a decimal string") from exc
    if not result.is_finite():
        raise ChallengeIntegrityError(f"{field_name} must be finite")
    if not allow_negative and result < ZERO:
        raise ChallengeIntegrityError(f"{field_name} must not be negative")
    if not allow_zero and result == ZERO:
        raise ChallengeIntegrityError(f"{field_name} must be positive")
    return result


def parse_owner_decimal(value: str, field_name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = parse_decimal(value, field_name, allow_negative=False, allow_zero=allow_zero)
    except ChallengeIntegrityError as exc:
        raise ChallengeValidationError(str(exc)) from exc
    return result


def quantize_currency(value: Decimal, currency: str) -> Decimal:
    quantum = USD_QUANTUM if currency == "USD" else IDR_QUANTUM
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def data_path(base_dir: Path, relative: Path) -> Path:
    return base_dir / relative


def config_path(base_dir: Path) -> Path:
    return data_path(base_dir, CONFIG_FILE)


def event_path(base_dir: Path) -> Path:
    return data_path(base_dir, EVENT_FILE)


def snapshot_path(base_dir: Path) -> Path:
    return data_path(base_dir, SNAPSHOT_FILE)


def disclosure_history_path(base_dir: Path) -> Path:
    return data_path(base_dir, DISCLOSURE_HISTORY_FILE)


def public_path(base_dir: Path, name: str) -> Path:
    if name not in PUBLIC_FILES:
        raise ChallengeIntegrityError(f"Unknown public export: {name}")
    return data_path(base_dir, PUBLIC_FILES[name])


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = canonical_json(payload) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())


def default_config() -> dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "challenge_id": DEFAULT_CHALLENGE_ID,
        "name": DEFAULT_CHALLENGE_NAME,
        "status": "prelaunch",
        "base_currency": "USD",
        "display_currency": "IDR",
        "start_at_utc": None,
        "starting_event_id": None,
        "include_legacy_position": False,
    }


def ensure_challenge_files(base_dir: Path = Path(".")) -> None:
    config = config_path(base_dir)
    events = event_path(base_dir)
    snapshots = snapshot_path(base_dir)
    if not config.exists():
        atomic_write_json(config, default_config())
    events.parent.mkdir(parents=True, exist_ok=True)
    if not events.exists():
        atomic_write_text(events, "")
    if not snapshots.exists():
        atomic_write_text(snapshots, "")
    disclosure_history = disclosure_history_path(base_dir)
    if not disclosure_history.exists():
        atomic_write_text(disclosure_history, "")
    data_path(base_dir, PUBLIC_DIR).mkdir(parents=True, exist_ok=True)


def validate_config(config: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "challenge_id",
        "name",
        "status",
        "base_currency",
        "display_currency",
        "start_at_utc",
        "starting_event_id",
        "include_legacy_position",
    }
    if set(config) != required:
        raise ChallengeIntegrityError("challenge_config.json has unexpected or missing fields")
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ChallengeIntegrityError("Unsupported challenge config schema version")
    if not isinstance(config.get("challenge_id"), str) or not config["challenge_id"]:
        raise ChallengeIntegrityError("challenge_id must be a non-empty string")
    if not isinstance(config.get("name"), str) or not config["name"]:
        raise ChallengeIntegrityError("challenge name must be a non-empty string")
    if config.get("status") not in VALID_STATUSES:
        raise ChallengeIntegrityError("challenge status is invalid")
    if config.get("base_currency") != "USD" or config.get("display_currency") != "IDR":
        raise ChallengeIntegrityError("challenge currencies must remain USD/IDR")
    start = config.get("start_at_utc")
    event_id = config.get("starting_event_id")
    if (start is None) != (event_id is None):
        raise ChallengeIntegrityError("start_at_utc and starting_event_id must be set together")
    if start is not None:
        parse_datetime(start, "start_at_utc")
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            raise ChallengeIntegrityError("starting_event_id is invalid")
    if not isinstance(config.get("include_legacy_position"), bool):
        raise ChallengeIntegrityError("include_legacy_position must be boolean")


def load_config(base_dir: Path = Path(".")) -> dict[str, Any]:
    ensure_challenge_files(base_dir)
    try:
        value = json.loads(config_path(base_dir).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ChallengeIntegrityError("challenge_config.json is unreadable") from exc
    if not isinstance(value, dict):
        raise ChallengeIntegrityError("challenge_config.json must contain an object")
    validate_config(value)
    return value


def save_config(base_dir: Path, config: Mapping[str, Any]) -> None:
    validate_config(config)
    atomic_write_json(config_path(base_dir), config)


def private_source(
    *,
    telegram_update_id: int | None = None,
    telegram_message_id: int | None = None,
    chat_id: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    return {
        "source": source,
        "telegram_update_id": telegram_update_id,
        "telegram_message_id": telegram_message_id,
        "chat_id": str(chat_id) if chat_id is not None else None,
    }


def empty_event_fields() -> dict[str, Any]:
    return {
        "currency": None,
        "amount": None,
        "base_amount_usd": None,
        "asset": None,
        "quantity": None,
        "price_usd": None,
        "from_currency": None,
        "to_currency": None,
        "from_amount": None,
        "to_amount": None,
        "actual_fx_rate": None,
        "target_event_id": None,
        "related_event_id": None,
        "opening_mstr_quantity": None,
        "opening_mstr_average_cost": None,
        "thesis_zone": None,
        "reference_prices": None,
        "valuation_fx_rate": None,
        "valuation_fx_source": None,
    }


def next_event_id(events: Sequence[Mapping[str, Any]]) -> str:
    maximum = 0
    for event in events:
        match = EVENT_ID_RE.fullmatch(str(event.get("event_id", "")))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return f"CH-{maximum + 1:06d}"


def make_event(
    events: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    event_type: str,
    *,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ChallengeValidationError(f"Unsupported challenge event type: {event_type}")
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": next_event_id(events),
        "event_type": event_type,
        "challenge_id": config["challenge_id"],
        "timestamp_utc": iso_seconds(timestamp_utc),
        "timestamp_wib": wib_iso_seconds(timestamp_utc),
        **empty_event_fields(),
        "private_source": dict(source),
    }
    for key, value in (values or {}).items():
        if key not in event or key == "private_source":
            raise ChallengeValidationError(f"Unsupported challenge event field: {key}")
        event[key] = decimal_plain(value) if isinstance(value, Decimal) else value
    validate_event(event, config)
    return event


def _require_none(event: Mapping[str, Any], fields: Sequence[str], event_type: str) -> None:
    for field_name in fields:
        if event.get(field_name) is not None:
            raise ChallengeIntegrityError(f"{event_type} does not allow {field_name}")


def validate_private_source(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "source",
        "telegram_update_id",
        "telegram_message_id",
        "chat_id",
    }:
        raise ChallengeIntegrityError("private_source has an invalid shape")
    if not isinstance(value.get("source"), str) or not value["source"]:
        raise ChallengeIntegrityError("private_source.source must be a string")
    for key in ("telegram_update_id", "telegram_message_id"):
        item = value.get(key)
        if item is not None and (not isinstance(item, int) or item < 0):
            raise ChallengeIntegrityError(f"private_source.{key} must be a non-negative integer or null")
    chat_id = value.get("chat_id")
    if chat_id is not None and (not isinstance(chat_id, str) or not chat_id):
        raise ChallengeIntegrityError("private_source.chat_id must be a string or null")


def validate_reference_prices(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) - {"MSTR", "BTC", "USD_IDR", "as_of", "source"}:
        raise ChallengeIntegrityError("reference_prices has an invalid shape")
    for key in ("MSTR", "BTC", "USD_IDR"):
        if value.get(key) is not None:
            parse_decimal(value[key], f"reference_prices.{key}", allow_zero=False)
    for key in ("as_of", "source"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ChallengeIntegrityError(f"reference_prices.{key} must be a string or null")


def validate_event(event: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "event_id",
        "event_type",
        "challenge_id",
        "timestamp_utc",
        "timestamp_wib",
        *empty_event_fields().keys(),
        "private_source",
    }
    if set(event) != expected_fields:
        raise ChallengeIntegrityError("Challenge event has unexpected or missing fields")
    if event.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise ChallengeIntegrityError("Unsupported challenge event schema version")
    if not isinstance(event.get("event_id"), str) or not EVENT_ID_RE.fullmatch(event["event_id"]):
        raise ChallengeIntegrityError("Challenge event_id is invalid")
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        raise ChallengeIntegrityError("Challenge event_type is invalid")
    if event.get("challenge_id") != config.get("challenge_id"):
        raise ChallengeIntegrityError("Challenge event belongs to a different challenge")
    timestamp_utc = parse_datetime(event.get("timestamp_utc"), "timestamp_utc")
    timestamp_wib = parse_datetime(event.get("timestamp_wib"), "timestamp_wib")
    if timestamp_wib.utcoffset() != timedelta(hours=7):
        raise ChallengeIntegrityError("timestamp_wib must use +07:00")
    if timestamp_utc.astimezone(UTC) != timestamp_wib.astimezone(UTC):
        raise ChallengeIntegrityError("Challenge timestamps must represent the same instant")
    validate_private_source(event.get("private_source"))
    validate_reference_prices(event.get("reference_prices"))
    if event.get("thesis_zone") is not None and not isinstance(event["thesis_zone"], str):
        raise ChallengeIntegrityError("thesis_zone must be a string or null")

    currency = event.get("currency")
    if currency is not None and currency not in VALID_CURRENCIES:
        raise ChallengeIntegrityError("Challenge currency is invalid")
    if event.get("asset") is not None and event.get("asset") != "MSTR":
        raise ChallengeIntegrityError("The challenge only supports MSTR")

    if event_type == "CHALLENGE_INIT":
        if currency not in VALID_CURRENCIES:
            raise ChallengeIntegrityError("CHALLENGE_INIT requires a currency")
        parse_decimal(event.get("amount"), "amount", allow_zero=True)
        if event.get("base_amount_usd") is not None:
            parse_decimal(event["base_amount_usd"], "base_amount_usd", allow_zero=True)
        opening_quantity = event.get("opening_mstr_quantity")
        opening_cost = event.get("opening_mstr_average_cost")
        if (opening_quantity is None) != (opening_cost is None):
            raise ChallengeIntegrityError("Opening quantity and cost must be set together")
        if opening_quantity is not None:
            parse_decimal(opening_quantity, "opening_mstr_quantity", allow_zero=False)
            parse_decimal(opening_cost, "opening_mstr_average_cost", allow_zero=False)
        _require_none(
            event,
            ("asset", "quantity", "price_usd", "from_currency", "to_currency", "from_amount", "to_amount", "actual_fx_rate", "target_event_id"),
            event_type,
        )
    elif event_type in {"DEPOSIT", "WITHDRAWAL", "FEE", "TAX"}:
        if currency not in VALID_CURRENCIES:
            raise ChallengeIntegrityError(f"{event_type} requires a currency")
        parse_decimal(event.get("amount"), "amount", allow_zero=False)
        if event.get("base_amount_usd") is not None:
            parse_decimal(event["base_amount_usd"], "base_amount_usd", allow_zero=False)
        _require_none(
            event,
            ("asset", "quantity", "price_usd", "from_currency", "to_currency", "from_amount", "to_amount", "actual_fx_rate", "target_event_id", "opening_mstr_quantity", "opening_mstr_average_cost"),
            event_type,
        )
    elif event_type == "FX_CONVERSION":
        if event.get("from_currency") not in VALID_CURRENCIES or event.get("to_currency") not in VALID_CURRENCIES:
            raise ChallengeIntegrityError("FX_CONVERSION requires supported currencies")
        if event["from_currency"] == event["to_currency"]:
            raise ChallengeIntegrityError("FX currencies must differ")
        from_amount = parse_decimal(event.get("from_amount"), "from_amount", allow_zero=False)
        to_amount = parse_decimal(event.get("to_amount"), "to_amount", allow_zero=False)
        actual_rate = parse_decimal(event.get("actual_fx_rate"), "actual_fx_rate", allow_zero=False)
        expected_rate = to_amount / from_amount
        if abs(expected_rate - actual_rate) > Decimal("0.00000001"):
            raise ChallengeIntegrityError("actual_fx_rate does not match actual amounts")
        _require_none(
            event,
            ("currency", "amount", "asset", "quantity", "price_usd", "target_event_id", "opening_mstr_quantity", "opening_mstr_average_cost"),
            event_type,
        )
    elif event_type in {"BUY", "SELL"}:
        if currency != "USD" or event.get("asset") != "MSTR":
            raise ChallengeIntegrityError(f"{event_type} must use MSTR and USD")
        quantity = parse_decimal(event.get("quantity"), "quantity", allow_zero=False)
        price = parse_decimal(event.get("price_usd"), "price_usd", allow_zero=False)
        amount = parse_decimal(event.get("amount"), "amount", allow_zero=False)
        if quantity.quantize(MSTR_QUANTUM) != quantity:
            raise ChallengeIntegrityError("MSTR quantity exceeds six decimal places")
        if abs(amount - quantity * price) > USD_QUANTUM:
            raise ChallengeIntegrityError("Trade amount does not equal quantity times price")
        _require_none(
            event,
            ("from_currency", "to_currency", "from_amount", "to_amount", "actual_fx_rate", "target_event_id", "opening_mstr_quantity", "opening_mstr_average_cost"),
            event_type,
        )
    elif event_type == "RESET":
        _require_none(event, tuple(empty_event_fields().keys()), event_type)
    elif event_type == "UNDO":
        target = event.get("target_event_id")
        if not isinstance(target, str) or not EVENT_ID_RE.fullmatch(target):
            raise ChallengeIntegrityError("UNDO requires a valid target_event_id")
        _require_none(event, tuple(key for key in empty_event_fields() if key != "target_event_id"), event_type)


def read_events(base_dir: Path = Path(".")) -> list[dict[str, Any]]:
    ensure_challenge_files(base_dir)
    config = load_config(base_dir)
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    update_ids: dict[int, str] = {}
    previous_event_number = 0
    previous_timestamp: datetime | None = None
    for line_number, line in enumerate(event_path(base_dir).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChallengeIntegrityError(f"Malformed challenge JSONL line {line_number}") from exc
        if not isinstance(event, dict):
            raise ChallengeIntegrityError(f"Challenge line {line_number} must be an object")
        validate_event(event, config)
        if event["event_id"] in seen_ids:
            raise ChallengeIntegrityError(f"Duplicate challenge event_id: {event['event_id']}")
        event_number = int(EVENT_ID_RE.fullmatch(event["event_id"]).group(1))
        if event_number <= previous_event_number:
            raise ChallengeIntegrityError("Challenge event IDs must be strictly increasing")
        event_timestamp = parse_datetime(event["timestamp_utc"], "timestamp_utc").astimezone(UTC)
        if previous_timestamp is not None and event_timestamp < previous_timestamp:
            raise ChallengeIntegrityError("Challenge events must be chronological")
        previous_event_number = event_number
        previous_timestamp = event_timestamp
        seen_ids.add(event["event_id"])
        update_id = event["private_source"].get("telegram_update_id")
        if update_id is not None:
            if update_id in update_ids:
                raise ChallengeIntegrityError(f"Duplicate challenge Telegram update ID: {update_id}")
            update_ids[update_id] = event["event_id"]
        events.append(event)
    validate_undo_references(events)
    return events


def validate_undo_references(events: Sequence[Mapping[str, Any]]) -> None:
    positions = {str(event["event_id"]): index for index, event in enumerate(events)}
    undone: set[str] = set()
    for index, event in enumerate(events):
        if event.get("event_type") != "UNDO":
            continue
        target = str(event["target_event_id"])
        if target not in positions or positions[target] >= index:
            raise ChallengeIntegrityError(f"UNDO target does not precede event: {target}")
        target_event = events[positions[target]]
        if target_event.get("event_type") == "UNDO":
            raise ChallengeIntegrityError("UNDO of UNDO is forbidden")
        if target in undone:
            raise ChallengeIntegrityError(f"Challenge event was undone twice: {target}")
        undone.add(target)


def active_target_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(event["target_event_id"]) for event in events if event.get("event_type") == "UNDO"}


def _event_base_amount_usd(event: Mapping[str, Any]) -> Decimal | None:
    base_amount = event.get("base_amount_usd")
    if base_amount is not None:
        result: Decimal | None = parse_decimal(base_amount, "base_amount_usd")
    else:
        currency = event.get("currency")
        amount = event.get("amount")
        result = parse_decimal(amount, "amount") if currency == "USD" and amount is not None else None
    if event.get("event_type") == "CHALLENGE_INIT" and event.get("opening_mstr_quantity") is not None:
        if result is None:
            return None
        quantity = parse_decimal(event["opening_mstr_quantity"], "opening_mstr_quantity", allow_zero=False)
        average_cost = parse_decimal(event["opening_mstr_average_cost"], "opening_mstr_average_cost", allow_zero=False)
        result += quantity * average_cost
    return result


def replay_events(events: Sequence[Mapping[str, Any]]) -> ChallengeState:
    validate_undo_references(events)
    undone = active_target_ids(events)
    state = ChallengeState(undone_event_ids=set(undone))
    for raw_event in events:
        event = dict(raw_event)
        if event["event_type"] == "UNDO" or event["event_id"] in undone:
            continue
        event_type = event["event_type"]
        state.active_events.append(event)
        if event_type == "RESET":
            state = ChallengeState(active_events=list(state.active_events), undone_event_ids=set(undone))
            continue
        if event_type == "CHALLENGE_INIT":
            if state.initialized:
                raise ChallengeIntegrityError("A challenge era cannot contain two active initializations")
            state.initialized = True
            currency = str(event["currency"])
            amount = parse_decimal(event["amount"], "amount")
            state.cash[currency] += amount
            base_amount = _event_base_amount_usd(event)
            if base_amount is None:
                state.contribution_data_complete = False
            else:
                state.net_contributions_usd += base_amount
            if event.get("opening_mstr_quantity") is not None:
                state.position = ChallengePosition(
                    parse_decimal(event["opening_mstr_quantity"], "opening_mstr_quantity", allow_zero=False),
                    parse_decimal(event["opening_mstr_average_cost"], "opening_mstr_average_cost", allow_zero=False),
                )
            continue
        if not state.initialized:
            raise ChallengeIntegrityError(f"{event_type} appears before CHALLENGE_INIT")
        if event_type == "DEPOSIT":
            currency = str(event["currency"])
            amount = parse_decimal(event["amount"], "amount", allow_zero=False)
            state.cash[currency] += amount
            base_amount = _event_base_amount_usd(event)
            if base_amount is None:
                state.contribution_data_complete = False
            else:
                state.net_contributions_usd += base_amount
        elif event_type == "WITHDRAWAL":
            currency = str(event["currency"])
            amount = parse_decimal(event["amount"], "amount", allow_zero=False)
            if amount > state.cash[currency]:
                raise ChallengeIntegrityError(f"WITHDRAWAL {event['event_id']} exceeds {currency} cash")
            state.cash[currency] -= amount
            base_amount = _event_base_amount_usd(event)
            if base_amount is None:
                state.contribution_data_complete = False
            else:
                state.net_contributions_usd -= base_amount
        elif event_type == "FX_CONVERSION":
            from_currency = str(event["from_currency"])
            to_currency = str(event["to_currency"])
            from_amount = parse_decimal(event["from_amount"], "from_amount", allow_zero=False)
            to_amount = parse_decimal(event["to_amount"], "to_amount", allow_zero=False)
            if from_amount > state.cash[from_currency]:
                raise ChallengeIntegrityError(f"FX {event['event_id']} exceeds {from_currency} cash")
            state.cash[from_currency] -= from_amount
            state.cash[to_currency] += to_amount
        elif event_type == "BUY":
            quantity = parse_decimal(event["quantity"], "quantity", allow_zero=False)
            price = parse_decimal(event["price_usd"], "price_usd", allow_zero=False)
            consideration = quantity * price
            if consideration > state.cash["USD"]:
                raise ChallengeIntegrityError(f"BUY {event['event_id']} exceeds USD cash")
            state.cash["USD"] -= consideration
            new_quantity = state.position.quantity + quantity
            new_average = (
                state.position.quantity * state.position.average_cost + consideration
            ) / new_quantity
            state.position = ChallengePosition(new_quantity, new_average)
        elif event_type == "SELL":
            quantity = parse_decimal(event["quantity"], "quantity", allow_zero=False)
            price = parse_decimal(event["price_usd"], "price_usd", allow_zero=False)
            if quantity > state.position.quantity:
                raise ChallengeIntegrityError(f"SELL {event['event_id']} exceeds MSTR position")
            proceeds = quantity * price
            released_cost = quantity * state.position.average_cost
            state.cash["USD"] += proceeds
            state.realized_pl_usd += proceeds - released_cost
            remaining = state.position.quantity - quantity
            state.position = ChallengePosition(remaining, ZERO if remaining == ZERO else state.position.average_cost)
        elif event_type in {"FEE", "TAX"}:
            currency = str(event["currency"])
            amount = parse_decimal(event["amount"], "amount", allow_zero=False)
            if amount > state.cash[currency]:
                raise ChallengeIntegrityError(f"{event_type} {event['event_id']} exceeds {currency} cash")
            state.cash[currency] -= amount
            target = state.fees if event_type == "FEE" else state.taxes
            target[currency] += amount
            base_amount = _event_base_amount_usd(event)
            if base_amount is not None:
                state.realized_pl_usd -= base_amount
    return state


def event_by_telegram_update(events: Sequence[Mapping[str, Any]], update_id: int | None) -> dict[str, Any] | None:
    if update_id is None:
        return None
    for event in events:
        if event.get("private_source", {}).get("telegram_update_id") == update_id:
            return dict(event)
    return None


def append_checked_event(base_dir: Path, event: Mapping[str, Any]) -> ChallengeState:
    config = load_config(base_dir)
    events = read_events(base_dir)
    validate_event(event, config)
    if any(item["event_id"] == event["event_id"] for item in events):
        raise ChallengeIntegrityError(f"Duplicate challenge event: {event['event_id']}")
    candidate = [*events, dict(event)]
    state = replay_events(candidate)
    append_jsonl(event_path(base_dir), event)
    return state


def _reference_payload(market: MarketInputs | None) -> dict[str, Any] | None:
    if market is None:
        return None
    if market.mstr_price is None and market.btc_price is None and market.usd_idr is None:
        return None
    return {
        "MSTR": decimal_plain(market.mstr_price) if market.mstr_price is not None else None,
        "BTC": decimal_plain(market.btc_price) if market.btc_price is not None else None,
        "USD_IDR": decimal_plain(market.usd_idr) if market.usd_idr is not None else None,
        "as_of": market.fetched_at,
        "source": "challenge_market_inputs",
    }


def _base_usd_value(currency: str, amount: Decimal, market: MarketInputs | None) -> tuple[Decimal | None, Decimal | None, str | None]:
    if currency == "USD":
        return amount, None, None
    if market is None or market.usd_idr is None or market.usd_idr <= ZERO:
        return None, None, None
    return amount / market.usd_idr, market.usd_idr, market.fx_source or "display_fx_reference"


def initialize_challenge(
    base_dir: Path,
    *,
    currency: str,
    amount: Decimal,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
    market: MarketInputs | None = None,
    include_legacy_position: bool = False,
    opening_mstr_quantity: Decimal | None = None,
    opening_mstr_average_cost: Decimal | None = None,
) -> tuple[dict[str, Any], ChallengeState]:
    config = load_config(base_dir)
    if config["status"] != "prelaunch" or config["starting_event_id"] is not None:
        raise ChallengeValidationError("Challenge has already been initialized")
    currency = currency.upper()
    if currency not in VALID_CURRENCIES:
        raise ChallengeValidationError("Challenge currency must be USD or IDR")
    if amount < ZERO:
        raise ChallengeValidationError("Starting cash must not be negative")
    if include_legacy_position and (
        opening_mstr_quantity is None
        or opening_mstr_average_cost is None
        or opening_mstr_quantity <= ZERO
        or opening_mstr_average_cost <= ZERO
    ):
        raise ChallengeValidationError("Legacy inclusion requires a positive MSTR quantity and average cost")
    if not include_legacy_position and (opening_mstr_quantity is not None or opening_mstr_average_cost is not None):
        raise ChallengeValidationError("Opening position requires include_legacy_position")
    events = read_events(base_dir)
    base_amount, fx_rate, fx_source = _base_usd_value(currency, amount, market)
    values: dict[str, Any] = {
        "currency": currency,
        "amount": amount,
        "base_amount_usd": base_amount,
        "opening_mstr_quantity": opening_mstr_quantity,
        "opening_mstr_average_cost": opening_mstr_average_cost,
        "reference_prices": _reference_payload(market),
        "valuation_fx_rate": fx_rate,
        "valuation_fx_source": fx_source,
    }
    event = make_event(events, config, "CHALLENGE_INIT", timestamp_utc=timestamp_utc, source=source, values=values)
    state = append_checked_event(base_dir, event)
    updated = dict(config)
    updated.update(
        {
            "status": "active",
            "start_at_utc": event["timestamp_utc"],
            "starting_event_id": event["event_id"],
            "include_legacy_position": include_legacy_position,
        }
    )
    save_config(base_dir, updated)
    return event, state


def require_active(config: Mapping[str, Any]) -> None:
    if config.get("status") != "active":
        raise ChallengeValidationError(f"Challenge is {config.get('status')}; an active challenge is required")


def record_cash_event(
    base_dir: Path,
    *,
    event_type: str,
    currency: str,
    amount: Decimal,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
    market: MarketInputs | None = None,
    related_event_id: str | None = None,
) -> tuple[dict[str, Any], ChallengeState, bool]:
    config = load_config(base_dir)
    require_active(config)
    currency = currency.upper()
    if event_type not in {"DEPOSIT", "WITHDRAWAL", "FEE", "TAX"}:
        raise ChallengeValidationError("Unsupported cash event type")
    if currency not in VALID_CURRENCIES or amount <= ZERO:
        raise ChallengeValidationError("Cash event requires a positive USD or IDR amount")
    events = read_events(base_dir)
    duplicate = event_by_telegram_update(events, source.get("telegram_update_id"))
    if duplicate:
        return duplicate, replay_events(events), False
    base_amount, fx_rate, fx_source = _base_usd_value(currency, amount, market)
    event = make_event(
        events,
        config,
        event_type,
        timestamp_utc=timestamp_utc,
        source=source,
        values={
            "currency": currency,
            "amount": amount,
            "base_amount_usd": base_amount,
            "related_event_id": related_event_id,
            "reference_prices": _reference_payload(market),
            "valuation_fx_rate": fx_rate,
            "valuation_fx_source": fx_source,
        },
    )
    try:
        state = append_checked_event(base_dir, event)
    except ChallengeIntegrityError as exc:
        raise ChallengeValidationError(str(exc)) from exc
    return event, state, True


def record_fx_conversion(
    base_dir: Path,
    *,
    from_currency: str,
    from_amount: Decimal,
    to_currency: str,
    to_amount: Decimal,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
    market: MarketInputs | None = None,
) -> tuple[dict[str, Any], ChallengeState, bool]:
    config = load_config(base_dir)
    require_active(config)
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency not in VALID_CURRENCIES or to_currency not in VALID_CURRENCIES or from_currency == to_currency:
        raise ChallengeValidationError("FX conversion must use different USD/IDR currencies")
    if from_amount <= ZERO or to_amount <= ZERO:
        raise ChallengeValidationError("FX conversion amounts must be positive")
    events = read_events(base_dir)
    duplicate = event_by_telegram_update(events, source.get("telegram_update_id"))
    if duplicate:
        return duplicate, replay_events(events), False
    event = make_event(
        events,
        config,
        "FX_CONVERSION",
        timestamp_utc=timestamp_utc,
        source=source,
        values={
            "from_currency": from_currency,
            "to_currency": to_currency,
            "from_amount": from_amount,
            "to_amount": to_amount,
            "actual_fx_rate": to_amount / from_amount,
            "reference_prices": _reference_payload(market),
        },
    )
    try:
        state = append_checked_event(base_dir, event)
    except ChallengeIntegrityError as exc:
        raise ChallengeValidationError(str(exc)) from exc
    return event, state, True


def record_trade(
    base_dir: Path,
    *,
    event_type: str,
    quantity: Decimal,
    price_usd: Decimal,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
    thesis_zone: str | None = None,
    market: MarketInputs | None = None,
) -> tuple[dict[str, Any], ChallengeState, bool]:
    config = load_config(base_dir)
    require_active(config)
    if event_type not in {"BUY", "SELL"}:
        raise ChallengeValidationError("Challenge trade must be BUY or SELL")
    if quantity <= ZERO or price_usd <= ZERO:
        raise ChallengeValidationError("Trade quantity and price must be positive")
    if quantity.quantize(MSTR_QUANTUM) != quantity:
        raise ChallengeValidationError("MSTR quantity supports at most six decimal places")
    events = read_events(base_dir)
    duplicate = event_by_telegram_update(events, source.get("telegram_update_id"))
    if duplicate:
        return duplicate, replay_events(events), False
    event = make_event(
        events,
        config,
        event_type,
        timestamp_utc=timestamp_utc,
        source=source,
        values={
            "currency": "USD",
            "amount": quantity * price_usd,
            "asset": "MSTR",
            "quantity": quantity,
            "price_usd": price_usd,
            "thesis_zone": thesis_zone,
            "reference_prices": _reference_payload(market),
        },
    )
    try:
        state = append_checked_event(base_dir, event)
    except ChallengeIntegrityError as exc:
        raise ChallengeValidationError(str(exc)) from exc
    return event, state, True


def record_undo(
    base_dir: Path,
    *,
    target_event_id: str,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
) -> tuple[dict[str, Any], ChallengeState, bool]:
    config = load_config(base_dir)
    if config["status"] not in {"active", "paused"}:
        raise ChallengeValidationError("Challenge undo requires an active or paused challenge")
    target_event_id = target_event_id.upper()
    events = read_events(base_dir)
    duplicate = event_by_telegram_update(events, source.get("telegram_update_id"))
    if duplicate:
        return duplicate, replay_events(events), False
    by_id = {event["event_id"]: event for event in events}
    if target_event_id not in by_id or target_event_id in active_target_ids(events):
        raise ChallengeValidationError("Challenge event is missing or already undone")
    if by_id[target_event_id]["event_type"] in {"UNDO", "CHALLENGE_INIT"}:
        raise ChallengeValidationError("The selected challenge event cannot be undone")
    event = make_event(
        events,
        config,
        "UNDO",
        timestamp_utc=timestamp_utc,
        source=source,
        values={"target_event_id": target_event_id},
    )
    try:
        state = append_checked_event(base_dir, event)
    except ChallengeIntegrityError as exc:
        raise ChallengeValidationError(f"Undo would make challenge replay invalid: {exc}") from exc
    return event, state, True


def reset_challenge(
    base_dir: Path,
    *,
    timestamp_utc: datetime,
    source: Mapping[str, Any],
) -> tuple[dict[str, Any], ChallengeState]:
    config = load_config(base_dir)
    if config["status"] not in {"active", "paused", "completed"}:
        raise ChallengeValidationError("Challenge is already prelaunch")
    events = read_events(base_dir)
    event = make_event(events, config, "RESET", timestamp_utc=timestamp_utc, source=source)
    state = append_checked_event(base_dir, event)
    updated = dict(config)
    updated.update(
        {
            "status": "prelaunch",
            "start_at_utc": None,
            "starting_event_id": None,
            "include_legacy_position": False,
        }
    )
    save_config(base_dir, updated)
    return event, state


def set_challenge_status(base_dir: Path, status: str) -> dict[str, Any]:
    config = load_config(base_dir)
    status = status.lower()
    if status not in {"active", "paused", "completed"}:
        raise ChallengeValidationError("Status must be active, paused, or completed")
    if config["starting_event_id"] is None:
        raise ChallengeValidationError("Initialize the challenge before changing status")
    updated = dict(config)
    updated["status"] = status
    save_config(base_dir, updated)
    return updated


def load_market_inputs(base_dir: Path = Path("."), *, at: datetime | None = None) -> MarketInputs:
    state_file = base_dir / "data" / "portfolio_state.json"
    warnings: list[str] = []
    if not state_file.exists():
        return MarketInputs(warnings=("portfolio market cache is missing",))
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return MarketInputs(warnings=("portfolio market cache is unreadable",))
    market_cache = state.get("market_cache") if isinstance(state, dict) else None
    jisdor_cache = state.get("jisdor_cache") if isinstance(state, dict) else None
    market_cache = market_cache if isinstance(market_cache, dict) else {}
    jisdor_cache = jisdor_cache if isinstance(jisdor_cache, dict) else {}

    def cached_decimal(asset: str) -> Decimal | None:
        value = market_cache.get(asset)
        if not isinstance(value, dict) or value.get("price_usd") is None:
            return None
        try:
            return parse_decimal(value["price_usd"], f"market_cache.{asset}", allow_zero=False)
        except ChallengeIntegrityError:
            warnings.append(f"{asset} cache is invalid")
            return None

    mstr = market_cache.get("MSTR") if isinstance(market_cache.get("MSTR"), dict) else {}
    btc = market_cache.get("BTC") if isinstance(market_cache.get("BTC"), dict) else {}
    fx: Decimal | None = None
    if jisdor_cache.get("rate") is not None:
        try:
            fx = parse_decimal(jisdor_cache["rate"], "jisdor_cache.rate", allow_zero=False)
        except ChallengeIntegrityError:
            warnings.append("USD/IDR cache is invalid")
    fetched_values = [
        value
        for value in (mstr.get("fetched_at_utc"), btc.get("fetched_at_utc"), jisdor_cache.get("fetched_at_utc"))
        if isinstance(value, str)
    ]
    fetched_at = max(fetched_values) if fetched_values else None
    current = ensure_aware_utc(at or now_utc())
    ages: list[timedelta] = []
    for value in fetched_values:
        try:
            ages.append(current - parse_datetime(value, "market fetched_at").astimezone(UTC))
        except ChallengeIntegrityError:
            warnings.append("market cache timestamp is invalid")
    if not fetched_values:
        freshness = "unavailable"
    elif ages and max(ages) <= timedelta(minutes=90):
        freshness = "fresh"
    elif ages and max(ages) <= timedelta(hours=24):
        freshness = "cached"
    else:
        freshness = "stale"
    return MarketInputs(
        mstr_price=cached_decimal("MSTR"),
        btc_price=cached_decimal("BTC"),
        usd_idr=fx,
        mstr_as_of=mstr.get("as_of") if isinstance(mstr.get("as_of"), str) else None,
        btc_as_of=btc.get("as_of") if isinstance(btc.get("as_of"), str) else None,
        fx_as_of=jisdor_cache.get("official_date") if isinstance(jisdor_cache.get("official_date"), str) else None,
        mstr_source=mstr.get("source") if isinstance(mstr.get("source"), str) else None,
        btc_source=btc.get("source") if isinstance(btc.get("source"), str) else None,
        fx_source=jisdor_cache.get("source") if isinstance(jisdor_cache.get("source"), str) else None,
        fetched_at=fetched_at,
        freshness=freshness,
        market_status=us_equity_market_status(current, mstr.get("as_of") if isinstance(mstr.get("as_of"), str) else None),
        warnings=tuple(warnings),
    )


def create_snapshot(
    base_dir: Path,
    *,
    market: MarketInputs,
    captured_at: datetime | None = None,
) -> dict[str, Any] | None:
    config = load_config(base_dir)
    if config["status"] == "prelaunch":
        return None
    events = read_events(base_dir)
    state = replay_events(events)
    if not state.initialized:
        return None
    current = ensure_aware_utc(captured_at or now_utc())
    idr_equivalent = state.cash["IDR"] / market.usd_idr if market.usd_idr else None
    market_value = state.position.quantity * market.mstr_price if market.mstr_price else None
    total_value = None
    if market_value is not None and idr_equivalent is not None:
        total_value = state.cash["USD"] + idr_equivalent + market_value
    elif market_value is not None and state.cash["IDR"] == ZERO:
        total_value = state.cash["USD"] + market_value
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "challenge_id": config["challenge_id"],
        "captured_at_utc": iso_seconds(current),
        "captured_at_wib": wib_iso_seconds(current),
        "prices": {
            "MSTR": decimal_plain(market.mstr_price) if market.mstr_price is not None else None,
            "BTC": decimal_plain(market.btc_price) if market.btc_price is not None else None,
            "USD_IDR": decimal_plain(market.usd_idr) if market.usd_idr is not None else None,
        },
        "sources": {
            "MSTR": market.mstr_source,
            "BTC": market.btc_source,
            "USD_IDR": market.fx_source,
            "MSTR_as_of": market.mstr_as_of,
            "BTC_as_of": market.btc_as_of,
            "USD_IDR_as_of": market.fx_as_of,
            "freshness": market.freshness,
        },
        "portfolio": {
            "cash_usd": decimal_plain(state.cash["USD"]),
            "cash_idr": decimal_plain(state.cash["IDR"]),
            "mstr_quantity": decimal_plain(state.position.quantity),
            "mstr_average_cost": decimal_plain(state.position.average_cost),
            "total_value_usd": decimal_plain(total_value) if total_value is not None else None,
            "net_contributions_usd": decimal_plain(state.net_contributions_usd) if state.contribution_data_complete else None,
        },
    }
    validate_snapshot(snapshot)
    snapshots = read_snapshots(base_dir)
    if snapshots:
        last_at = parse_datetime(snapshots[-1]["captured_at_utc"], "captured_at_utc").astimezone(UTC)
        if current < last_at:
            raise ChallengeIntegrityError("A challenge snapshot cannot move backward in time")
        if current == last_at:
            snapshots[-1] = snapshot
        else:
            snapshots.append(snapshot)
    else:
        snapshots.append(snapshot)
    atomic_write_text(snapshot_path(base_dir), "".join(canonical_json(item) + "\n" for item in snapshots))
    return snapshot


def validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    required = {"schema_version", "challenge_id", "captured_at_utc", "captured_at_wib", "prices", "sources", "portfolio"}
    if set(snapshot) != required or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ChallengeIntegrityError("Challenge snapshot shape is invalid")
    parse_datetime(snapshot.get("captured_at_utc"), "snapshot captured_at_utc")
    parse_datetime(snapshot.get("captured_at_wib"), "snapshot captured_at_wib")
    if not isinstance(snapshot.get("prices"), dict) or set(snapshot["prices"]) != {"MSTR", "BTC", "USD_IDR"}:
        raise ChallengeIntegrityError("Challenge snapshot prices are invalid")
    for key, value in snapshot["prices"].items():
        if value is not None:
            parse_decimal(value, f"snapshot price {key}", allow_zero=False)
    if not isinstance(snapshot.get("sources"), dict) or not isinstance(snapshot.get("portfolio"), dict):
        raise ChallengeIntegrityError("Challenge snapshot source/portfolio data is invalid")
    for key in ("cash_usd", "cash_idr", "mstr_quantity", "mstr_average_cost"):
        parse_decimal(snapshot["portfolio"].get(key), f"snapshot portfolio {key}")
    for key in ("total_value_usd", "net_contributions_usd"):
        if snapshot["portfolio"].get(key) is not None:
            parse_decimal(snapshot["portfolio"][key], f"snapshot portfolio {key}", allow_negative=True)


def read_snapshots(base_dir: Path = Path(".")) -> list[dict[str, Any]]:
    ensure_challenge_files(base_dir)
    snapshots: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for line_number, line in enumerate(snapshot_path(base_dir).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            snapshot = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChallengeIntegrityError(f"Malformed challenge snapshot line {line_number}") from exc
        if not isinstance(snapshot, dict):
            raise ChallengeIntegrityError("Challenge snapshot line must be an object")
        validate_snapshot(snapshot)
        current_time = parse_datetime(snapshot["captured_at_utc"], "captured_at_utc")
        if previous_time is not None and current_time <= previous_time:
            raise ChallengeIntegrityError("Challenge snapshots must be strictly chronological")
        previous_time = current_time
        snapshots.append(snapshot)
    return snapshots


def _format_money(value: Decimal, currency: str) -> str:
    if currency == "USD":
        return f"USD {value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    return f"IDR {value.quantize(ONE, rounding=ROUND_HALF_UP):,.0f}"


def render_challenge_status(base_dir: Path = Path(".")) -> str:
    config = load_config(base_dir)
    events = read_events(base_dir)
    state = replay_events(events)
    lines = [
        "MSTR LIVE THESIS CHALLENGE",
        f"Status: {config['status'].upper()}",
        f"Challenge ID: {config['challenge_id']}",
    ]
    if config["start_at_utc"]:
        lines.append(f"Started: {config['start_at_utc']}")
    else:
        lines.append("Setup required: starting capital and official start are not configured.")
    lines.extend(
        [
            f"Active events: {len(state.active_events)}",
            "Legacy MSTR included: " + ("yes" if config["include_legacy_position"] else "no"),
        ]
    )
    return "\n".join(lines)


def render_cash(base_dir: Path = Path(".")) -> str:
    config = load_config(base_dir)
    if config["status"] == "prelaunch":
        return "Challenge cash is not configured. Use /challenge_init after confirming the official starting capital."
    state = replay_events(read_events(base_dir))
    return "\n".join(
        [
            "MSTR CHALLENGE CASH",
            f"USD: {_format_money(state.cash['USD'], 'USD')}",
            f"IDR: {_format_money(state.cash['IDR'], 'IDR')}",
        ]
    )


def render_challenge_portfolio(base_dir: Path = Path("."), market: MarketInputs | None = None) -> str:
    config = load_config(base_dir)
    if config["status"] == "prelaunch":
        return render_challenge_status(base_dir)
    state = replay_events(read_events(base_dir))
    market = market or load_market_inputs(base_dir)
    lines = [
        "MSTR LIVE THESIS CHALLENGE",
        f"Status: {config['status'].upper()}",
        f"Cash USD: {_format_money(state.cash['USD'], 'USD')}",
        f"Cash IDR: {_format_money(state.cash['IDR'], 'IDR')}",
        f"MSTR: {decimal_plain(state.position.quantity)} shares",
        f"Average cost: {_format_money(state.position.average_cost, 'USD')}",
    ]
    if market.mstr_price is not None:
        market_value = state.position.quantity * market.mstr_price
        lines.append(f"MSTR market value: {_format_money(market_value, 'USD')}")
        if state.cash["IDR"] == ZERO or market.usd_idr is not None:
            idr_cash_usd = ZERO if state.cash["IDR"] == ZERO else state.cash["IDR"] / market.usd_idr
            lines.append(f"Total value: {_format_money(state.cash['USD'] + idr_cash_usd + market_value, 'USD')}")
    lines.append(f"Data freshness: {market.freshness}")
    return "\n".join(lines)


def render_challenge_history(base_dir: Path = Path("."), limit: int = 20) -> str:
    events = read_events(base_dir)
    undone = active_target_ids(events)
    rows = []
    for event in reversed(events[-limit:]):
        status = "UNDONE" if event["event_id"] in undone else "ACTIVE"
        detail = event["event_type"]
        if event["event_type"] in {"BUY", "SELL"}:
            detail += f" MSTR {event['quantity']} @ USD {event['price_usd']}"
        elif event.get("amount") is not None:
            detail += f" {event['currency']} {event['amount']}"
        elif event["event_type"] == "FX_CONVERSION":
            detail += f" {event['from_currency']} {event['from_amount']} -> {event['to_currency']} {event['to_amount']}"
        elif event["event_type"] == "UNDO":
            detail += f" {event['target_event_id']}"
        rows.append(f"{event['event_id']} | {detail} | {status}")
    return "MSTR CHALLENGE HISTORY\n" + ("\n".join(rows) if rows else "No challenge events.")


def handle_challenge_command(
    base_dir: Path,
    command: str,
    args: Sequence[str],
    *,
    timestamp_utc: datetime,
    telegram_update_id: int,
    telegram_message_id: int | None,
    chat_id: str,
    market: MarketInputs | None = None,
    thesis_zone: str | None = None,
) -> tuple[str, bool]:
    source = private_source(
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        source="telegram",
    )
    market = market or load_market_inputs(base_dir, at=timestamp_utc)
    if command == "challenge_status":
        if args:
            raise ChallengeValidationError("/challenge_status does not accept parameters")
        return render_challenge_status(base_dir), False
    if command == "challenge_init":
        if len(args) != 2:
            raise ChallengeValidationError("Usage: /challenge_init USD 1000")
        currency = args[0].upper()
        amount = parse_owner_decimal(args[1], "starting cash", allow_zero=True)
        event, _ = initialize_challenge(
            base_dir,
            currency=currency,
            amount=amount,
            timestamp_utc=timestamp_utc,
            source=source,
            market=market,
        )
        return f"Challenge initialized from zero. Event: {event['event_id']}\nLegacy positions were not included.", True
    if command == "cash":
        if args:
            raise ChallengeValidationError("/cash does not accept parameters")
        return render_cash(base_dir), False
    if command in {"deposit", "withdraw", "fee", "tax"}:
        if len(args) != 2:
            raise ChallengeValidationError(f"Usage: /{command} USD 100")
        event_type = {
            "deposit": "DEPOSIT",
            "withdraw": "WITHDRAWAL",
            "fee": "FEE",
            "tax": "TAX",
        }[command]
        event, _, mutated = record_cash_event(
            base_dir,
            event_type=event_type,
            currency=args[0],
            amount=parse_owner_decimal(args[1], "amount"),
            timestamp_utc=timestamp_utc,
            source=source,
            market=market,
        )
        return f"{event_type} recorded: {event['event_id']}", mutated
    if command == "fx_convert":
        if len(args) != 4:
            raise ChallengeValidationError("Usage: /fx_convert IDR 15000000 USD 830")
        event, _, mutated = record_fx_conversion(
            base_dir,
            from_currency=args[0],
            from_amount=parse_owner_decimal(args[1], "from amount"),
            to_currency=args[2],
            to_amount=parse_owner_decimal(args[3], "to amount"),
            timestamp_utc=timestamp_utc,
            source=source,
            market=market,
        )
        return f"FX conversion recorded: {event['event_id']} at actual rate {event['actual_fx_rate']}", mutated
    if command in {"buy_mstr", "sell_mstr"}:
        if len(args) != 2:
            raise ChallengeValidationError(f"Usage: /{command} QUANTITY PRICE_USD")
        event, _, mutated = record_trade(
            base_dir,
            event_type="BUY" if command == "buy_mstr" else "SELL",
            quantity=parse_owner_decimal(args[0], "MSTR quantity"),
            price_usd=parse_owner_decimal(args[1], "MSTR price"),
            timestamp_utc=timestamp_utc,
            source=source,
            thesis_zone=thesis_zone,
            market=market,
        )
        return f"{event['event_type']} MSTR recorded: {event['event_id']}", mutated
    if command == "portofolio":
        if args:
            raise ChallengeValidationError("/portofolio does not accept parameters")
        return render_challenge_portfolio(base_dir, market), False
    if command == "history":
        if len(args) > 1 or (args and (not args[0].isdigit() or not 1 <= int(args[0]) <= 100)):
            raise ChallengeValidationError("Usage: /history [1-100]")
        return render_challenge_history(base_dir, int(args[0]) if args else 20), False
    if command == "undo":
        if len(args) != 1 or not EVENT_ID_RE.fullmatch(args[0].upper()):
            raise ChallengeValidationError("Usage: /undo CH-000001")
        event, _, mutated = record_undo(
            base_dir,
            target_event_id=args[0],
            timestamp_utc=timestamp_utc,
            source=source,
        )
        return f"Challenge event undone by {event['event_id']}", mutated
    if command == "challenge_reset":
        if list(args) != ["CONFIRM"]:
            raise ChallengeValidationError("Usage: /challenge_reset CONFIRM")
        event, _ = reset_challenge(base_dir, timestamp_utc=timestamp_utc, source=source)
        return f"Challenge returned to prelaunch: {event['event_id']}", True
    raise ChallengeValidationError(f"Unsupported challenge command: /{command}")


def _public_balance(state: ChallengeState) -> dict[str, Any]:
    return {
        "initialized": state.initialized,
        "cash_usd": decimal_plain(state.cash["USD"]),
        "cash_idr": decimal_plain(state.cash["IDR"]),
        "mstr_quantity": decimal_plain(state.position.quantity),
        "mstr_average_cost": decimal_plain(state.position.average_cost),
        "realized_pl_usd": decimal_plain(state.realized_pl_usd),
        "net_contributions_usd": (
            decimal_plain(state.net_contributions_usd) if state.contribution_data_complete else None
        ),
    }


def _public_event(
    event: Mapping[str, Any],
    undone: set[str],
    source_commit_sha: str | None,
    balance_before: Mapping[str, Any],
    balance_after: Mapping[str, Any],
) -> dict[str, Any]:
    value_usd = None
    if event["event_type"] in {"BUY", "SELL"}:
        value_usd = event["amount"]
    elif event.get("currency") == "USD":
        value_usd = event.get("amount")
    return {
        "event_id": event["event_id"],
        "timestamp_utc": event["timestamp_utc"],
        "timestamp_wib": event["timestamp_wib"],
        "type": event["event_type"],
        "asset": event.get("asset"),
        "currency": event.get("currency"),
        "amount": event.get("amount"),
        "base_amount_usd": event.get("base_amount_usd"),
        "quantity": event.get("quantity"),
        "price_usd": event.get("price_usd"),
        "value_usd": value_usd,
        "from_currency": event.get("from_currency"),
        "to_currency": event.get("to_currency"),
        "from_amount": event.get("from_amount"),
        "to_amount": event.get("to_amount"),
        "actual_fx_rate": event.get("actual_fx_rate"),
        "target_event_id": event.get("target_event_id"),
        "related_event_id": event.get("related_event_id"),
        "opening_mstr_quantity": event.get("opening_mstr_quantity"),
        "opening_mstr_average_cost": event.get("opening_mstr_average_cost"),
        "status": "undone" if event["event_id"] in undone else "active",
        "thesis_zone": event.get("thesis_zone"),
        "balance_before": dict(balance_before),
        "balance_after": dict(balance_after),
        "source_commit_sha": source_commit_sha,
    }


def public_events(
    events: Sequence[Mapping[str, Any]],
    source_commit_sha: str | None,
) -> list[dict[str, Any]]:
    undone = active_target_ids(events)
    effective_prefix: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    for event in events:
        state_before = replay_events(effective_prefix)
        if event["event_type"] != "UNDO" and event["event_id"] not in undone:
            effective_prefix.append(dict(event))
        state_after = replay_events(effective_prefix)
        if event["event_type"] in {"BUY", "SELL"} and event["event_id"] not in undone:
            result.append(
                _public_event(
                    event,
                    undone,
                    source_commit_sha,
                    _public_balance(state_before),
                    _public_balance(state_after),
                )
            )
    return result


def public_ledger_hash(events: Sequence[Mapping[str, Any]]) -> str:
    exported_events = public_events(events, None)
    return hashlib.sha256(canonical_json(exported_events).encode("utf-8")).hexdigest()


def _snapshot_decimal(snapshot: Mapping[str, Any], section: str, key: str) -> Decimal | None:
    value = snapshot.get(section, {}).get(key)
    return parse_decimal(value, f"snapshot {section}.{key}", allow_negative=True) if value is not None else None


def _closest_reference_price(
    event: Mapping[str, Any],
    asset: str,
    snapshots: Sequence[Mapping[str, Any]],
) -> Decimal | None:
    references = event.get("reference_prices")
    if isinstance(references, dict) and references.get(asset) is not None:
        return parse_decimal(references[asset], f"reference {asset}", allow_zero=False)
    if not snapshots:
        return None
    event_time = parse_datetime(event["timestamp_utc"], "event timestamp")
    candidates = sorted(
        snapshots,
        key=lambda item: abs((parse_datetime(item["captured_at_utc"], "snapshot timestamp") - event_time).total_seconds()),
    )
    for snapshot in candidates:
        value = snapshot.get("prices", {}).get(asset)
        if value is not None:
            return parse_decimal(value, f"snapshot {asset}", allow_zero=False)
    return None


def _external_flow_usd(event: Mapping[str, Any]) -> Decimal | None:
    if event["event_type"] not in {"CHALLENGE_INIT", "DEPOSIT", "WITHDRAWAL"}:
        return ZERO
    amount = _event_base_amount_usd(event)
    if amount is None:
        return None
    return -amount if event["event_type"] == "WITHDRAWAL" else amount


def current_era_events(
    config: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    starting_event_id = config.get("starting_event_id")
    if not isinstance(starting_event_id, str):
        return []
    for index, event in enumerate(events):
        if event["event_id"] == starting_event_id:
            return [dict(item) for item in events[index:]]
    return []


def current_era_snapshots(
    config: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    start_at = config.get("start_at_utc")
    if not isinstance(start_at, str):
        return []
    start = parse_datetime(start_at, "start_at_utc").astimezone(UTC)
    return [
        dict(snapshot)
        for snapshot in snapshots
        if parse_datetime(snapshot["captured_at_utc"], "captured_at_utc").astimezone(UTC) >= start
    ]


def modified_dietz_return(
    begin_value: Decimal,
    end_value: Decimal,
    begin_at: datetime,
    end_at: datetime,
    events: Sequence[Mapping[str, Any]],
) -> tuple[Decimal | None, str | None]:
    start = ensure_aware_utc(begin_at)
    finish = ensure_aware_utc(end_at)
    elapsed_seconds = Decimal(str((finish - start).total_seconds()))
    if elapsed_seconds <= ZERO:
        return None, "Modified Dietz requires two chronologically distinct observations"
    total_flow = ZERO
    weighted_flow = ZERO
    for event in events:
        event_at = parse_datetime(event["timestamp_utc"], "event timestamp").astimezone(UTC)
        if event_at <= start or event_at > finish:
            continue
        flow = _external_flow_usd(event)
        if flow is None:
            return None, "Modified Dietz cannot value a non-USD external flow"
        if flow == ZERO:
            continue
        remaining_seconds = Decimal(str((finish - event_at).total_seconds()))
        weight = remaining_seconds / elapsed_seconds
        total_flow += flow
        weighted_flow += flow * weight
    denominator = begin_value + weighted_flow
    if denominator <= ZERO:
        return None, "Modified Dietz denominator is not positive"
    return (end_value - begin_value - total_flow) / denominator * HUNDRED, None


def benchmark_value_at(
    events: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    asset: str,
    at: datetime,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    units = ZERO
    for event in events:
        if event["event_type"] in {"UNDO", "RESET"} or event["event_id"] in active_target_ids(events):
            continue
        if parse_datetime(event["timestamp_utc"], "event timestamp") > at:
            continue
        flow = _external_flow_usd(event)
        if flow == ZERO:
            continue
        if flow is None:
            return None, None, "non-USD external flow lacks a timestamped USD valuation"
        price = _closest_reference_price(event, asset, snapshots)
        if price is None:
            return None, None, f"{asset} price unavailable for an external cash flow"
        if flow > ZERO:
            units += flow / price
        else:
            units_to_sell = abs(flow) / price
            if units_to_sell > units:
                return None, None, f"withdrawal exceeds {asset} benchmark value"
            units -= units_to_sell
    eligible_snapshots = [
        item for item in snapshots if parse_datetime(item["captured_at_utc"], "snapshot timestamp") <= at
    ]
    if not eligible_snapshots:
        return None, units, f"{asset} current benchmark price is unavailable"
    current_price = None
    for snapshot in reversed(eligible_snapshots):
        value = snapshot.get("prices", {}).get(asset)
        if value is not None:
            current_price = parse_decimal(value, f"current {asset} price", allow_zero=False)
            break
    if current_price is None:
        return None, units, f"{asset} current benchmark price is unavailable"
    return units * current_price, units, None


def _series_statistics(values: Sequence[Decimal]) -> dict[str, str | None]:
    if not values:
        return {
            "return_pct": None,
            "max_drawdown_pct": None,
            "volatility_pct": None,
            "best_period_pct": None,
            "worst_period_pct": None,
        }
    if values[0] <= ZERO:
        total_return = None
    else:
        total_return = (values[-1] / values[0] - ONE) * HUNDRED
    peak = values[0]
    max_drawdown = ZERO
    period_returns: list[Decimal] = []
    for previous, current in zip(values, values[1:]):
        if current > peak:
            peak = current
        if peak > ZERO:
            drawdown = (current / peak - ONE) * HUNDRED
            max_drawdown = min(max_drawdown, drawdown)
        if previous > ZERO:
            period_returns.append((current / previous - ONE) * HUNDRED)
    volatility = None
    if len(period_returns) >= 2:
        mean = sum(period_returns, ZERO) / Decimal(len(period_returns))
        variance = sum(((item - mean) ** 2 for item in period_returns), ZERO) / Decimal(len(period_returns) - 1)
        volatility = variance.sqrt()
    return {
        "return_pct": decimal_plain(total_return) if total_return is not None else None,
        "max_drawdown_pct": decimal_plain(max_drawdown),
        "volatility_pct": decimal_plain(volatility) if volatility is not None else None,
        "best_period_pct": decimal_plain(max(period_returns)) if period_returns else None,
        "worst_period_pct": decimal_plain(min(period_returns)) if period_returns else None,
    }


def _performance_statistics(
    points: Sequence[tuple[datetime, Decimal]],
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str | None], str | None]:
    statistics = _series_statistics([value for _, value in points])
    if len(points) < 2:
        statistics["return_pct"] = None
        return statistics, "Modified Dietz requires at least two valued observations"
    dietz, reason = modified_dietz_return(
        points[0][1],
        points[-1][1],
        points[0][0],
        points[-1][0],
        events,
    )
    statistics["return_pct"] = decimal_plain(dietz) if dietz is not None else None
    return statistics, reason


def _performance_metric_bundle(
    actual_points: Sequence[tuple[datetime, Decimal]],
    btc_points: Sequence[tuple[datetime, Decimal]],
    mstr_points: Sequence[tuple[datetime, Decimal]],
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    challenge_statistics, challenge_reason = _performance_statistics(actual_points, events)
    btc_statistics, btc_reason = _performance_statistics(btc_points, events)
    mstr_statistics, mstr_reason = _performance_statistics(mstr_points, events)
    reasons = {reason for reason in (challenge_reason, btc_reason, mstr_reason) if reason}
    metrics: dict[str, Any] = {
        "challenge": challenge_statistics,
        "direct_btc": btc_statistics,
        "mstr_buy_hold": mstr_statistics,
    }
    challenge_return = challenge_statistics["return_pct"]
    btc_return = btc_statistics["return_pct"]
    mstr_return = mstr_statistics["return_pct"]
    metrics["alpha_vs_btc_pct"] = (
        decimal_plain(Decimal(challenge_return) - Decimal(btc_return))
        if challenge_return is not None and btc_return is not None
        else None
    )
    metrics["alpha_vs_mstr_buy_hold_pct"] = (
        decimal_plain(Decimal(challenge_return) - Decimal(mstr_return))
        if challenge_return is not None and mstr_return is not None
        else None
    )
    return metrics, reasons


def _performance_range_start(label: str, end_at: datetime) -> datetime | None:
    current = ensure_aware_utc(end_at)
    if label == "1D":
        return current - timedelta(days=1)
    if label == "1W":
        return current - timedelta(days=7)
    if label == "1M":
        return current - timedelta(days=30)
    if label == "3M":
        return current - timedelta(days=90)
    if label == "YTD":
        return datetime(current.year, 1, 1, tzinfo=UTC)
    if label == "ALL":
        return None
    raise ChallengeIntegrityError(f"Unsupported performance range: {label}")


def build_performance_export(
    config: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    generated_at: str,
) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    actual_points: list[tuple[datetime, Decimal]] = []
    btc_points: list[tuple[datetime, Decimal]] = []
    mstr_points: list[tuple[datetime, Decimal]] = []
    reasons: set[str] = set()
    era_events = current_era_events(config, events)
    undone = active_target_ids(era_events)
    active_events = [
        event
        for event in era_events
        if event["event_type"] != "UNDO" and event["event_id"] not in undone
    ]
    era_snapshots = current_era_snapshots(config, snapshots)
    for snapshot in era_snapshots:
        timestamp = parse_datetime(snapshot["captured_at_utc"], "captured_at_utc")
        actual = _snapshot_decimal(snapshot, "portfolio", "total_value_usd")
        invested = _snapshot_decimal(snapshot, "portfolio", "net_contributions_usd")
        btc_value, btc_units, btc_reason = benchmark_value_at(active_events, era_snapshots, asset="BTC", at=timestamp)
        mstr_value, mstr_units, mstr_reason = benchmark_value_at(active_events, era_snapshots, asset="MSTR", at=timestamp)
        for reason in (btc_reason, mstr_reason):
            if reason:
                reasons.add(reason)
        series.append(
            {
                "timestamp": snapshot["captured_at_utc"],
                "challenge_portfolio_usd": decimal_plain(actual) if actual is not None else None,
                "direct_btc_usd": decimal_plain(btc_value) if btc_value is not None else None,
                "direct_btc_quantity": decimal_plain(btc_units) if btc_units is not None else None,
                "mstr_buy_hold_usd": decimal_plain(mstr_value) if mstr_value is not None else None,
                "mstr_buy_hold_quantity": decimal_plain(mstr_units) if mstr_units is not None else None,
                "invested_capital_usd": decimal_plain(invested) if invested is not None else None,
                "freshness": snapshot.get("sources", {}).get("freshness", "unavailable"),
            }
        )
        if actual is not None:
            actual_points.append((timestamp, actual))
        if btc_value is not None:
            btc_points.append((timestamp, btc_value))
        if mstr_value is not None:
            mstr_points.append((timestamp, mstr_value))
    metrics, metric_reasons = _performance_metric_bundle(
        actual_points,
        btc_points,
        mstr_points,
        active_events,
    )
    reasons.update(metric_reasons)
    range_end = (
        parse_datetime(era_snapshots[-1]["captured_at_utc"], "captured_at_utc").astimezone(UTC)
        if era_snapshots
        else None
    )
    range_metrics: dict[str, Any] = {}
    for label in ("1D", "1W", "1M", "3M", "YTD", "ALL"):
        start = _performance_range_start(label, range_end) if range_end is not None else None

        def in_range(point: tuple[datetime, Decimal]) -> bool:
            return start is None or ensure_aware_utc(point[0]) >= start

        range_actual = [point for point in actual_points if in_range(point)]
        range_btc = [point for point in btc_points if in_range(point)]
        range_mstr = [point for point in mstr_points if in_range(point)]
        range_bundle, range_reasons = _performance_metric_bundle(
            range_actual,
            range_btc,
            range_mstr,
            active_events,
        )
        range_metrics[label] = {
            "start_at": iso_seconds(start) if start is not None else config.get("start_at_utc"),
            "end_at": iso_seconds(range_end) if range_end is not None else None,
            "observation_count": len(range_actual),
            "data_sufficient": len(range_actual) >= 2 and not range_reasons,
            "insufficiency_reasons": sorted(range_reasons),
            "metrics": range_bundle,
        }
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated_at,
        "challenge_id": config["challenge_id"],
        "status": config["status"],
        "methodology": {
            "portfolio_return": "Modified Dietz with external cash flows; interval series when sufficient",
            "direct_btc": "Each external cash flow is applied to BTC at the closest source-labelled price",
            "mstr_buy_hold": "Each external cash flow is applied to MSTR at the closest source-labelled price",
            "benchmark_fee_pct": "0",
            "invested_capital": "Cumulative external contributions less withdrawals",
            "volatility": "Sample standard deviation of available observation returns; not annualized",
        },
        "data_sufficient": len(series) >= 2 and not reasons,
        "insufficiency_reasons": sorted(reasons),
        "series": series,
        "metrics": metrics,
        "ranges": range_metrics,
    }


def _load_engine_state(base_dir: Path) -> dict[str, Any]:
    path = base_dir / "mstr_decision_engine_v2_state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _disclosure_observation(engine: Mapping[str, Any]) -> dict[str, Any] | None:
    fingerprint = engine.get("fingerprint")
    if not isinstance(fingerprint, dict) or not engine.get("updated_at_utc"):
        return None
    public_fingerprint = {
        field: _public_numeric_strings(fingerprint.get(field))
        for field in DISCLOSURE_FINGERPRINT_FIELDS
    }
    comparison_key = hashlib.sha256(canonical_json(public_fingerprint).encode("utf-8")).hexdigest()
    zones = engine.get("zones") if isinstance(engine.get("zones"), dict) else {}
    annual_dividends = _decimal_or_none(fingerprint.get("annual_dividends_b"))
    preferred_notional = _decimal_or_none(fingerprint.get("preferred_b"))
    preferred_cash_yield = (
        annual_dividends / preferred_notional * HUNDRED
        if annual_dividends is not None and preferred_notional is not None and preferred_notional > ZERO
        else None
    )
    return {
        "as_of": str(engine["updated_at_utc"]),
        "comparison_key": comparison_key,
        "fingerprint": public_fingerprint,
        "zones": {
            "accretion_score": _public_numeric_strings(zones.get("accretion_score")),
            "fair_ev_nav": _public_numeric_strings(zones.get("fair_ev_nav")),
            "liquidity_score": _public_numeric_strings(zones.get("liquidity_score")),
            "preferred_cash_yield_pct": _public_numeric_strings(preferred_cash_yield),
            "risk_score": _public_numeric_strings(zones.get("risk_score")),
        },
    }


def load_disclosure_history(base_dir: Path) -> list[dict[str, Any]]:
    path = disclosure_history_path(base_dir)
    if not path.exists():
        return []
    observations: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChallengeIntegrityError(
                f"mstr_disclosure_history.jsonl line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(item, dict) or not isinstance(item.get("fingerprint"), dict):
            raise ChallengeIntegrityError(
                f"mstr_disclosure_history.jsonl line {line_number} is invalid"
            )
        parse_datetime(item.get("as_of"), f"disclosure history line {line_number} as_of")
        if not isinstance(item.get("comparison_key"), str) or not item["comparison_key"]:
            raise ChallengeIntegrityError(
                f"mstr_disclosure_history.jsonl line {line_number} has no comparison key"
            )
        observations.append(item)
    observations.sort(key=lambda item: parse_datetime(item["as_of"], "disclosure as_of"))
    return observations


def record_engine_disclosure(base_dir: Path, engine: Mapping[str, Any]) -> bool:
    observation = _disclosure_observation(engine)
    if observation is None:
        return False
    history = load_disclosure_history(base_dir)
    if history and history[-1]["comparison_key"] == observation["comparison_key"]:
        return False
    append_jsonl(disclosure_history_path(base_dir), observation)
    return True


def _percentage_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == ZERO:
        return None
    return (current / previous - ONE) * HUNDRED


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _public_numeric_strings(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_plain(value)
    if isinstance(value, float):
        converted = Decimal(str(value))
        return decimal_plain(converted) if converted.is_finite() else None
    if isinstance(value, dict):
        return {str(key): _public_numeric_strings(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_public_numeric_strings(child) for child in value]
    return value


def _gate_item(name: str, value: Any, threshold: str, passed: bool | None, source: str, explanation: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "threshold": threshold,
        "passed": passed,
        "status": "pass" if passed is True else "fail" if passed is False else "unknown",
        "source": source,
        "explanation": explanation,
    }


def _invalidation_item(name: str, value: Any, threshold: str, triggered: bool, source: str, explanation: str) -> dict[str, Any]:
    item = _gate_item(name, value, threshold, triggered, source, explanation)
    item["triggered"] = triggered
    item["status"] = "fail" if triggered else "pass"
    return item


def _observation_btc_per_adso(observation: Mapping[str, Any]) -> Decimal | None:
    fingerprint = observation.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        return None
    holdings = _decimal_or_none(fingerprint.get("btc_holdings"))
    shares = _decimal_or_none(fingerprint.get("diluted_shares_m"))
    return holdings / (shares * Decimal("1000000")) if holdings and shares else None


def _series_changes(values: Sequence[Decimal]) -> list[Decimal]:
    return [change for previous, current in zip(values, values[1:]) if (change := _percentage_change(current, previous)) is not None]


def _bitcoin_regime(base_dir: Path, current_price: Decimal | None) -> tuple[str, Decimal]:
    prices: list[Decimal] = []
    try:
        snapshots = read_snapshots(base_dir)
    except ChallengeError:
        snapshots = []
    for snapshot in snapshots:
        price = _decimal_or_none(snapshot.get("prices", {}).get("BTC")) if isinstance(snapshot.get("prices"), dict) else None
        if price is not None and (not prices or price != prices[-1]):
            prices.append(price)
    if current_price is not None and (not prices or current_price != prices[-1]):
        prices.append(current_price)
    if len(prices) < 2:
        return "NEUTRAL", ZERO
    change = _percentage_change(prices[-1], prices[0]) or ZERO
    if change >= Decimal("10"):
        return "EXPANSION", change
    if change >= ZERO:
        return "RECOVERY", change
    if change >= Decimal("-10"):
        return "CONSOLIDATION", change
    return "STRESS", change


def build_thesis_export(
    base_dir: Path,
    market: MarketInputs,
    *,
    generated_at: str,
    source_commit_sha: str | None,
) -> dict[str, Any]:
    engine = _load_engine_state(base_dir)
    fingerprint = engine.get("fingerprint") if isinstance(engine.get("fingerprint"), dict) else {}
    zones = engine.get("zones") if isinstance(engine.get("zones"), dict) else {}
    reserve_months = _decimal_or_none(fingerprint.get("usd_div_coverage_months"))
    btc_holdings = _decimal_or_none(fingerprint.get("btc_holdings"))
    basic_shares = _decimal_or_none(fingerprint.get("basic_shares_m"))
    diluted_shares = _decimal_or_none(fingerprint.get("diluted_shares_m"))
    btc_per_basic = btc_holdings / (basic_shares * Decimal("1000000")) if btc_holdings and basic_shares else None
    btc_per_adso = btc_holdings / (diluted_shares * Decimal("1000000")) if btc_holdings and diluted_shares else None
    risk_score = _decimal_or_none(zones.get("risk_score"))
    liquidity_score = _decimal_or_none(zones.get("liquidity_score"))
    accretion_score = _decimal_or_none(zones.get("accretion_score"))

    history = load_disclosure_history(base_dir)
    current_observation = _disclosure_observation(engine)
    if current_observation is not None and (
        not history or history[-1]["comparison_key"] != current_observation["comparison_key"]
    ):
        history.append(current_observation)
    previous_observation = history[-2] if len(history) >= 2 else None
    previous_fingerprint = previous_observation.get("fingerprint", {}) if previous_observation else {}
    previous_btc_holdings = _decimal_or_none(previous_fingerprint.get("btc_holdings"))
    previous_basic_shares = _decimal_or_none(previous_fingerprint.get("basic_shares_m"))
    previous_diluted_shares = _decimal_or_none(previous_fingerprint.get("diluted_shares_m"))
    previous_btc_per_basic = (
        previous_btc_holdings / (previous_basic_shares * Decimal("1000000"))
        if previous_btc_holdings and previous_basic_shares
        else None
    )
    previous_btc_per_adso = (
        previous_btc_holdings / (previous_diluted_shares * Decimal("1000000"))
        if previous_btc_holdings and previous_diluted_shares
        else None
    )
    btc_per_basic_change = _percentage_change(btc_per_basic, previous_btc_per_basic)
    btc_per_adso_change = _percentage_change(btc_per_adso, previous_btc_per_adso)
    disclosure_trend = None
    if btc_per_adso_change is not None:
        disclosure_trend = "stable" if btc_per_adso_change == ZERO else "improving" if btc_per_adso_change > ZERO else "deteriorating"
    disclosure_count = len(history)

    btc_adso_series = [value for observation in history if (value := _observation_btc_per_adso(observation)) is not None]
    btc_adso_changes = _series_changes(btc_adso_series)
    three_update_available = len(btc_adso_series) >= 3
    three_non_deteriorating = three_update_available and all(change >= ZERO for change in btc_adso_changes[-2:])
    repeated_adso_deterioration = three_update_available and all(change < ZERO for change in btc_adso_changes[-2:])

    btc_history = [
        value
        for observation in history
        if isinstance(observation.get("fingerprint"), Mapping)
        and (value := _decimal_or_none(observation["fingerprint"].get("btc_holdings"))) is not None
    ]
    btc_reduction_count = sum(current < previous for previous, current in zip(btc_history, btc_history[1:]))
    repeated_obligation_sales = btc_reduction_count >= 2

    mnav_history = [
        value
        for observation in history
        if isinstance(observation.get("zones"), Mapping)
        and (value := _decimal_or_none(observation["zones"].get("fair_ev_nav"))) is not None
    ]
    mnav_changes = _series_changes(mnav_history)
    residual_damage = len(mnav_changes) >= 2 and all(change < ZERO for change in mnav_changes[-2:])
    residual_change = mnav_changes[-1] if mnav_changes else ZERO

    debt = _decimal_or_none(fingerprint.get("debt_b")) or ZERO
    preferred = _decimal_or_none(fingerprint.get("preferred_b")) or ZERO
    annual_dividends = _decimal_or_none(fingerprint.get("annual_dividends_b")) or ZERO
    preferred_cash_yield = annual_dividends / preferred * HUNDRED if preferred > ZERO else ZERO
    preferred_yield_history = [
        value
        for observation in history
        if isinstance(observation.get("zones"), Mapping)
        and (value := _decimal_or_none(observation["zones"].get("preferred_cash_yield_pct"))) is not None
    ]
    previous_preferred_yield = preferred_yield_history[-1] if preferred_yield_history else preferred_cash_yield
    preferred_normalizing = preferred_cash_yield <= previous_preferred_yield + Decimal("0.25")

    previous_debt = _decimal_or_none(previous_fingerprint.get("debt_b")) or debt
    previous_preferred = _decimal_or_none(previous_fingerprint.get("preferred_b")) or preferred
    previous_reserve_months = _decimal_or_none(previous_fingerprint.get("usd_div_coverage_months")) or reserve_months or ZERO
    obligations_increase = debt + preferred > (previous_debt + previous_preferred) * Decimal("1.05")
    destructive_refinancing = obligations_increase and reserve_months is not None and reserve_months < previous_reserve_months

    price = market.mstr_price
    fair_price = _decimal_or_none(zones.get("fair_price"))
    fair_mnav = _decimal_or_none(zones.get("fair_ev_nav"))
    supportive_valuation = price is not None and fair_price is not None and price <= fair_price and (fair_mnav is None or fair_mnav <= Decimal("1.5"))
    issuance_accretive = accretion_score is not None and accretion_score >= ZERO
    funding_stress = "LOW" if reserve_months is not None and reserve_months > Decimal("18") and liquidity_score is not None and liquidity_score >= Decimal("0.5") else "MODERATE" if reserve_months is not None and reserve_months >= Decimal("12") else "HIGH"
    financing_condition = "STABLE" if issuance_accretive and funding_stress == "LOW" else "WATCH" if funding_stress == "MODERATE" else "STRESSED"
    bitcoin_regime, bitcoin_change = _bitcoin_regime(base_dir, market.btc_price)

    no_red = None
    if risk_score is not None and liquidity_score is not None:
        no_red = risk_score < Decimal("0.75") and liquidity_score >= Decimal("0.25")
    starter_checks = [
        _gate_item("MSTR price", decimal_plain(price) if price is not None else None, "<= 85 USD", price <= Decimal("85") if price is not None else None, market.mstr_source or "market cache", "Price alone is insufficient."),
        _gate_item("Reserve coverage", decimal_plain(reserve_months) if reserve_months is not None else None, ">= 15 months", reserve_months >= Decimal("15") if reserve_months is not None else None, "decision engine fingerprint", "Policy reserve divided by modeled annual cash burden."),
        _gate_item("BTC per ADSO", decimal_plain(btc_per_adso) if btc_per_adso is not None else None, "stabilizing", btc_per_adso_change >= ZERO if btc_per_adso_change is not None else None, "corporate disclosure history", "Compared with the previous comparable disclosure."),
        _gate_item("Risk dashboard", None if no_red is None else "not red" if no_red else "red", "no red indicator", no_red, "decision engine scores", "Risk and liquidity inputs must both be available."),
    ]
    invalidation_checks = [
        _invalidation_item("Reserve below 12 months", decimal_plain(reserve_months) if reserve_months is not None else "0", "< 12 months", reserve_months is not None and reserve_months < Decimal("12"), "decision engine fingerprint", "Immediate review threshold from the original thesis."),
        _invalidation_item("Obligation-funded BTC sales", f"{btc_reduction_count} REDUCTION" if btc_reduction_count == 1 else f"{btc_reduction_count} REDUCTIONS", "repeated sales", repeated_obligation_sales, "corporate disclosure history", "Observed BTC balance reductions across comparable disclosures."),
        _invalidation_item("Residual value damage", f"{decimal_plain(residual_change.quantize(Decimal('0.01')))}%", "structural deterioration", residual_damage, "multi-disclosure trend", "Change in the residual enterprise mNAV series."),
        _invalidation_item("BTC per ADSO deterioration", f"{sum(change < ZERO for change in btc_adso_changes[-2:])} DECLINES", "repeated deterioration", repeated_adso_deterioration, "multi-disclosure trend", "Latest comparable BTC per ADSO intervals."),
        _invalidation_item("Destructive refinancing", "DETECTED" if destructive_refinancing else "CLEAR", "permanent burden increase without compensation", destructive_refinancing, "financing history", "Compares funded obligations and reserve coverage with the prior disclosure."),
    ]
    any_invalidation = any(item["passed"] is True for item in invalidation_checks)
    strategic_checks = [
        _gate_item("Supportive valuation", f"{decimal_plain(fair_mnav)}x mNAV" if fair_mnav is not None else "0x mNAV", "supportive residual valuation", supportive_valuation, "dynamic valuation bridge", "Compares current MSTR price with the dynamic fair price and enterprise mNAV."),
        _gate_item("BTC per ADSO disclosures", decimal_plain(btc_per_adso) if btc_per_adso is not None else "0", "3 non-deteriorating updates", three_non_deteriorating, "corporate disclosure history", f"{disclosure_count} comparable observations are available."),
        _gate_item("Reserve coverage", decimal_plain(reserve_months) if reserve_months is not None else None, "> 18 months", reserve_months > Decimal("18") if reserve_months is not None else None, "decision engine fingerprint", "Strategic gate requires more than 18 months."),
        _gate_item("Preferred yields", f"{decimal_plain(preferred_cash_yield.quantize(Decimal('0.01')))}%", "normalizing", preferred_normalizing, "preferred cash burden", "Blended annual preferred cash burden divided by preferred notional."),
        _gate_item("Invalidation", "TRIGGERED" if any_invalidation else "CLEAR", "no invalidation trigger", not any_invalidation, "invalidation monitor", "Aggregates every modeled invalidation trigger."),
    ]
    starter_passed = sum(item["passed"] is True for item in starter_checks)
    strategic_passed = sum(item["passed"] is True for item in strategic_checks)
    required_values = [price, reserve_months, btc_holdings, basic_shares, diluted_shares, risk_score, liquidity_score, accretion_score]
    available = sum(value is not None for value in required_values)
    confidence_score = Decimal(available) / Decimal(len(required_values)) * Decimal("10")
    confidence_reasons = [
        f"{available} of {len(required_values)} core current inputs are available",
        f"{disclosure_count} comparable corporate disclosure observations are available",
        f"preferred cash yield is {decimal_plain(preferred_cash_yield.quantize(Decimal('0.01')))}%",
    ]
    if any_invalidation:
        thesis_state = "INVALIDATED"
    elif available < len(required_values):
        thesis_state = "UNDER_REVIEW"
    elif risk_score is not None and risk_score >= Decimal("0.65"):
        thesis_state = "UNDER_REVIEW"
    else:
        thesis_state = "INTACT"
    action = str(engine.get("last_action") or "MONITOR").upper()
    action = {"SELL": "EXIT", "STRONG BUY": "STRONG_BUY"}.get(action, action.replace(" ", "_"))
    if any_invalidation:
        action = "REVIEW"
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source_commit_sha": source_commit_sha,
        "document": copy.deepcopy(THESIS_DOCUMENT),
        "original_rules": copy.deepcopy(THESIS_RULES),
        "current": {
            "thesis_state": thesis_state,
            "position_action": action,
            "data_confidence": {
                "score": decimal_plain(confidence_score.quantize(Decimal("0.1"))),
                "max": "10",
                "reasons": confidence_reasons,
            },
            "as_of": engine.get("updated_at_utc"),
            "freshness": market.freshness,
        },
        "valuation": {
            "mstr_price": decimal_plain(price) if price is not None else None,
            "btc_price": decimal_plain(market.btc_price) if market.btc_price is not None else None,
            "btc_holdings": decimal_plain(btc_holdings) if btc_holdings is not None else None,
            "basic_shares_m": decimal_plain(basic_shares) if basic_shares is not None else None,
            "adso_m": decimal_plain(diluted_shares) if diluted_shares is not None else None,
            "strong_buy_price": str(zones.get("strong_buy_price")) if zones.get("strong_buy_price") is not None else None,
            "accumulate_price": str(zones.get("accumulate_price")) if zones.get("accumulate_price") is not None else None,
            "fair_price": str(zones.get("fair_price")) if zones.get("fair_price") is not None else None,
            "hold_price": str(zones.get("hold_price")) if zones.get("hold_price") is not None else None,
            "reduce_price": str(zones.get("reduce_price")) if zones.get("reduce_price") is not None else None,
            "enterprise_mnav_estimate": str(zones.get("fair_ev_nav")) if zones.get("fair_ev_nav") is not None else None,
            "uncertainty_band": str(zones.get("uncertainty_band")) if zones.get("uncertainty_band") is not None else None,
        },
        "accretion": {
            "btc_per_basic_share": decimal_plain(btc_per_basic) if btc_per_basic is not None else None,
            "btc_per_adso": decimal_plain(btc_per_adso) if btc_per_adso is not None else None,
            "three_update_test": "pass" if three_non_deteriorating else "fail",
            "accretion_score": decimal_plain(accretion_score) if accretion_score is not None else None,
            "prior_btc_per_basic_share": decimal_plain(previous_btc_per_basic) if previous_btc_per_basic is not None else None,
            "prior_btc_per_adso": decimal_plain(previous_btc_per_adso) if previous_btc_per_adso is not None else None,
            "btc_per_basic_share_change_pct": decimal_plain(btc_per_basic_change) if btc_per_basic_change is not None else None,
            "btc_per_adso_change_pct": decimal_plain(btc_per_adso_change) if btc_per_adso_change is not None else None,
            "disclosure_observations": disclosure_count,
            "comparison_as_of": previous_observation.get("as_of") if previous_observation else None,
            "residual_adso_trend": disclosure_trend or "stable",
        },
        "liquidity": {
            "usd_reserve_b": str(fingerprint.get("usd_reserve_b")) if fingerprint.get("usd_reserve_b") is not None else None,
            "reserve_coverage_months": decimal_plain(reserve_months) if reserve_months is not None else None,
            "liquidity_score": decimal_plain(liquidity_score) if liquidity_score is not None else None,
            "debt_schedule": (
                _public_numeric_strings(fingerprint.get("debt_schedule"))
                if isinstance(fingerprint.get("debt_schedule"), (dict, list))
                else None
            ),
        },
        "financing": {
            "debt_b": str(fingerprint.get("debt_b")) if fingerprint.get("debt_b") is not None else None,
            "preferred_notional_b": str(fingerprint.get("preferred_b")) if fingerprint.get("preferred_b") is not None else None,
            "preferred_yields": decimal_plain(preferred_cash_yield.quantize(Decimal("0.01"))),
            "condition": financing_condition,
            "issuance_economics": "ACCRETIVE" if issuance_accretive else "DILUTIVE",
            "funding_stress": funding_stress,
        },
        "bitcoin_regime": {
            "state": bitcoin_regime,
            "change_pct": decimal_plain(bitcoin_change.quantize(Decimal("0.01"))),
        },
        "methodology": {
            "preferred_yield": "annual preferred cash burden divided by preferred notional",
            "funding_stress": "reserve coverage and liquidity score",
            "supportive_valuation": "market price at or below dynamic fair price with enterprise mNAV at or below 1.5x",
            "bitcoin_regime": "available challenge BTC history from first observation to current price",
        },
        "scores": {
            "risk": decimal_plain(risk_score) if risk_score is not None else None,
            "liquidity": decimal_plain(liquidity_score) if liquidity_score is not None else None,
            "accretion": decimal_plain(accretion_score) if accretion_score is not None else None,
        },
        "gates": {
            "starter": {
                "passed": starter_passed,
                "required": len(starter_checks),
                "eligible": all(item["passed"] is True for item in starter_checks),
                "checks": starter_checks,
            },
            "strategic": {
                "passed": strategic_passed,
                "required": len(strategic_checks),
                "eligible": all(item["passed"] is True for item in strategic_checks),
                "checks": strategic_checks,
            },
            "invalidation": {"triggered": any_invalidation, "checks": invalidation_checks},
        },
        "limitations": [
            "The original thesis snapshot is not presented as current corporate data.",
            "A public ledger proves recorded history, not a broker position.",
            "Corporate metrics use the latest available disclosure until a newer disclosure is recorded.",
        ],
    }


def _market_payload(market: MarketInputs) -> dict[str, Any]:
    return {
        "mstr_price": decimal_plain(market.mstr_price) if market.mstr_price is not None else None,
        "btc_price": decimal_plain(market.btc_price) if market.btc_price is not None else None,
        "usd_idr": decimal_plain(market.usd_idr) if market.usd_idr is not None else None,
        "market_status": market.market_status,
        "price_as_of": market.mstr_as_of,
        "btc_as_of": market.btc_as_of,
        "fx_as_of": market.fx_as_of,
        "fetched_at": market.fetched_at,
        "sources": {"MSTR": market.mstr_source, "BTC": market.btc_source, "USD_IDR": market.fx_source},
        "freshness": market.freshness,
        "warnings": list(market.warnings),
    }


def _portfolio_payload(config: Mapping[str, Any], state: ChallengeState, market: MarketInputs) -> dict[str, Any]:
    if config["status"] == "prelaunch" or not state.initialized:
        return {
            "setup_required": True,
            "cash_usd": None,
            "cash_idr": None,
            "cash_idr_equivalent": None,
            "mstr_quantity": None,
            "mstr_average_cost": None,
            "mstr_cost_basis": None,
            "mstr_market_value": None,
            "total_portfolio_usd": None,
            "total_portfolio_idr": None,
            "unrealized_pl_usd": None,
            "realized_pl_usd": None,
            "absolute_pl_usd": None,
            "total_return_pct": None,
            "net_contributions_usd": None,
            "cash_allocation_pct": None,
            "mstr_allocation_pct": None,
        }
    cash_idr_usd = state.cash["IDR"] / market.usd_idr if market.usd_idr is not None else None
    mstr_market_value = state.position.quantity * market.mstr_price if market.mstr_price is not None else None
    mstr_cost_basis = state.position.quantity * state.position.average_cost
    total = None
    if mstr_market_value is not None and (state.cash["IDR"] == ZERO or cash_idr_usd is not None):
        total = state.cash["USD"] + (cash_idr_usd or ZERO) + mstr_market_value
    unrealized = mstr_market_value - mstr_cost_basis if mstr_market_value is not None else None
    net = state.net_contributions_usd if state.contribution_data_complete else None
    absolute_pl = total - net if total is not None and net is not None else None
    total_return = absolute_pl / net * HUNDRED if absolute_pl is not None and net and net > ZERO else None
    cash_value = state.cash["USD"] + (cash_idr_usd or ZERO) if state.cash["IDR"] == ZERO or cash_idr_usd is not None else None
    cash_pct = cash_value / total * HUNDRED if cash_value is not None and total and total > ZERO else None
    mstr_pct = mstr_market_value / total * HUNDRED if mstr_market_value is not None and total and total > ZERO else None
    return {
        "setup_required": False,
        "cash_usd": decimal_plain(state.cash["USD"]),
        "cash_idr": decimal_plain(state.cash["IDR"]),
        "cash_idr_equivalent": decimal_plain(cash_idr_usd) if cash_idr_usd is not None else None,
        "mstr_quantity": decimal_plain(state.position.quantity),
        "mstr_average_cost": decimal_plain(state.position.average_cost),
        "mstr_cost_basis": decimal_plain(mstr_cost_basis),
        "mstr_market_value": decimal_plain(mstr_market_value) if mstr_market_value is not None else None,
        "total_portfolio_usd": decimal_plain(total) if total is not None else None,
        "total_portfolio_idr": decimal_plain(total * market.usd_idr) if total is not None and market.usd_idr is not None else None,
        "unrealized_pl_usd": decimal_plain(unrealized) if unrealized is not None else None,
        "realized_pl_usd": decimal_plain(state.realized_pl_usd),
        "absolute_pl_usd": decimal_plain(absolute_pl) if absolute_pl is not None else None,
        "total_return_pct": decimal_plain(total_return) if total_return is not None else None,
        "net_contributions_usd": decimal_plain(net) if net is not None else None,
        "cash_allocation_pct": decimal_plain(cash_pct) if cash_pct is not None else None,
        "mstr_allocation_pct": decimal_plain(mstr_pct) if mstr_pct is not None else None,
    }


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(child)
            for key, child in value.items()
            if key not in {"generated_at", "exported_at", "last_updated_at"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def write_public_if_changed(path: Path, payload: Mapping[str, Any]) -> bool:
    validate_public_payload(payload)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing is not None and _strip_volatile(existing) == _strip_volatile(payload):
            return False
    atomic_write_json(path, payload)
    return True


def _walk_public(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_PUBLIC_KEYS or lowered.startswith("telegram_") or lowered.endswith("_token"):
                raise ChallengeIntegrityError(f"Forbidden public key at {path}.{key}")
            _walk_public(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_public(child, f"{path}[{index}]")
    elif isinstance(value, float):
        raise ChallengeIntegrityError(f"Public financial payload contains float at {path}")


def validate_public_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != PUBLIC_SCHEMA_VERSION:
        raise ChallengeIntegrityError("Public payload schema_version is invalid")
    _walk_public(payload)
    serialized = canonical_json(payload).lower()
    for forbidden in FORBIDDEN_PUBLIC_TEXT:
        if forbidden in serialized:
            raise ChallengeIntegrityError(f"Public payload contains forbidden text: {forbidden}")


def export_public(
    base_dir: Path = Path("."),
    *,
    generated_at: datetime | None = None,
    source_commit_sha: str | None = None,
    market: MarketInputs | None = None,
    create_market_snapshot: bool = True,
) -> dict[str, Any]:
    ensure_challenge_files(base_dir)
    current = ensure_aware_utc(generated_at or now_utc())
    generated = iso_seconds(current)
    source_commit_sha = source_commit_sha or os.environ.get("PUBLIC_SOURCE_COMMIT_SHA") or None
    config = load_config(base_dir)
    events = read_events(base_dir)
    state = replay_events(events)
    engine = _load_engine_state(base_dir)
    record_engine_disclosure(base_dir, engine)
    market = market or load_market_inputs(base_dir, at=current)
    if create_market_snapshot and config["status"] != "prelaunch":
        create_snapshot(base_dir, market=market, captured_at=current)
    snapshots = read_snapshots(base_dir)
    undone = active_target_ids(events)
    digest = public_ledger_hash(events)
    exported_events = public_events(events, source_commit_sha)
    portfolio = _portfolio_payload(config, state, market)
    overview = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated,
        "challenge": {
            "id": config["challenge_id"],
            "name": config["name"],
            "status": config["status"],
            "start_at": config["start_at_utc"],
            "last_updated_at": generated,
            "include_legacy_position": config["include_legacy_position"],
        },
        "portfolio": portfolio,
        "market": _market_payload(market),
        "audit": {
            "source_commit_sha": source_commit_sha,
            "ledger_hash": digest,
            "generated_at": generated,
        },
        "disclaimer": "Public ledger history is not broker verification and is not investment advice.",
    }
    transactions = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated,
        "challenge_id": config["challenge_id"],
        "count": len(exported_events),
        "events": exported_events,
    }
    performance = build_performance_export(config, events, snapshots, generated_at=generated)
    thesis = build_thesis_export(
        base_dir,
        market,
        generated_at=generated,
        source_commit_sha=source_commit_sha,
    )
    audit = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated,
        "challenge_id": config["challenge_id"],
        "source_commit_sha": source_commit_sha,
        "ledger_hash": digest,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "active_event_count": len([item for item in events if item["event_id"] not in undone and item["event_type"] != "UNDO"]),
        "undone_event_count": len(undone),
        "latest_transaction_id": exported_events[-1]["event_id"] if exported_events else None,
        "source_status": "configured" if source_commit_sha else "source_commit_unavailable",
        "market_data_health": market.freshness,
        "jisdor_health": "available" if market.usd_idr is not None else "unavailable",
        "workflow": {"latest_success": None, "status": "not_exported"},
        "verification_scope": "The digest proves the exported recording history, not a broker position.",
    }
    health_status = "setup_required" if config["status"] == "prelaunch" else "ok"
    if config["status"] != "prelaunch" and market.freshness in {"stale", "unavailable"}:
        health_status = "degraded"
    health = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": generated,
        "ok": health_status in {"ok", "setup_required"},
        "status": health_status,
        "challenge_status": config["status"],
        "setup_required": config["status"] == "prelaunch",
        "ledger_valid": True,
        "public_export_valid": True,
        "market_freshness": market.freshness,
        "warnings": list(market.warnings),
    }
    payloads = {
        "overview": overview,
        "transactions": transactions,
        "performance": performance,
        "thesis": thesis,
        "audit": audit,
        "health": health,
    }
    changed = []
    for name, payload in payloads.items():
        if write_public_if_changed(public_path(base_dir, name), payload):
            changed.append(str(PUBLIC_FILES[name]))
    return {"changed": changed, "payloads": payloads}


def validate_all(base_dir: Path = Path("."), *, require_public: bool = False) -> None:
    config = load_config(base_dir)
    events = read_events(base_dir)
    state = replay_events(events)
    snapshots = read_snapshots(base_dir)
    if config["status"] == "prelaunch" and state.initialized:
        reset_seen = any(event["event_type"] == "RESET" and event["event_id"] not in active_target_ids(events) for event in events)
        if not reset_seen:
            raise ChallengeIntegrityError("Prelaunch config conflicts with an initialized ledger")
    if config["starting_event_id"] is not None and not any(
        event["event_id"] == config["starting_event_id"] and event["event_type"] == "CHALLENGE_INIT"
        for event in events
    ):
        raise ChallengeIntegrityError("starting_event_id is missing from the ledger")
    for snapshot in snapshots:
        validate_snapshot(snapshot)
    if require_public:
        for name in PUBLIC_FILES:
            path = public_path(base_dir, name)
            if not path.exists():
                raise ChallengeIntegrityError(f"Missing public export: {path}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ChallengeIntegrityError(f"Invalid public JSON: {path}") from exc
            if not isinstance(payload, dict):
                raise ChallengeIntegrityError(f"Public export must be an object: {path}")
            validate_public_payload(payload)
