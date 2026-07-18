from __future__ import annotations

import argparse
import calendar
import copy
import difflib
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

import challenge as mstr_challenge


getcontext().prec = 40

SCHEMA_VERSION = 1
DATA_DIR = Path("data")
LEDGER_FILE = DATA_DIR / "transactions.jsonl"
STATE_FILE = DATA_DIR / "portfolio_state.json"
SNAPSHOT_FILE = DATA_DIR / "portfolio_snapshots.jsonl"
MSTR_ENGINE_STATE_FILE = Path("mstr_decision_engine_v2_state.json")
WIB = ZoneInfo("Asia/Jakarta")
UTC = timezone.utc
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
BI_JISDOR_URL = "https://www.bi.go.id/biwebservice/wskursbi.asmx/getSubKursJisdor1"
REPORT_MAX_CHARS = 3900
HTTP_TIMEOUT = 20
TELEGRAM_TIMEOUT = 20
WEB_SYNC_TIMEOUT = 30
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")
IDR_UNIT = Decimal("1")
THRESHOLDS = [
    Decimal("-50"),
    Decimal("-25"),
    Decimal("50"),
    Decimal("100"),
    Decimal("200"),
    Decimal("500"),
    Decimal("1000"),
    Decimal("2000"),
    Decimal("5000"),
    Decimal("10000"),
]
SUPPORTED_THRESHOLD_STRINGS = {str(value) for value in THRESHOLDS}
VALID_MSTR_ZONES = {"STRONG BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL"}


class PortfolioError(RuntimeError):
    """Base class for portfolio failures."""


class PortfolioValidationError(PortfolioError):
    """Raised for user input or command validation failures."""


class DataIntegrityError(PortfolioError):
    """Raised when persisted data is corrupt or replay-invalid."""


class MarketDataError(PortfolioError):
    """Raised when market data is unavailable or malformed."""


class TelegramError(PortfolioError):
    """Raised when Telegram API calls fail."""


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    max_quantity_decimals: int
    unit_label: str


@dataclass
class Position:
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO

    def copy(self) -> "Position":
        return Position(self.quantity, self.average_cost)


@dataclass(frozen=True)
class ReplayResult:
    positions: dict[str, Position]
    active_events: list[dict[str, Any]]
    undone_event_ids: set[str]


@dataclass(frozen=True)
class MarketData:
    prices: dict[str, Decimal]
    as_of: dict[str, str | None]
    source: str
    fresh: bool
    stale_assets: set[str]
    warnings: list[str]


@dataclass(frozen=True)
class JisdorData:
    rate: Decimal | None
    official_date: str | None
    fresh: bool
    warning: str | None = None


@dataclass(frozen=True)
class CashFlow:
    timestamp_utc: datetime
    amount: Decimal


ASSETS: dict[str, AssetSpec] = {
    "MSTR": AssetSpec("MSTR", 6, "lembar"),
    "BTC": AssetSpec("BTC", 8, "BTC"),
}
COMMANDS = {
    "buy",
    "buy_mstr",
    "buy_btc",
    "sell",
    "sell_mstr",
    "sell_btc",
    "portofolio",
    "last",
    "history",
    "undo",
    "clear_all",
    "help",
    "challenge_status",
    "challenge_init",
    "challenge_reset",
    "cash",
    "deposit",
    "withdraw",
    "fx_convert",
    "fee",
    "tax",
}
COMMAND_MENU = [
    ("buy", "Panduan catat pembelian"),
    ("buy_mstr", "Catat beli MSTR"),
    ("buy_btc", "Catat beli BTC"),
    ("sell", "Panduan catat penjualan"),
    ("sell_mstr", "Catat jual MSTR"),
    ("sell_btc", "Catat jual BTC"),
    ("portofolio", "Lihat portofolio"),
    ("last", "Lihat 5 transaksi terakhir"),
    ("history", "Lihat riwayat transaksi"),
    ("undo", "Batalkan transaksi aktif"),
    ("clear_all", "Kosongkan posisi aktif"),
    ("challenge_status", "Status MSTR live challenge"),
    ("challenge_init", "Mulai challenge dari nol"),
    ("cash", "Lihat cash challenge"),
    ("deposit", "Catat setoran challenge"),
    ("withdraw", "Catat penarikan challenge"),
    ("fx_convert", "Catat konversi FX aktual"),
    ("fee", "Catat biaya challenge"),
    ("tax", "Catat pajak challenge"),
    ("help", "Bantuan perintah"),
]
CHALLENGE_COMMANDS = {
    "challenge_status",
    "challenge_init",
    "challenge_reset",
    "cash",
    "deposit",
    "withdraw",
    "fx_convert",
    "fee",
    "tax",
}
NUMERIC_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
STORED_DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
EVENT_ID_RE = re.compile(r"^(TX|RESET|UNDO)-([0-9]{6})$")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_update_id": 0,
        "market_cache": {},
        "jisdor_cache": {},
        "alert_state": {
            "MSTR": {"positive": [], "negative": []},
            "BTC": {"positive": [], "negative": []},
            "PORTFOLIO": {"positive": [], "negative": []},
            "mstr_zone": None,
        },
        "alert_baseline_pending": False,
        "last_daily_report_date_wib": None,
        "bot_commands_registered": False,
        "bot_commands_fingerprint": "",
        "outbox": [],
    }


def data_path(base_dir: Path, relative: Path) -> Path:
    return base_dir / relative


def ledger_path(base_dir: Path) -> Path:
    return data_path(base_dir, LEDGER_FILE)


def state_path(base_dir: Path) -> Path:
    return data_path(base_dir, STATE_FILE)


def snapshot_path(base_dir: Path) -> Path:
    return data_path(base_dir, SNAPSHOT_FILE)


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def wib_now() -> datetime:
    return now_utc().astimezone(WIB)


def to_wib(value: datetime) -> datetime:
    return ensure_aware_utc(value).astimezone(WIB)


def iso_seconds(value: datetime) -> str:
    return ensure_aware_utc(value).isoformat(timespec="seconds")


def wib_iso_seconds(value: datetime) -> str:
    return to_wib(value).isoformat(timespec="seconds")


def parse_iso_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DataIntegrityError(f"{field_name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataIntegrityError(f"{field_name} is not a valid ISO datetime: {value!r}") from exc
    return ensure_aware_utc(parsed)


def parse_offset_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DataIntegrityError(f"{field_name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataIntegrityError(f"{field_name} is not a valid ISO datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DataIntegrityError(f"{field_name} must include a timezone offset")
    return parsed


def parse_wib_date(value: Any, field_name: str = "date_wib") -> date:
    if not isinstance(value, str):
        raise DataIntegrityError(f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataIntegrityError(f"{field_name} must be YYYY-MM-DD: {value!r}") from exc


def decimal_plain(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def decimal_scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if exponent < 0 else 0


def parse_user_decimal(text: str, *, max_decimals: int, field_name: str) -> Decimal:
    if "," in text:
        raise PortfolioValidationError(f"{field_name} tidak boleh memakai koma. Gunakan titik desimal.")
    if "e" in text.lower():
        raise PortfolioValidationError(f"{field_name} tidak boleh memakai notasi ilmiah.")
    if not NUMERIC_RE.match(text):
        raise PortfolioValidationError(f"{field_name} tidak valid.")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise PortfolioValidationError(f"{field_name} tidak valid.") from exc
    if not value.is_finite() or value <= ZERO:
        raise PortfolioValidationError(f"{field_name} harus lebih besar dari nol.")
    if decimal_scale(value) > max_decimals:
        raise PortfolioValidationError(f"{field_name} maksimal {max_decimals} angka desimal.")
    return value


def parse_stored_decimal(value: Any, field_name: str, *, allow_negative: bool = True) -> Decimal:
    if not isinstance(value, str) or not STORED_DECIMAL_RE.match(value):
        raise DataIntegrityError(f"{field_name} must be a plain JSON decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise DataIntegrityError(f"{field_name} is not a valid Decimal string") from exc
    if not decimal_value.is_finite():
        raise DataIntegrityError(f"{field_name} must be finite")
    if not allow_negative and decimal_value < ZERO:
        raise DataIntegrityError(f"{field_name} must not be negative")
    return decimal_value


def parse_quantity(asset: str, text: str) -> Decimal:
    spec = ASSETS.get(asset.upper())
    if spec is None:
        raise PortfolioValidationError(f"Aset {asset.upper()} belum didukung.")
    return parse_user_decimal(text, max_decimals=spec.max_quantity_decimals, field_name="Jumlah")


def parse_price(text: str) -> Decimal:
    return parse_user_decimal(text, max_decimals=8, field_name="Harga")


def fmt_usd(value: Decimal) -> str:
    quantized = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return f"${quantized:,.2f}"


def fmt_usd_signed(value: Decimal) -> str:
    prefix = "+" if value > ZERO else ""
    return f"{prefix}{fmt_usd(value)}"


def fmt_idr(value: Decimal) -> str:
    quantized = value.quantize(IDR_UNIT, rounding=ROUND_HALF_UP)
    return "Rp" + f"{int(quantized):,}".replace(",", ".")


def fmt_pct(value: Decimal, *, signed: bool = True) -> str:
    quantized = value.quantize(CENT, rounding=ROUND_HALF_UP)
    prefix = "+" if signed and quantized > ZERO else ""
    return f"{prefix}{quantized:.2f}%"


def fmt_price_plain(value: Decimal) -> str:
    return fmt_usd(value)


def fmt_quantity(asset: str, value: Decimal, *, trim: bool = True) -> str:
    places = ASSETS[asset].max_quantity_decimals
    quantum = Decimal(1).scaleb(-places)
    text = format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")
    if trim and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def safe_append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_data_files(base_dir: Path = Path(".")) -> None:
    data_dir = data_path(base_dir, DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    if not ledger_path(base_dir).exists():
        ledger_path(base_dir).write_text("", encoding="utf-8")
    if not snapshot_path(base_dir).exists():
        snapshot_path(base_dir).write_text("", encoding="utf-8")
    if not state_path(base_dir).exists():
        atomic_write_json(state_path(base_dir), default_state())


def fill_missing_defaults(value: dict[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(value)
    for key, default_value in defaults.items():
        if key not in merged:
            merged[key] = copy.deepcopy(default_value)
            continue
        if isinstance(default_value, dict) and isinstance(merged[key], dict):
            merged[key] = fill_missing_defaults(merged[key], default_value)
    return merged


def load_state(base_dir: Path = Path(".")) -> dict[str, Any]:
    ensure_data_files(base_dir)
    path = state_path(base_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataIntegrityError(f"{path} contains malformed JSON") from exc
    if not isinstance(raw, dict):
        raise DataIntegrityError(f"{path} must contain a JSON object")
    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, int):
        raise DataIntegrityError("portfolio_state.json schema_version must be an integer")
    if schema_version > SCHEMA_VERSION:
        raise DataIntegrityError(f"Unsupported future state schema_version: {schema_version}")
    state = fill_missing_defaults(raw, default_state())
    validate_state_shape(state)
    return state


def require_positive_decimal_string(value: Any, field_name: str) -> Decimal:
    decimal_value = parse_stored_decimal(value, field_name, allow_negative=False)
    if decimal_value <= ZERO:
        raise DataIntegrityError(f"{field_name} must be positive")
    return decimal_value


def validate_state_shape(state: Mapping[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise DataIntegrityError("portfolio_state.json schema_version must be 1")
    if not isinstance(state.get("last_update_id"), int) or state["last_update_id"] < 0:
        raise DataIntegrityError("last_update_id must be a non-negative integer")
    if not isinstance(state.get("bot_commands_registered"), bool):
        raise DataIntegrityError("bot_commands_registered must be a boolean")
    command_fingerprint = state.get("bot_commands_fingerprint")
    if not isinstance(command_fingerprint, str):
        raise DataIntegrityError("bot_commands_fingerprint must be a string")
    if command_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", command_fingerprint):
        raise DataIntegrityError("bot_commands_fingerprint must be empty or a SHA-256 digest")
    if not isinstance(state.get("alert_baseline_pending"), bool):
        raise DataIntegrityError("alert_baseline_pending must be a boolean")
    last_daily = state.get("last_daily_report_date_wib")
    if last_daily is not None:
        parse_wib_date(last_daily, "last_daily_report_date_wib")
    if not isinstance(state.get("market_cache"), dict):
        raise DataIntegrityError("market_cache must be an object")
    if not isinstance(state.get("jisdor_cache"), dict):
        raise DataIntegrityError("jisdor_cache must be an object")
    if not isinstance(state.get("outbox"), list):
        raise DataIntegrityError("outbox must be a list")
    alert_state = state.get("alert_state")
    if not isinstance(alert_state, dict):
        raise DataIntegrityError("alert_state must be an object")
    for key in ("MSTR", "BTC", "PORTFOLIO"):
        item = alert_state.get(key)
        if not isinstance(item, dict):
            raise DataIntegrityError(f"alert_state.{key} must be an object")
        for side in ("positive", "negative"):
            values = item.get(side)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise DataIntegrityError(f"alert_state.{key}.{side} must be a list of strings")
            invalid = [value for value in values if value not in SUPPORTED_THRESHOLD_STRINGS]
            if invalid:
                raise DataIntegrityError(f"alert_state.{key}.{side} contains unsupported thresholds: {invalid}")
    mstr_zone = alert_state.get("mstr_zone")
    if mstr_zone is not None and mstr_zone not in VALID_MSTR_ZONES:
        raise DataIntegrityError("alert_state.mstr_zone must be null or a valid MSTR zone")
    for asset, item in state.get("market_cache", {}).items():
        if asset in ASSETS:
            if not isinstance(item, dict):
                raise DataIntegrityError(f"market_cache.{asset} must be an object")
            if "price_usd" in item:
                require_positive_decimal_string(item["price_usd"], f"market_cache.{asset}.price_usd")
    if isinstance(state.get("jisdor_cache"), dict) and state["jisdor_cache"].get("rate"):
        require_positive_decimal_string(state["jisdor_cache"]["rate"], "jisdor_cache.rate")
    outbox_ids: set[str] = set()
    for item in state.get("outbox", []):
        if not isinstance(item, dict):
            raise DataIntegrityError("outbox items must be objects")
        for field in ("id", "chat_id", "text", "created_at_utc", "category"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise DataIntegrityError(f"outbox item missing string field {field}")
        if item["id"] in outbox_ids:
            raise DataIntegrityError(f"duplicate outbox id: {item['id']}")
        outbox_ids.add(item["id"])
        parse_iso_datetime(item["created_at_utc"], "outbox.created_at_utc")


def state_has_disallowed_secret_keys(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = str(key).lower()
            if "bot_token" in key_lower or key_lower == "token" or "telegram_bot_token" in key_lower:
                return True
            if state_has_disallowed_secret_keys(child):
                return True
    elif isinstance(value, list):
        return any(state_has_disallowed_secret_keys(item) for item in value)
    return False


def save_state(base_dir: Path, state: Mapping[str, Any]) -> None:
    validate_state_shape(state)
    if state_has_disallowed_secret_keys(state):
        raise DataIntegrityError("Refusing to write state containing Telegram token fields")
    atomic_write_json(state_path(base_dir), state)


def validate_event_id(event_id: str, expected_prefix: str | None = None) -> None:
    match = EVENT_ID_RE.match(event_id)
    if not match:
        raise DataIntegrityError(f"Invalid event_id: {event_id!r}")
    if expected_prefix is not None and match.group(1) != expected_prefix:
        raise DataIntegrityError(f"event_id {event_id!r} must start with {expected_prefix}-")


def validate_event(event: Mapping[str, Any], *, line_no: int | None = None) -> None:
    location = f" on line {line_no}" if line_no is not None else ""
    if event.get("schema_version") != SCHEMA_VERSION:
        raise DataIntegrityError(f"Invalid event schema_version{location}")
    event_id = event.get("event_id")
    if not isinstance(event_id, str):
        raise DataIntegrityError(f"event_id must be a string{location}")
    event_type = event.get("event_type")
    if event_type not in {"BUY", "SELL", "RESET", "UNDO"}:
        raise DataIntegrityError(f"Invalid event_type{location}: {event_type!r}")
    expected_prefix = "TX" if event_type in {"BUY", "SELL"} else event_type
    validate_event_id(event_id, expected_prefix)
    timestamp_utc = parse_offset_datetime(event.get("timestamp_utc"), "timestamp_utc")
    timestamp_wib = parse_offset_datetime(event.get("timestamp_wib"), "timestamp_wib")
    if timestamp_wib.utcoffset() != timedelta(hours=7):
        raise DataIntegrityError(f"timestamp_wib must use +07:00 offset{location}")
    if timestamp_utc.astimezone(UTC) != timestamp_wib.astimezone(UTC):
        raise DataIntegrityError(f"timestamp_wib must represent the same instant as timestamp_utc{location}")
    telegram_update_id = event.get("telegram_update_id")
    if telegram_update_id is not None and (not isinstance(telegram_update_id, int) or telegram_update_id < 0):
        raise DataIntegrityError(f"telegram_update_id must be null or a non-negative integer{location}")
    telegram_message_id = event.get("telegram_message_id")
    if telegram_message_id is not None and not isinstance(telegram_message_id, int):
        raise DataIntegrityError(f"telegram_message_id must be null or an integer{location}")
    chat_id = event.get("chat_id")
    if chat_id is not None and (not isinstance(chat_id, str) or not chat_id):
        raise DataIntegrityError(f"chat_id must be null or a non-empty string{location}")
    if event_type in {"BUY", "SELL"}:
        asset = event.get("asset")
        if asset not in ASSETS:
            raise DataIntegrityError(f"Unsupported asset in ledger{location}: {asset!r}")
        quantity = parse_stored_decimal(event.get("quantity"), "quantity", allow_negative=False)
        price = parse_stored_decimal(event.get("price_usd"), "price_usd", allow_negative=False)
        if quantity <= ZERO or price <= ZERO:
            raise DataIntegrityError(f"Ledger quantity and price must be positive{location}")
        if decimal_scale(quantity) > ASSETS[asset].max_quantity_decimals:
            raise DataIntegrityError(f"Ledger quantity exceeds precision for {asset}{location}")
        if decimal_scale(price) > 8:
            raise DataIntegrityError(f"Ledger price exceeds precision{location}")
        if event.get("target_event_id") is not None:
            raise DataIntegrityError(f"BUY/SELL target_event_id must be null{location}")
    elif event_type == "RESET":
        if event.get("asset") is not None or event.get("quantity") is not None or event.get("price_usd") is not None:
            raise DataIntegrityError(f"RESET asset/quantity/price must be null{location}")
        if event.get("target_event_id") is not None:
            raise DataIntegrityError(f"RESET target_event_id must be null{location}")
    elif event_type == "UNDO":
        target = event.get("target_event_id")
        if not isinstance(target, str):
            raise DataIntegrityError(f"UNDO target_event_id must be a string{location}")
        validate_event_id(target)
        if event.get("asset") is not None or event.get("quantity") is not None or event.get("price_usd") is not None:
            raise DataIntegrityError(f"UNDO asset/quantity/price must be null{location}")


def read_ledger(base_dir: Path = Path(".")) -> list[dict[str, Any]]:
    ensure_data_files(base_dir)
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(ledger_path(base_dir).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataIntegrityError(f"Malformed JSONL ledger line {index}") from exc
        if not isinstance(event, dict):
            raise DataIntegrityError(f"Ledger line {index} must be a JSON object")
        validate_event(event, line_no=index)
        event_id = event["event_id"]
        if event_id in seen:
            raise DataIntegrityError(f"Duplicate event_id in ledger: {event_id}")
        seen.add(event_id)
        events.append(event)
    validate_undo_references(events)
    validate_unique_telegram_update_ids(events)
    return events


def validate_unique_telegram_update_ids(events: Sequence[Mapping[str, Any]]) -> None:
    seen: dict[int, str] = {}
    for event in events:
        update_id = event.get("telegram_update_id")
        if update_id is None:
            continue
        if update_id in seen:
            raise DataIntegrityError(f"Duplicate telegram_update_id {update_id} in {seen[update_id]} and {event['event_id']}")
        seen[update_id] = event["event_id"]


def validate_undo_references(events: Sequence[Mapping[str, Any]]) -> None:
    by_id: dict[str, tuple[int, Mapping[str, Any]]] = {event["event_id"]: (index, event) for index, event in enumerate(events)}
    undone_targets: set[str] = set()
    for index, event in enumerate(events):
        if event["event_type"] != "UNDO":
            continue
        target_id = event["target_event_id"]
        if target_id not in by_id:
            raise DataIntegrityError(f"UNDO target does not exist: {target_id}")
        target_index, target = by_id[target_id]
        if target_index >= index:
            raise DataIntegrityError(f"UNDO target must appear before UNDO event: {target_id}")
        if target["event_type"] == "UNDO":
            raise DataIntegrityError("UNDO of another UNDO is forbidden")
        if target_id in undone_targets:
            raise DataIntegrityError(f"Event was undone more than once: {target_id}")
        undone_targets.add(target_id)


def active_event_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        event["target_event_id"]
        for event in events
        if event["event_type"] == "UNDO"
    }


def replay_events(events: Sequence[Mapping[str, Any]], *, until_utc: datetime | None = None) -> ReplayResult:
    validate_undo_references(events)
    undone = active_event_ids(events)
    positions = {asset: Position() for asset in ASSETS}
    active_events: list[dict[str, Any]] = []
    cutoff = ensure_aware_utc(until_utc) if until_utc else None
    for event in events:
        if event["event_type"] == "UNDO" or event["event_id"] in undone:
            continue
        event_time = parse_iso_datetime(event["timestamp_utc"], "timestamp_utc")
        if cutoff is not None and event_time > cutoff:
            continue
        event_type = event["event_type"]
        active_events.append(dict(event))
        if event_type == "RESET":
            positions = {asset: Position() for asset in ASSETS}
            continue
        asset = event["asset"]
        quantity = parse_stored_decimal(event["quantity"], "quantity", allow_negative=False)
        price = parse_stored_decimal(event["price_usd"], "price_usd", allow_negative=False)
        position = positions[asset]
        if event_type == "BUY":
            new_quantity = position.quantity + quantity
            new_average = ((position.quantity * position.average_cost) + (quantity * price)) / new_quantity
            positions[asset] = Position(new_quantity, new_average)
        elif event_type == "SELL":
            if quantity > position.quantity:
                raise DataIntegrityError(f"SELL {event['event_id']} oversells {asset}")
            remaining = position.quantity - quantity
            positions[asset] = Position(remaining, ZERO if remaining == ZERO else position.average_cost)
    return ReplayResult(positions, active_events, set(undone))


def append_event(base_dir: Path, event: Mapping[str, Any]) -> None:
    validate_event(event)
    safe_append_jsonl(ledger_path(base_dir), event)


def next_sequence(events: Sequence[Mapping[str, Any]], prefix: str) -> int:
    max_seen = 0
    for event in events:
        event_id = event["event_id"]
        match = EVENT_ID_RE.match(event_id)
        if match and match.group(1) == prefix:
            max_seen = max(max_seen, int(match.group(2)))
    return max_seen + 1


def next_event_id(events: Sequence[Mapping[str, Any]], event_type: str) -> str:
    prefix = "TX" if event_type in {"BUY", "SELL"} else event_type
    return f"{prefix}-{next_sequence(events, prefix):06d}"


def make_event(
    events: Sequence[Mapping[str, Any]],
    event_type: str,
    *,
    asset: str | None,
    quantity: Decimal | None,
    price_usd: Decimal | None,
    target_event_id: str | None,
    telegram_update_id: int | None,
    telegram_message_id: int | None,
    chat_id: str | None,
    timestamp_utc: datetime,
) -> dict[str, Any]:
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": next_event_id(events, event_type),
        "event_type": event_type,
        "asset": asset,
        "quantity": decimal_plain(quantity) if quantity is not None else None,
        "price_usd": decimal_plain(price_usd) if price_usd is not None else None,
        "target_event_id": target_event_id,
        "telegram_update_id": telegram_update_id,
        "telegram_message_id": telegram_message_id,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "timestamp_utc": iso_seconds(timestamp_utc),
        "timestamp_wib": wib_iso_seconds(timestamp_utc),
    }
    validate_event(event)
    return event


def event_by_update_id(events: Sequence[Mapping[str, Any]], update_id: int | None) -> Mapping[str, Any] | None:
    if update_id is None:
        return None
    for event in events:
        if event.get("telegram_update_id") == update_id:
            return event
    return None


def position_is_empty(positions: Mapping[str, Position]) -> bool:
    return all(position.quantity == ZERO for position in positions.values())


def active_last_events(events: Sequence[Mapping[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    replay = replay_events(events)
    eligible = [event for event in replay.active_events if event["event_type"] in {"BUY", "SELL", "RESET"}]
    return list(reversed(eligible[-limit:]))


def format_event_summary_line(event: Mapping[str, Any]) -> str:
    if event["event_type"] == "RESET":
        return f"{event['event_id']} | CLEAR ALL"
    asset = event["asset"]
    quantity = fmt_quantity(asset, parse_stored_decimal(event["quantity"], "quantity", allow_negative=False))
    price = fmt_usd(parse_stored_decimal(event["price_usd"], "price_usd", allow_negative=False))
    return f"{event['event_id']} | {event['event_type']} {asset} | {quantity} @ {price}"


def format_last_events(events: Sequence[Mapping[str, Any]]) -> str:
    last = active_last_events(events)
    if not last:
        return "🧾 TRANSAKSI TERAKHIR\n\nBelum ada transaksi aktif."
    lines = ["🧾 TRANSAKSI TERAKHIR", ""]
    for event in last:
        lines.append(format_event_summary_line(event))
    return "\n".join(lines)


def format_history_events(events: Sequence[Mapping[str, Any]], limit: int = 20) -> str:
    history = active_last_events(events, limit)
    if not history:
        return "🧾 RIWAYAT TRANSAKSI\n\nBelum ada transaksi aktif."
    lines = [f"🧾 RIWAYAT TRANSAKSI AKTIF ({len(history)})", ""]
    for event in history:
        lines.append(format_event_summary_line(event))
    return "\n".join(lines)


def find_event_by_id(events: Sequence[Mapping[str, Any]], event_id: str) -> Mapping[str, Any] | None:
    for event in events:
        if event["event_id"] == event_id:
            return event
    return None


def active_undoable_event_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        event["event_id"]
        for event in replay_events(events).active_events
        if event["event_type"] in {"BUY", "SELL", "RESET"}
    }


def format_wib_human(timestamp_utc: datetime) -> str:
    return to_wib(timestamp_utc).strftime("%d %b %Y, %H:%M WIB")


def buy_guide() -> str:
    return "\n".join(
        [
            "🟢 CATAT PEMBELIAN",
            "",
            "Pilih aset:",
            "/buy_mstr JUMLAH HARGA",
            "/buy_btc JUMLAH HARGA",
            "",
            "Contoh:",
            "/buy_mstr 10 112.53",
            "/buy_btc 0.015 64250",
        ]
    )


def sell_guide() -> str:
    return "\n".join(
        [
            "🔴 CATAT PENJUALAN",
            "",
            "Pilih aset:",
            "/sell_mstr JUMLAH HARGA",
            "/sell_btc JUMLAH HARGA",
            "",
            "Contoh:",
            "/sell_mstr 10 112.53",
            "/sell_btc 0.015 64250",
        ]
    )


def command_usage(command: str) -> str:
    examples = {
        "buy_mstr": "/buy_mstr 10 112.53",
        "buy_btc": "/buy_btc 0.015 64250",
        "sell_mstr": "/sell_mstr 10 112.53",
        "sell_btc": "/sell_btc 0.015 64250",
        "undo": "/undo TX-000012",
        "history": "/history 20",
    }
    command_name = command.lower().lstrip("/")
    if command_name in examples:
        if command_name == "undo":
            usage = "/undo ID"
        elif command_name == "history":
            usage = "/history N"
        else:
            usage = f"/{command_name} JUMLAH HARGA"
        return f"Format:\n{usage}\n\nContoh:\n{examples[command_name]}"
    return help_text()


def help_text() -> str:
    return "\n".join(
        [
            "ℹ️ BANTUAN PORTOFOLIO",
            "",
            "/buy - panduan pembelian",
            "/buy_mstr JUMLAH HARGA",
            "/buy_btc JUMLAH HARGA",
            "/sell - panduan penjualan",
            "/sell_mstr JUMLAH HARGA",
            "/sell_btc JUMLAH HARGA",
            "/portofolio - lihat laporan",
            "/last - lima transaksi aktif terakhir",
            "/history - dua puluh transaksi aktif terakhir",
            "/history N - riwayat aktif, 1 sampai 100",
            "/undo ID - batalkan transaksi aktif",
            "/clear_all - panduan reset posisi",
            "/clear_all CONFIRM - reset posisi aktif",
            "",
            "MSTR LIVE THESIS CHALLENGE",
            "/challenge_status - status dan kesiapan challenge",
            "/challenge_init USD 1000 - mulai dari nol tanpa posisi lama",
            "/cash - saldo USD dan IDR challenge",
            "/deposit USD 100 - catat setoran",
            "/withdraw USD 50 - catat penarikan",
            "/fx_convert IDR 15000000 USD 830 - catat hasil FX aktual",
            "/fee USD 1.25 - catat biaya",
            "/tax USD 1.25 - catat pajak",
            "/challenge_reset CONFIRM - kembali ke prelaunch",
        ]
    )


def clear_all_warning() -> str:
    return "\n".join(
        [
            "⚠️ PERINGATAN",
            "",
            "Perintah ini akan mengosongkan seluruh posisi aktif MSTR dan BTC.",
            "",
            "Untuk melanjutkan, kirim:",
            "/clear_all CONFIRM",
            "",
            "Riwayat lama tetap disimpan secara internal.",
        ]
    )


def confirm_buy(event: Mapping[str, Any], positions: Mapping[str, Position]) -> str:
    asset = event["asset"]
    position = positions[asset]
    quantity = fmt_quantity(asset, parse_stored_decimal(event["quantity"], "quantity", allow_negative=False))
    price = fmt_usd(parse_stored_decimal(event["price_usd"], "price_usd", allow_negative=False))
    unit = "lembar" if asset == "MSTR" else "BTC"
    timestamp = format_wib_human(parse_iso_datetime(event["timestamp_utc"], "timestamp_utc"))
    position_text = f"{fmt_quantity(asset, position.quantity)} {unit}"
    buy_label = f"{quantity} {unit}" if asset == "MSTR" else f"{quantity} BTC"
    return "\n".join(
        [
            f"✅ BUY {asset} TERCATAT",
            "",
            f"Pembelian: {buy_label} @ {price}",
            f"Posisi {asset}: {position_text}",
            f"Rata-rata beli: {fmt_usd(position.average_cost)}",
            f"Waktu: {timestamp}",
            f"ID: {event['event_id']}",
        ]
    )


def confirm_sell(event: Mapping[str, Any], positions_before: Mapping[str, Position], positions_after: Mapping[str, Position]) -> str:
    asset = event["asset"]
    quantity = parse_stored_decimal(event["quantity"], "quantity", allow_negative=False)
    price = parse_stored_decimal(event["price_usd"], "price_usd", allow_negative=False)
    average_before = positions_before[asset].average_cost
    realized = quantity * (price - average_before)
    unit = "lembar" if asset == "MSTR" else "BTC"
    timestamp = format_wib_human(parse_iso_datetime(event["timestamp_utc"], "timestamp_utc"))
    return "\n".join(
        [
            f"✅ SELL {asset} TERCATAT",
            "",
            f"Terjual: {fmt_quantity(asset, quantity)} {unit} @ {fmt_usd(price)}",
            f"Sisa posisi: {fmt_quantity(asset, positions_after[asset].quantity)} {unit}",
            f"Rata-rata beli: {fmt_usd(positions_after[asset].average_cost)}",
            f"Realized P/L: {fmt_usd_signed(realized)}",
            f"Waktu: {timestamp}",
            f"ID: {event['event_id']}",
        ]
    )


def oversell_rejection(asset: str, available: Decimal, requested: Decimal) -> str:
    unit = "lembar" if asset == "MSTR" else "BTC"
    return "\n".join(
        [
            "❌ PENJUALAN DITOLAK",
            "",
            f"Posisi {asset} tersedia: {fmt_quantity(asset, available)} {unit}",
            f"Jumlah yang ingin dijual: {fmt_quantity(asset, requested)} {unit}",
            "",
            f"Gunakan jumlah maksimal {fmt_quantity(asset, available)} {unit}.",
        ]
    )


def duplicate_event_reply(events: Sequence[Mapping[str, Any]], duplicate: Mapping[str, Any]) -> str:
    event_type = duplicate["event_type"]
    event_id = duplicate["event_id"]
    positions = replay_events(events).positions
    if event_type in {"BUY", "SELL"}:
        asset = duplicate["asset"]
        unit = "lembar" if asset == "MSTR" else "BTC"
        quantity = parse_stored_decimal(duplicate["quantity"], "quantity", allow_negative=False)
        price = parse_stored_decimal(duplicate["price_usd"], "price_usd", allow_negative=False)
        return "\n".join(
            [
                "ℹ️ TRANSAKSI SUDAH TERCATAT",
                "",
                f"ID: {event_id}",
                f"{event_type} {asset}: {fmt_quantity(asset, quantity)} {unit} @ {fmt_usd(price)}",
                f"Posisi {asset} saat ini: {fmt_quantity(asset, positions[asset].quantity)} {unit}",
                "Tidak ada transaksi duplikat dibuat.",
            ]
        )
    if event_type == "RESET":
        return "\n".join(
            [
                "ℹ️ RESET SUDAH TERCATAT",
                "",
                f"ID: {event_id}",
                "Posisi aktif MSTR dan BTC sudah dikosongkan oleh event ini.",
                "Tidak ada RESET duplikat dibuat.",
            ]
        )
    if event_type == "UNDO":
        return "\n".join(
            [
                "ℹ️ UNDO SUDAH TERCATAT",
                "",
                f"ID: {event_id}",
                f"Event dibatalkan: {duplicate['target_event_id']}",
                "Tidak ada UNDO duplikat dibuat.",
            ]
        )
    return f"Update sudah pernah diproses sebagai {event_id}."


def process_trade(
    base_dir: Path,
    events: list[dict[str, Any]],
    *,
    event_type: str,
    asset: str,
    quantity: Decimal,
    price: Decimal,
    telegram_update_id: int | None,
    telegram_message_id: int | None,
    chat_id: str | None,
    timestamp_utc: datetime,
) -> tuple[dict[str, Any] | None, str, bool]:
    duplicate = event_by_update_id(events, telegram_update_id)
    if duplicate is not None:
        return None, duplicate_event_reply(events, duplicate), False
    if asset not in ASSETS:
        raise PortfolioValidationError(f"Aset {asset} belum didukung.")
    before = replay_events(events).positions
    if event_type == "SELL" and quantity > before[asset].quantity:
        return None, oversell_rejection(asset, before[asset].quantity, quantity), False
    event = make_event(
        events,
        event_type,
        asset=asset,
        quantity=quantity,
        price_usd=price,
        target_event_id=None,
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        timestamp_utc=timestamp_utc,
    )
    candidate = [*events, event]
    replayed = replay_events(candidate)
    append_event(base_dir, event)
    rebuild_snapshots(base_dir)
    if event_type == "BUY":
        return event, confirm_buy(event, replayed.positions), True
    return event, confirm_sell(event, before, replayed.positions), True


def process_reset(
    base_dir: Path,
    events: list[dict[str, Any]],
    *,
    telegram_update_id: int | None,
    telegram_message_id: int | None,
    chat_id: str | None,
    timestamp_utc: datetime,
) -> tuple[dict[str, Any] | None, str, bool]:
    duplicate = event_by_update_id(events, telegram_update_id)
    if duplicate is not None:
        return None, duplicate_event_reply(events, duplicate), False
    if position_is_empty(replay_events(events).positions):
        return None, "Portofolio sudah kosong. RESET baru tidak dibuat.", False
    event = make_event(
        events,
        "RESET",
        asset=None,
        quantity=None,
        price_usd=None,
        target_event_id=None,
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        timestamp_utc=timestamp_utc,
    )
    replay_events([*events, event])
    append_event(base_dir, event)
    rebuild_snapshots(base_dir)
    return event, f"✅ CLEAR ALL TERCATAT\n\nPosisi aktif MSTR dan BTC dikosongkan.\nID: {event['event_id']}", True


def process_undo(
    base_dir: Path,
    events: list[dict[str, Any]],
    *,
    target_event_id: str,
    telegram_update_id: int | None,
    telegram_message_id: int | None,
    chat_id: str | None,
    timestamp_utc: datetime,
) -> tuple[dict[str, Any] | None, str, bool]:
    duplicate = event_by_update_id(events, telegram_update_id)
    if duplicate is not None:
        return None, duplicate_event_reply(events, duplicate), False
    target = find_event_by_id(events, target_event_id)
    if target is None:
        return None, f"ID {target_event_id} tidak ditemukan.\nGunakan /history untuk melihat transaksi.", False
    if target["event_type"] not in {"BUY", "SELL", "RESET"} or target_event_id not in active_undoable_event_ids(events):
        return None, f"ID {target_event_id} sudah tidak aktif atau sudah pernah di-undo.\nGunakan /history untuk melihat transaksi aktif.", False
    event = make_event(
        events,
        "UNDO",
        asset=None,
        quantity=None,
        price_usd=None,
        target_event_id=target_event_id,
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        chat_id=chat_id,
        timestamp_utc=timestamp_utc,
    )
    try:
        replay_events([*events, event])
    except DataIntegrityError:
        return None, f"❌ UNDO DITOLAK\n\nMembatalkan {target_event_id} akan membuat transaksi setelahnya tidak valid.\nPortfolio tidak diubah.", False
    append_event(base_dir, event)
    rebuild_snapshots(base_dir)
    return event, f"✅ UNDO TERCATAT\n\nEvent dibatalkan: {target_event_id}\nID: {event['event_id']}", True


def parse_command_text(text: str) -> tuple[str | None, list[str]]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, []
    parts = stripped.split()
    command = parts[0][1:].split("@", 1)[0].lower()
    return command, parts[1:]


def unsupported_command_response(command: str) -> str:
    suggestion = difflib.get_close_matches(command, COMMANDS, n=1, cutoff=0.72)
    lines = [f"Perintah /{command} tidak dikenal."]
    if suggestion:
        lines.append(f"Mungkin maksud Anda /{suggestion[0]}.")
    lines.append("Ketik /help untuk melihat daftar perintah.")
    return "\n".join(lines)


def challenge_thesis_zone(base_dir: Path, market: mstr_challenge.MarketInputs) -> str | None:
    if market.mstr_price is None:
        return None
    payload, _ = load_mstr_engine_state(base_dir)
    if payload is None or not isinstance(payload.get("zones"), dict):
        return None
    return classify_mstr_zone(market.mstr_price, payload["zones"])


def should_use_challenge_surface(base_dir: Path, command: str, args: Sequence[str]) -> bool:
    config = mstr_challenge.load_config(base_dir)
    if command in CHALLENGE_COMMANDS:
        return True
    if config["status"] == "prelaunch":
        return False
    if command in {"buy_mstr", "sell_mstr", "portofolio", "history", "last"}:
        return True
    return command == "undo" and len(args) == 1 and bool(mstr_challenge.EVENT_ID_RE.fullmatch(args[0].upper()))


def handle_challenge_surface(
    base_dir: Path,
    command: str,
    args: Sequence[str],
    *,
    update_id: int,
    chat_id: str,
    message_id: int | None,
    message_timestamp_utc: datetime,
) -> tuple[str, bool]:
    effective_command = "history" if command == "last" else command
    effective_args = ["5"] if command == "last" else list(args)
    market = mstr_challenge.load_market_inputs(base_dir, at=message_timestamp_utc)
    return mstr_challenge.handle_challenge_command(
        base_dir,
        effective_command,
        effective_args,
        timestamp_utc=message_timestamp_utc,
        telegram_update_id=update_id,
        telegram_message_id=message_id,
        chat_id=chat_id,
        market=market,
        thesis_zone=challenge_thesis_zone(base_dir, market),
    )


def handle_authorized_text_command(
    base_dir: Path,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    update_id: int,
    chat_id: str,
    message_id: int | None,
    message_timestamp_utc: datetime,
    text: str,
) -> tuple[str | None, bool]:
    command, args = parse_command_text(text)
    if command is None:
        return None, False
    try:
        if should_use_challenge_surface(base_dir, command, args):
            return handle_challenge_surface(
                base_dir,
                command,
                args,
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                message_timestamp_utc=message_timestamp_utc,
            )
        if command == "buy":
            if args:
                return "Perintah /buy tidak menerima parameter.\n\n" + buy_guide(), False
            return buy_guide(), False
        if command == "sell":
            if args:
                return "Perintah /sell tidak menerima parameter.\n\n" + sell_guide(), False
            return sell_guide(), False
        if command in {"buy_mstr", "buy_btc", "sell_mstr", "sell_btc"}:
            if len(args) == 0:
                return command_usage(command), False
            if len(args) != 2:
                return "Parameter tidak valid atau berlebih.\n\n" + command_usage(command), False
            _, asset_suffix = command.split("_", 1)
            asset = asset_suffix.upper()
            quantity = parse_quantity(asset, args[0])
            price = parse_price(args[1])
            event_type = "BUY" if command.startswith("buy_") else "SELL"
            updated_events = read_ledger(base_dir)
            _, message, mutated = process_trade(
                base_dir,
                updated_events,
                event_type=event_type,
                asset=asset,
                quantity=quantity,
                price=price,
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                chat_id=chat_id,
                timestamp_utc=message_timestamp_utc,
            )
            return message, mutated
        if command == "portofolio":
            if args:
                return "Perintah /portofolio tidak menerima parameter.\nKetik /help untuk bantuan.", False
            return render_portfolio_report(base_dir, now=message_timestamp_utc), False
        if command == "last":
            if args:
                return "Perintah /last tidak menerima parameter.\nKetik /help untuk bantuan.", False
            return format_last_events(read_ledger(base_dir)), False
        if command == "history":
            if len(args) > 1:
                return "Parameter /history tidak valid.\n\n" + command_usage(command), False
            if not args:
                limit = 20
            else:
                if not args[0].isdigit():
                    return "Parameter /history harus angka 1 sampai 100.\n\n" + command_usage(command), False
                limit = int(args[0])
                if limit < 1 or limit > 100:
                    return "Parameter /history harus angka 1 sampai 100.\n\n" + command_usage(command), False
            return format_history_events(read_ledger(base_dir), limit), False
        if command == "undo":
            if len(args) == 0:
                return command_usage(command), False
            if len(args) != 1:
                return "Parameter /undo tidak valid.\n\n" + command_usage(command), False
            target = args[0].upper()
            if not EVENT_ID_RE.match(target):
                return "ID undo tidak valid. Gunakan format seperti TX-000012 atau RESET-000003.", False
            updated_events = read_ledger(base_dir)
            _, message, mutated = process_undo(
                base_dir,
                updated_events,
                target_event_id=target,
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                chat_id=chat_id,
                timestamp_utc=message_timestamp_utc,
            )
            return message, mutated
        if command == "clear_all":
            if len(args) == 0:
                return clear_all_warning(), False
            if args != ["CONFIRM"]:
                return "Konfirmasi tidak valid. Gunakan tepat:\n/clear_all CONFIRM", False
            updated_events = read_ledger(base_dir)
            _, message, mutated = process_reset(
                base_dir,
                updated_events,
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                chat_id=chat_id,
                timestamp_utc=message_timestamp_utc,
            )
            return message, mutated
        if command == "help":
            if args:
                return "Perintah /help tidak menerima parameter.\n\n" + help_text(), False
            return help_text(), False
        return unsupported_command_response(command), False
    except (PortfolioValidationError, mstr_challenge.ChallengeError) as exc:
        return f"{exc}\nKetik /help untuk bantuan.", False


def outbox_item_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"OUT-{digest}"


def enqueue_outbox(state: dict[str, Any], *, item_id: str, chat_id: str, text: str, category: str, created_at_utc: datetime) -> None:
    if not text:
        return
    outbox = state.setdefault("outbox", [])
    if any(item.get("id") == item_id for item in outbox):
        return
    outbox.append(
        {
            "id": item_id,
            "chat_id": str(chat_id),
            "text": text,
            "created_at_utc": iso_seconds(created_at_utc),
            "category": category,
        }
    )


def telegram_credentials() -> tuple[str | None, str | None]:
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def telegram_get(token: str, method: str, *, params: Mapping[str, Any]) -> Any:
    try:
        response = requests.get(TELEGRAM_API.format(token=token, method=method), params=params, timeout=TELEGRAM_TIMEOUT)
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram {method} request failed") from exc
    if response.status_code >= 400:
        raise TelegramError(f"Telegram {method} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram {method} returned non-JSON response") from exc
    if not payload.get("ok"):
        description = str(payload.get("description", "unknown error"))
        raise TelegramError(f"Telegram {method} failed: {description}")
    return payload.get("result")


def telegram_post(token: str, method: str, *, json_payload: Mapping[str, Any]) -> Any:
    try:
        response = requests.post(TELEGRAM_API.format(token=token, method=method), json=json_payload, timeout=TELEGRAM_TIMEOUT)
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram {method} request failed") from exc
    if response.status_code >= 400:
        raise TelegramError(f"Telegram {method} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram {method} returned non-JSON response") from exc
    if not payload.get("ok"):
        description = str(payload.get("description", "unknown error"))
        raise TelegramError(f"Telegram {method} failed: {description}")
    return payload.get("result")


def register_bot_commands(token: str) -> None:
    commands = [{"command": command, "description": description} for command, description in COMMAND_MENU]
    telegram_post(token, "setMyCommands", json_payload={"commands": commands})


def bot_commands_fingerprint(commands: Sequence[tuple[str, str]] = COMMAND_MENU) -> str:
    canonical = json.dumps(list(commands), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_bot_commands_registered(state: dict[str, Any], token: str) -> bool:
    fingerprint = bot_commands_fingerprint()
    if state.get("bot_commands_fingerprint") == fingerprint:
        return False
    register_bot_commands(token)
    state["bot_commands_registered"] = True
    state["bot_commands_fingerprint"] = fingerprint
    return True


def fetch_updates(token: str, offset: int) -> list[dict[str, Any]]:
    all_updates: list[dict[str, Any]] = []
    current_offset = offset
    while True:
        result = telegram_get(
            token,
            "getUpdates",
            params={"offset": current_offset, "timeout": 10, "limit": 100},
        )
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates result is not a list")
        if not result:
            break
        updates = [update for update in result if isinstance(update, dict) and isinstance(update.get("update_id"), int)]
        updates.sort(key=lambda item: item["update_id"])
        all_updates.extend(updates)
        current_offset = updates[-1]["update_id"] + 1 if updates else current_offset + 100
        if len(result) < 100:
            more = telegram_get(token, "getUpdates", params={"offset": current_offset, "timeout": 0, "limit": 100})
            if not more:
                break
            more_updates = [update for update in more if isinstance(update, dict) and isinstance(update.get("update_id"), int)]
            more_updates.sort(key=lambda item: item["update_id"])
            all_updates.extend(more_updates)
            if more_updates:
                current_offset = more_updates[-1]["update_id"] + 1
            break
    deduped: dict[int, dict[str, Any]] = {}
    for update in all_updates:
        deduped[update["update_id"]] = update
    return [deduped[key] for key in sorted(deduped)]


def process_telegram_updates(base_dir: Path, state: dict[str, Any], token: str, authorized_chat_id: str, current_time: datetime) -> bool:
    updates = fetch_updates(token, int(state.get("last_update_id", 0)) + 1)
    mutation_happened = False
    for update in updates:
        update_id = int(update["update_id"])
        reply_text: str | None = None
        category = "reply"
        try:
            message = update.get("message")
            if not isinstance(message, dict):
                state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
                continue
            chat = message.get("chat")
            from_user = message.get("from")
            if not isinstance(chat, dict) or str(chat.get("id")) != str(authorized_chat_id):
                state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
                continue
            if isinstance(from_user, dict) and from_user.get("is_bot"):
                state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
                continue
            text = message.get("text")
            if not isinstance(text, str):
                state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
                continue
            message_timestamp = datetime.fromtimestamp(int(message.get("date", current_time.timestamp())), tz=UTC)
            reply_text, mutated = handle_authorized_text_command(
                base_dir,
                state,
                read_ledger(base_dir),
                update_id=update_id,
                chat_id=authorized_chat_id,
                message_id=message.get("message_id") if isinstance(message.get("message_id"), int) else None,
                message_timestamp_utc=message_timestamp,
                text=text,
            )
            mutation_happened = mutation_happened or mutated
            if reply_text:
                enqueue_outbox(
                    state,
                    item_id=outbox_item_id("reply", update_id),
                    chat_id=authorized_chat_id,
                    text=reply_text,
                    category=category,
                    created_at_utc=current_time,
                )
        finally:
            state["last_update_id"] = max(int(state.get("last_update_id", 0)), update_id)
    return mutation_happened


def import_strategy_fetcher() -> Any:
    try:
        from mstr_bot import fetch_strategy_snapshot  # type: ignore

        return fetch_strategy_snapshot
    except ImportError as exc:
        raise MarketDataError("Cannot import fetch_strategy_snapshot from mstr_bot.py") from exc


def source_datetime_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return iso_seconds(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def decimal_from_market_value(value: Any, field_name: str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"{field_name} is not a valid Decimal value") from exc
    if not decimal_value.is_finite() or decimal_value <= ZERO:
        raise MarketDataError(f"{field_name} must be finite and greater than zero")
    return decimal_value


def fetch_strategy_market_data(state: dict[str, Any], *, update_cache: bool, current_time: datetime) -> MarketData:
    try:
        snapshot = import_strategy_fetcher()()
        prices = {
            "MSTR": decimal_from_market_value(snapshot.mstr_price, "snapshot.mstr_price"),
            "BTC": decimal_from_market_value(snapshot.btc_price, "snapshot.btc_price"),
        }
        source_meta = getattr(snapshot, "source_metadata", None)
        as_of = {
            "MSTR": source_datetime_to_iso(getattr(source_meta, "mstr_as_of", None)) if source_meta is not None else None,
            "BTC": source_datetime_to_iso(getattr(source_meta, "btc_as_of", None)) if source_meta is not None else None,
        }
        if update_cache:
            state["market_cache"] = {
                asset: {
                    "price_usd": decimal_plain(price),
                    "as_of": as_of.get(asset),
                    "fetched_at_utc": iso_seconds(current_time),
                    "source": "fetch_strategy_snapshot",
                }
                for asset, price in prices.items()
            }
        return MarketData(prices, as_of, "fetch_strategy_snapshot", True, set(), [])
    except Exception as exc:
        cache = state.get("market_cache", {})
        prices: dict[str, Decimal] = {}
        as_of: dict[str, str | None] = {}
        stale_assets: set[str] = set()
        for asset in ASSETS:
            item = cache.get(asset) if isinstance(cache, dict) else None
            if isinstance(item, dict) and item.get("price_usd"):
                prices[asset] = parse_stored_decimal(item["price_usd"], f"market_cache.{asset}.price_usd", allow_negative=False)
                as_of[asset] = item.get("as_of") if isinstance(item.get("as_of"), str) else item.get("fetched_at_utc")
                stale_assets.add(asset)
        warning = f"Market data fresh gagal; memakai cache terakhir. Detail: {exc}"
        return MarketData(prices, as_of, "cache", False, stale_assets, [warning])


def parse_bi_decimal(text: str) -> Decimal:
    cleaned = text.strip()
    cleaned = re.sub(r"(?i)rp", "", cleaned)
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        raise ValueError("empty rate")
    if re.search(r"[^0-9,.\-]", cleaned):
        raise ValueError("rate contains unsupported characters")
    if cleaned.startswith("-"):
        raise ValueError("rate must be positive")
    comma = cleaned.rfind(",")
    dot = cleaned.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_sep = "," if comma > dot else "."
        thousands_sep = "." if decimal_sep == "," else ","
        whole, fraction = cleaned.rsplit(decimal_sep, 1)
        if not fraction.isdigit() or len(fraction) not in {1, 2}:
            raise ValueError("ambiguous decimal separator")
        whole = whole.replace(thousands_sep, "")
        if not whole.isdigit():
            raise ValueError("invalid thousands grouping")
        normalized = whole + "." + fraction
    elif comma >= 0 or dot >= 0:
        separator = "," if comma >= 0 else "."
        parts = cleaned.split(separator)
        if not all(part.isdigit() for part in parts):
            raise ValueError("invalid rate number")
        if len(parts) == 2 and len(parts[1]) in {1, 2}:
            normalized = parts[0] + "." + parts[1]
        elif len(parts) >= 2 and all(len(part) == 3 for part in parts[1:]) and 1 <= len(parts[0]) <= 3:
            normalized = "".join(parts)
        else:
            raise ValueError("ambiguous rate separator")
    else:
        if not cleaned.isdigit():
            raise ValueError("invalid rate number")
        normalized = cleaned
    value = Decimal(normalized)
    if value <= ZERO or value < Decimal("1000") or value > Decimal("100000"):
        raise ValueError("implausible JISDOR rate")
    return value


def parse_bi_date(text: str) -> date:
    value = text.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return date.fromisoformat(value)


def parse_jisdor_xml(xml_text: str) -> tuple[Decimal, str]:
    root = ET.fromstring(xml_text)
    date_names = {"tanggal", "tgl", "date", "effective_date", "effectivedate", "kursdate", "tglkurs", "datevalue", "tgl_subkursasing"}
    rate_names = {"jisdor", "nilai", "kurs", "rate", "value", "subkurs", "kursjual", "kurs_tengah", "jual_subkursasing"}
    currency_names = {"currency", "curr", "kode", "kode_valuta", "valuta", "mts_subkursasing", "mata_uang"}
    candidates: list[tuple[date, Decimal]] = []
    for element in root.iter():
        children = list(element)
        if not children:
            continue
        fields: dict[str, str] = {}
        for child in children:
            if child.text and child.text.strip():
                fields[local_name(child.tag)] = child.text.strip()
        currency_values = [
            text.strip().upper()
            for name, text in fields.items()
            if name.replace("-", "_") in currency_names
        ]
        if currency_values and "USD" not in currency_values:
            continue
        date_value: date | None = None
        rate_value: Decimal | None = None
        for name, text in fields.items():
            normalized = name.replace("-", "_")
            if date_value is None and normalized in date_names:
                try:
                    date_value = parse_bi_date(text)
                except Exception:
                    pass
            if rate_value is None and normalized in rate_names:
                try:
                    rate_value = parse_bi_decimal(text)
                except Exception:
                    pass
        if date_value is not None and rate_value is not None:
            candidates.append((date_value, rate_value))
    if not candidates:
        raise MarketDataError("BI JISDOR XML did not contain a valid dated rate")
    official_date, rate = max(candidates, key=lambda item: item[0])
    return rate, official_date.isoformat()


def fetch_jisdor(state: dict[str, Any], *, update_cache: bool, current_time: datetime) -> JisdorData:
    try:
        response = requests.get(
            BI_JISDOR_URL,
            headers={"Accept": "application/xml, text/xml", "User-Agent": "nevets-portfolio-worker/1.0"},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        rate, official_date = parse_jisdor_xml(response.text)
        if update_cache:
            state["jisdor_cache"] = {
                "rate": decimal_plain(rate),
                "official_date": official_date,
                "fetched_at_utc": iso_seconds(current_time),
                "source": "Bank Indonesia JISDOR",
            }
        return JisdorData(rate, official_date, True)
    except Exception as exc:
        cache = state.get("jisdor_cache", {})
        if isinstance(cache, dict) and cache.get("rate"):
            rate = parse_stored_decimal(cache["rate"], "jisdor_cache.rate", allow_negative=False)
            official_date = cache.get("official_date") if isinstance(cache.get("official_date"), str) else None
            return JisdorData(rate, official_date, False, f"USD/IDR memakai cache JISDOR: {official_date or 'tanggal tidak tersedia'}")
        return JisdorData(None, None, False, f"USD/IDR JISDOR tidak tersedia: {exc}")


def load_mstr_engine_state(base_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = base_dir / MSTR_ENGINE_STATE_FILE
    if not path.exists():
        return None, "MSTR engine state belum tersedia."
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataIntegrityError("mstr_decision_engine_v2_state.json malformed") from exc
    if not isinstance(payload, dict):
        raise DataIntegrityError("mstr_decision_engine_v2_state.json must be an object")
    return payload, None


def classify_mstr_zone(price: Decimal, zones: Mapping[str, Any]) -> str | None:
    try:
        strong = Decimal(str(zones["strong_buy_price"]))
        accumulate = Decimal(str(zones["accumulate_price"]))
        hold = Decimal(str(zones["hold_price"]))
        reduce = Decimal(str(zones["reduce_price"]))
    except Exception:
        return None
    if price <= strong:
        return "STRONG BUY"
    if price <= accumulate:
        return "ACCUMULATE"
    if price <= hold:
        return "HOLD"
    if price <= reduce:
        return "REDUCE"
    return "SELL"


def mstr_engine_lines(
    base_dir: Path,
    mstr_position: Position,
    market_data: MarketData,
) -> tuple[list[str], list[str]]:
    if mstr_position.quantity <= ZERO:
        return [], []
    payload, warning = load_mstr_engine_state(base_dir)
    if payload is None:
        return [], [warning or "MSTR engine state tidak tersedia."]
    zones = payload.get("zones")
    fingerprint = payload.get("fingerprint")
    warnings: list[str] = []
    lines: list[str] = []
    if not isinstance(zones, dict):
        warnings.append("MSTR engine zones tidak tersedia.")
    else:
        price = market_data.prices.get("MSTR")
        zone = classify_mstr_zone(price, zones) if price is not None else None
        if zone is not None:
            lines.extend(["", "🎯 MSTR ENGINE", f"Current Zone: {zone}"])
        else:
            warnings.append("Current Zone MSTR tidak bisa dihitung.")
        fair = zones.get("fair_price")
        strong = zones.get("strong_buy_price")
        reduce = zones.get("reduce_price")
        if fair is not None:
            lines.append(f"Fair Price: {fmt_usd(Decimal(str(fair)))}")
        if strong is not None:
            lines.append(f"Strong Buy: ≤ {fmt_usd(Decimal(str(strong)))}")
        if reduce is not None:
            lines.append(f"Sell: > {fmt_usd(Decimal(str(reduce)))}")
    action = payload.get("last_action")
    if isinstance(action, str):
        if not lines:
            lines.extend(["", "🎯 MSTR ENGINE"])
        lines.append(f"Engine Action: {action}")
    if not isinstance(fingerprint, dict):
        warnings.append("Fingerprint MSTR engine tidak tersedia untuk BTC exposure.")
    return lines, warnings


def btc_exposure_lines(base_dir: Path, mstr_position: Position, direct_btc: Decimal) -> tuple[list[str], list[str]]:
    if mstr_position.quantity <= ZERO:
        return [], []
    payload, warning = load_mstr_engine_state(base_dir)
    if payload is None:
        return [], [warning or "MSTR engine state belum tersedia."]
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return [], ["Fingerprint MSTR engine tidak tersedia untuk BTC exposure."]
    try:
        btc_holdings = Decimal(str(fingerprint["btc_holdings"]))
        basic_shares_m = Decimal(str(fingerprint["basic_shares_m"]))
        btc_per_share = btc_holdings / (basic_shares_m * Decimal("1000000"))
        btc_via_mstr = mstr_position.quantity * btc_per_share
        total = direct_btc + btc_via_mstr
    except Exception:
        return [], ["BTC exposure via MSTR tidak bisa dihitung."]
    return [
        "",
        "₿ TOTAL BTC EXPOSURE",
        f"Direct BTC: {fmt_quantity('BTC', direct_btc)} BTC",
        f"BTC via MSTR: {fmt_quantity('BTC', btc_via_mstr)} BTC",
        f"Total Exposure: {fmt_quantity('BTC', total)} BTC",
    ], []


def current_era_id(events: Sequence[Mapping[str, Any]]) -> str:
    active = replay_events(events).active_events
    era = "ERA-000000"
    for event in active:
        if event["event_type"] == "RESET":
            era = event["event_id"]
    return era


def era_id_at(events: Sequence[Mapping[str, Any]], timestamp_utc: datetime) -> str:
    active = replay_events(events).active_events
    era = "ERA-000000"
    cutoff = ensure_aware_utc(timestamp_utc)
    for event in active:
        if parse_iso_datetime(event["timestamp_utc"], "timestamp_utc") > cutoff:
            continue
        if event["event_type"] == "RESET":
            era = event["event_id"]
    return era


def event_era_id(events: Sequence[Mapping[str, Any]], event: Mapping[str, Any]) -> str:
    event_time = parse_iso_datetime(event["timestamp_utc"], "timestamp_utc")
    active = replay_events(events).active_events
    era = "ERA-000000"
    for candidate in active:
        candidate_time = parse_iso_datetime(candidate["timestamp_utc"], "timestamp_utc")
        if candidate_time > event_time:
            continue
        if candidate["event_type"] == "RESET":
            era = candidate["event_id"]
    return era


def first_buy_time_in_era(events: Sequence[Mapping[str, Any]], era_id: str, before_or_at: datetime) -> datetime | None:
    cutoff = ensure_aware_utc(before_or_at)
    for event in replay_events(events).active_events:
        if event["event_type"] == "BUY" and event_era_id(events, event) == era_id:
            event_time = parse_iso_datetime(event["timestamp_utc"], "timestamp_utc")
            if event_time <= cutoff:
                return event_time
    return None


def snapshot_raw_prices(snapshot: Mapping[str, Any]) -> dict[str, Decimal]:
    raw = snapshot.get("raw")
    if not isinstance(raw, dict):
        raise DataIntegrityError("snapshot raw must be an object")
    prices = raw.get("prices")
    if not isinstance(prices, dict):
        raise DataIntegrityError("snapshot raw.prices must be an object")
    parsed: dict[str, Decimal] = {}
    for asset in ASSETS:
        if prices.get(asset) is not None:
            parsed[asset] = require_positive_decimal_string(prices[asset], f"snapshot.raw.prices.{asset}")
    return parsed


def portfolio_values(positions: Mapping[str, Position], prices: Mapping[str, Decimal]) -> tuple[dict[str, dict[str, Decimal]], Decimal, Decimal]:
    asset_values: dict[str, dict[str, Decimal]] = {}
    total_market_value = ZERO
    total_cost_basis = ZERO
    for asset, position in positions.items():
        if position.quantity <= ZERO:
            continue
        if asset not in prices:
            raise MarketDataError(f"Harga {asset} belum tersedia.")
        price = prices[asset]
        market_value = position.quantity * price
        cost_basis = position.quantity * position.average_cost
        unrealized = market_value - cost_basis
        percent = (unrealized / cost_basis * HUNDRED) if cost_basis > ZERO else ZERO
        asset_values[asset] = {
            "price": price,
            "market_value": market_value,
            "cost_basis": cost_basis,
            "unrealized_pl": unrealized,
            "unrealized_pct": percent,
        }
        total_market_value += market_value
        total_cost_basis += cost_basis
    return asset_values, total_market_value, total_cost_basis


def read_snapshots(base_dir: Path = Path(".")) -> list[dict[str, Any]]:
    ensure_data_files(base_dir)
    snapshots: list[dict[str, Any]] = []
    for index, line in enumerate(snapshot_path(base_dir).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            snapshot = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataIntegrityError(f"Malformed snapshot JSONL line {index}") from exc
        if not isinstance(snapshot, dict):
            raise DataIntegrityError(f"Snapshot line {index} must be a JSON object")
        validate_snapshot_record(snapshot)
        snapshots.append(snapshot)
    return snapshots


def validate_snapshot_record(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise DataIntegrityError("snapshot schema_version must be 1")
    date_wib = parse_wib_date(snapshot.get("date_wib"))
    parse_iso_datetime(snapshot.get("captured_at_utc"), "captured_at_utc")
    captured_at_wib = parse_offset_datetime(snapshot.get("captured_at_wib"), "captured_at_wib")
    if captured_at_wib.utcoffset() != timedelta(hours=7):
        raise DataIntegrityError("snapshot captured_at_wib must use +07:00 offset")
    if captured_at_wib.date() != date_wib:
        raise DataIntegrityError("snapshot date_wib must match captured_at_wib date")
    if not isinstance(snapshot.get("era_id"), str):
        raise DataIntegrityError("snapshot era_id must be a string")
    snapshot_raw_prices(snapshot)
    raw = snapshot.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("jisdor"), dict) and raw["jisdor"].get("rate") is not None:
        require_positive_decimal_string(raw["jisdor"]["rate"], "snapshot.raw.jisdor.rate")
    derived = snapshot.get("derived")
    if derived is not None:
        if not isinstance(derived, dict):
            raise DataIntegrityError("snapshot derived must be an object")
        for key in ("cost_basis", "market_value", "net_external_flow"):
            if key in derived and derived[key] is not None:
                parse_stored_decimal(derived[key], f"snapshot.derived.{key}")
        if "performance_index" in derived and derived["performance_index"] is not None:
            parse_stored_decimal(derived["performance_index"], "snapshot.derived.performance_index", allow_negative=False)
        if "interval_return" in derived and derived["interval_return"] is not None:
            parse_stored_decimal(derived["interval_return"], "snapshot.derived.interval_return")


def write_snapshots(base_dir: Path, snapshots: Sequence[Mapping[str, Any]]) -> None:
    lines = [json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for snapshot in snapshots]
    atomic_write_text(snapshot_path(base_dir), "\n".join(lines) + ("\n" if lines else ""))


def signed_cash_flow(event: Mapping[str, Any]) -> Decimal:
    if event["event_type"] not in {"BUY", "SELL"}:
        return ZERO
    quantity = parse_stored_decimal(event["quantity"], "quantity", allow_negative=False)
    price = parse_stored_decimal(event["price_usd"], "price_usd", allow_negative=False)
    amount = quantity * price
    return amount if event["event_type"] == "BUY" else -amount


def cash_flows_between(
    events: Sequence[Mapping[str, Any]],
    *,
    era_id: str,
    start: datetime,
    end: datetime,
    include_start: bool,
) -> list[CashFlow]:
    start_utc = ensure_aware_utc(start)
    end_utc = ensure_aware_utc(end)
    flows: list[CashFlow] = []
    for event in replay_events(events).active_events:
        if event["event_type"] not in {"BUY", "SELL"} or event_era_id(events, event) != era_id:
            continue
        event_time = parse_iso_datetime(event["timestamp_utc"], "timestamp_utc")
        if (event_time > start_utc or (include_start and event_time == start_utc)) and event_time <= end_utc:
            flows.append(CashFlow(event_time, signed_cash_flow(event)))
    return flows


def modified_dietz_return(beginning_value: Decimal, ending_value: Decimal, flows: Sequence[CashFlow], start: datetime, end: datetime) -> Decimal | None:
    start_utc = ensure_aware_utc(start)
    end_utc = ensure_aware_utc(end)
    total_seconds = Decimal(str((end_utc - start_utc).total_seconds()))
    if total_seconds <= ZERO:
        return None
    total_flow = sum((flow.amount for flow in flows), ZERO)
    weighted_flow = ZERO
    for flow in flows:
        remaining = Decimal(str((end_utc - ensure_aware_utc(flow.timestamp_utc)).total_seconds())) / total_seconds
        if remaining < ZERO:
            remaining = ZERO
        if remaining > ONE:
            remaining = ONE
        weighted_flow += remaining * flow.amount
    denominator = beginning_value + weighted_flow
    if denominator <= ZERO:
        return None
    return (ending_value - beginning_value - total_flow) / denominator


def rebuild_snapshots(base_dir: Path = Path(".")) -> None:
    ensure_data_files(base_dir)
    events = read_ledger(base_dir)
    snapshots = sorted(read_snapshots(base_dir), key=lambda item: (item["date_wib"], item["captured_at_utc"]))
    rebuilt: list[dict[str, Any]] = []
    previous_by_era: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        capture_time = parse_iso_datetime(snapshot["captured_at_utc"], "captured_at_utc")
        prices = snapshot_raw_prices(snapshot)
        positions = replay_events(events, until_utc=capture_time).positions
        values, market_value, cost_basis = portfolio_values(positions, prices) if not position_is_empty(positions) else ({}, ZERO, ZERO)
        era_id = era_id_at(events, capture_time)
        snapshot["era_id"] = era_id
        previous = previous_by_era.get(era_id)
        if previous is None:
            start = first_buy_time_in_era(events, era_id, capture_time) or capture_time
            beginning_value = ZERO
            include_start = True
            previous_index = Decimal("100")
        else:
            start = parse_iso_datetime(previous["captured_at_utc"], "captured_at_utc")
            beginning_value = parse_stored_decimal(previous["derived"]["market_value"], "previous.market_value")
            include_start = False
            previous_index = parse_stored_decimal(previous["derived"]["performance_index"], "previous.performance_index", allow_negative=False)
        flows = cash_flows_between(events, era_id=era_id, start=start, end=capture_time, include_start=include_start)
        interval_return = modified_dietz_return(beginning_value, market_value, flows, start, capture_time)
        performance_index = previous_index if interval_return is None else previous_index * (ONE + interval_return)
        snapshot["derived"] = {
            "quantities": {asset: decimal_plain(position.quantity) for asset, position in positions.items()},
            "average_costs": {asset: decimal_plain(position.average_cost) for asset, position in positions.items()},
            "asset_values": {
                asset: {key: decimal_plain(value) for key, value in asset_value.items()}
                for asset, asset_value in values.items()
            },
            "cost_basis": decimal_plain(cost_basis),
            "market_value": decimal_plain(market_value),
            "net_external_flow": decimal_plain(sum((flow.amount for flow in flows), ZERO)),
            "interval_return": decimal_plain(interval_return) if interval_return is not None else None,
            "performance_index": decimal_plain(performance_index),
        }
        rebuilt.append(snapshot)
        previous_by_era[era_id] = snapshot
    write_snapshots(base_dir, rebuilt)


def create_or_update_daily_snapshot(
    base_dir: Path,
    state: dict[str, Any],
    market_data: MarketData,
    jisdor: JisdorData,
    *,
    current_time: datetime,
) -> bool:
    events = read_ledger(base_dir)
    positions = replay_events(events).positions
    if position_is_empty(positions):
        return False
    held_assets = [asset for asset, position in positions.items() if position.quantity > ZERO]
    missing = [asset for asset in held_assets if asset not in market_data.prices]
    if missing:
        return False
    capture_wib = to_wib(current_time)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "date_wib": capture_wib.date().isoformat(),
        "captured_at_utc": iso_seconds(current_time),
        "captured_at_wib": wib_iso_seconds(current_time),
        "era_id": current_era_id(events),
        "raw": {
            "prices": {asset: decimal_plain(market_data.prices[asset]) for asset in market_data.prices},
            "market": {
                "fresh": market_data.fresh,
                "stale_assets": sorted(market_data.stale_assets),
                "source": market_data.source,
                "as_of": market_data.as_of,
            },
            "jisdor": {
                "rate": decimal_plain(jisdor.rate) if jisdor.rate is not None else None,
                "official_date": jisdor.official_date,
                "fresh": jisdor.fresh,
            },
        },
        "derived": {},
    }
    snapshots = [item for item in read_snapshots(base_dir) if item["date_wib"] != snapshot["date_wib"]]
    snapshots.append(snapshot)
    snapshots.sort(key=lambda item: (item["date_wib"], item["captured_at_utc"]))
    write_snapshots(base_dir, snapshots)
    rebuild_snapshots(base_dir)
    return True


def subtract_months(source: date, months: int) -> date:
    month_index = source.year * 12 + source.month - 1 - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(source.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def subtract_years(source: date, years: int) -> date:
    try:
        return source.replace(year=source.year - years)
    except ValueError:
        return source.replace(year=source.year - years, day=28)


def period_targets(current: date) -> dict[str, date]:
    return {
        "1D": current - timedelta(days=1),
        "1W": current - timedelta(days=7),
        "1M": subtract_months(current, 1),
        "6M": subtract_months(current, 6),
        "1Y": subtract_years(current, 1),
        "3Y": subtract_years(current, 3),
        "5Y": subtract_years(current, 5),
        "10Y": subtract_years(current, 10),
    }


def period_returns(base_dir: Path, era_id: str, current_date: date) -> dict[str, Decimal | None]:
    snapshots = [
        item
        for item in read_snapshots(base_dir)
        if item.get("era_id") == era_id and isinstance(item.get("derived"), dict) and item["derived"].get("performance_index") is not None
    ]
    snapshots.sort(key=lambda item: item["date_wib"])
    if not snapshots:
        return {label: None for label in [*period_targets(current_date), "Since Inception"]}
    current_snapshot = snapshots[-1]
    current_index = parse_stored_decimal(current_snapshot["derived"]["performance_index"], "performance_index", allow_negative=False)
    results: dict[str, Decimal | None] = {}
    by_target = period_targets(current_date)
    for label, target in by_target.items():
        candidates = [item for item in snapshots if parse_wib_date(item["date_wib"]) <= target]
        if not candidates:
            results[label] = None
            continue
        base_index = parse_stored_decimal(candidates[-1]["derived"]["performance_index"], "performance_index", allow_negative=False)
        results[label] = (current_index / base_index - ONE) * HUNDRED if base_index > ZERO else None
    inception_index = Decimal("100")
    results["Since Inception"] = (current_index / inception_index - ONE) * HUNDRED
    return results


def current_period_returns(
    base_dir: Path,
    events: Sequence[Mapping[str, Any]],
    positions: Mapping[str, Position],
    prices: Mapping[str, Decimal],
    current_time: datetime,
) -> dict[str, Decimal | None]:
    current_utc = ensure_aware_utc(current_time)
    current_date = to_wib(current_utc).date()
    era_id = current_era_id(events)
    labels = [*period_targets(current_date), "Since Inception"]
    snapshots = [
        item
        for item in read_snapshots(base_dir)
        if item.get("era_id") == era_id
        and isinstance(item.get("derived"), dict)
        and item["derived"].get("performance_index") is not None
        and parse_iso_datetime(item["captured_at_utc"], "captured_at_utc") <= current_utc
    ]
    snapshots.sort(key=lambda item: (item["date_wib"], item["captured_at_utc"]))
    try:
        _, current_market_value, _ = portfolio_values(positions, prices)
    except MarketDataError:
        return {label: None for label in labels}
    if snapshots:
        previous = snapshots[-1]
        start = parse_iso_datetime(previous["captured_at_utc"], "captured_at_utc")
        beginning_value = parse_stored_decimal(previous["derived"]["market_value"], "previous.market_value")
        previous_index = parse_stored_decimal(previous["derived"]["performance_index"], "previous.performance_index", allow_negative=False)
        flows = cash_flows_between(events, era_id=era_id, start=start, end=current_utc, include_start=False)
        interval_return = modified_dietz_return(beginning_value, current_market_value, flows, start, current_utc)
        current_index = previous_index if interval_return is None else previous_index * (ONE + interval_return)
    else:
        start = first_buy_time_in_era(events, era_id, current_utc) or current_utc
        flows = cash_flows_between(events, era_id=era_id, start=start, end=current_utc, include_start=True)
        interval_return = modified_dietz_return(ZERO, current_market_value, flows, start, current_utc)
        current_index = Decimal("100") if interval_return is None else Decimal("100") * (ONE + interval_return)
    results: dict[str, Decimal | None] = {}
    for label, target in period_targets(current_date).items():
        candidates = [item for item in snapshots if parse_wib_date(item["date_wib"]) <= target]
        if not candidates:
            results[label] = None
            continue
        base_index = parse_stored_decimal(candidates[-1]["derived"]["performance_index"], "performance_index", allow_negative=False)
        results[label] = (current_index / base_index - ONE) * HUNDRED if base_index > ZERO else None
    results["Since Inception"] = (current_index / Decimal("100") - ONE) * HUNDRED
    return results


def render_portfolio_report(
    base_dir: Path = Path("."),
    *,
    now: datetime | None = None,
    market_data: MarketData | None = None,
    jisdor: JisdorData | None = None,
) -> str:
    ensure_data_files(base_dir)
    current_time = ensure_aware_utc(now or now_utc())
    events = read_ledger(base_dir)
    positions = replay_events(events).positions
    if position_is_empty(positions):
        return "\n".join(
            [
                "💼 PORTOFOLIO MASIH KOSONG",
                "",
                "Belum ada posisi MSTR atau BTC.",
                "",
                "/buy_mstr JUMLAH HARGA",
                "/buy_btc JUMLAH HARGA",
            ]
        )
    state = load_state(base_dir)
    market = market_data or fetch_strategy_market_data(state, update_cache=False, current_time=current_time)
    fx = jisdor or fetch_jisdor(state, update_cache=False, current_time=current_time)
    held_assets = [asset for asset, position in positions.items() if position.quantity > ZERO]
    missing = [asset for asset in held_assets if asset not in market.prices]
    if missing:
        return "Data market belum tersedia untuk: " + ", ".join(missing) + ". Laporan belum bisa dibuat."
    asset_values, total_market_value, total_cost_basis = portfolio_values(positions, market.prices)
    unrealized = total_market_value - total_cost_basis
    unrealized_pct = (unrealized / total_cost_basis * HUNDRED) if total_cost_basis > ZERO else ZERO
    wib_time = to_wib(current_time)
    idr_total = fmt_idr(total_market_value * fx.rate) if fx.rate is not None else "IDR: unavailable"
    lines = [
        "💼 NEVETS HOLDING | PERSONAL PORTFOLIO",
        f"📅 {wib_time.strftime('%d %b %Y | %H:%M WIB')}",
        "",
        "💰 TOTAL PORTFOLIO",
        f"Market Value: {fmt_usd(total_market_value)} | {idr_total}",
        f"Cost Basis: {fmt_usd(total_cost_basis)}",
        f"Unrealized P/L: {fmt_usd_signed(unrealized)} ({fmt_pct(unrealized_pct)})",
    ]
    if fx.rate is not None:
        suffix = "" if fx.fresh else " (cache)"
        lines.append(f"USD/IDR JISDOR: {fmt_idr(fx.rate)}{suffix}")
    else:
        lines.append("USD/IDR JISDOR: unavailable")
    lines.extend(["", "📊 ALLOCATION"])
    for asset, values in sorted(asset_values.items()):
        allocation = values["market_value"] / total_market_value * HUNDRED if total_market_value > ZERO else ZERO
        lines.append(f"{asset}: {fmt_pct(allocation, signed=False)}")
    era_id = current_era_id(events)
    returns = current_period_returns(base_dir, events, positions, market.prices, current_time)
    lines.extend(["", "📈 PORTFOLIO PERFORMANCE"])
    for label in ("1D", "1W", "1M", "6M", "1Y", "3Y", "5Y", "10Y", "Since Inception"):
        value = returns.get(label)
        lines.append(f"{label}: {'N/A' if value is None else fmt_pct(value)}")
    if positions["BTC"].quantity > ZERO:
        btc_values = asset_values["BTC"]
        lines.extend(
            [
                "",
                "₿ BTC POSITION",
                f"Position: {fmt_quantity('BTC', positions['BTC'].quantity, trim=False)} BTC",
                f"Average Cost: {fmt_usd(positions['BTC'].average_cost)}",
                f"BTC Spot: {fmt_usd(btc_values['price'])}",
                f"Market Value: {fmt_usd(btc_values['market_value'])}",
                f"Unrealized P/L: {fmt_usd_signed(btc_values['unrealized_pl'])} ({fmt_pct(btc_values['unrealized_pct'])})",
            ]
        )
    if positions["MSTR"].quantity > ZERO:
        mstr_values = asset_values["MSTR"]
        lines.extend(
            [
                "",
                "📈 MSTR POSITION",
                f"Position: {fmt_quantity('MSTR', positions['MSTR'].quantity)} lembar",
                f"Average Cost: {fmt_usd(positions['MSTR'].average_cost)}",
                f"MSTR Last: {fmt_usd(mstr_values['price'])}",
                f"Market Value: {fmt_usd(mstr_values['market_value'])}",
                f"Unrealized P/L: {fmt_usd_signed(mstr_values['unrealized_pl'])} ({fmt_pct(mstr_values['unrealized_pct'])})",
            ]
        )
        exposure, exposure_warnings = btc_exposure_lines(base_dir, positions["MSTR"], positions["BTC"].quantity)
        lines.extend(exposure)
    else:
        exposure_warnings = []
    engine, engine_warnings = mstr_engine_lines(base_dir, positions["MSTR"], market)
    lines.extend(engine)
    warnings = [*market.warnings]
    if market.stale_assets:
        warnings.append("Harga market memakai cache/stale: " + ", ".join(sorted(market.stale_assets)))
    if fx.warning:
        warnings.append(fx.warning)
    warnings.extend(exposure_warnings)
    warnings.extend(engine_warnings)
    if warnings:
        lines.extend(["", "⚠️ CATATAN"])
        for warning in warnings[:5]:
            lines.append(f"- {warning}")
    report = "\n".join(lines)
    if len(report) > REPORT_MAX_CHARS:
        compact = [line for line in lines if not line.startswith("- ")][:]
        report = "\n".join(compact)
    if len(report) > REPORT_MAX_CHARS:
        report = report[: REPORT_MAX_CHARS - 20].rstrip() + "\n[terpotong]"
    return report


def threshold_membership(percent: Decimal | None) -> dict[str, list[str]]:
    if percent is None:
        return {"positive": [], "negative": []}
    positive = [decimal_plain(threshold) for threshold in THRESHOLDS if threshold > ZERO and percent >= threshold]
    negative = [decimal_plain(threshold) for threshold in THRESHOLDS if threshold < ZERO and percent <= threshold]
    return {"positive": positive, "negative": negative}


def current_threshold_memberships(positions: Mapping[str, Position], market_data: MarketData) -> dict[str, dict[str, list[str]]]:
    memberships: dict[str, dict[str, list[str]]] = {}
    total_market_value = ZERO
    total_cost_basis = ZERO
    for asset, position in positions.items():
        if position.quantity <= ZERO or asset not in market_data.prices:
            memberships[asset] = {"positive": [], "negative": []}
            continue
        percent = (market_data.prices[asset] / position.average_cost - ONE) * HUNDRED if position.average_cost > ZERO else None
        memberships[asset] = threshold_membership(percent)
        total_market_value += position.quantity * market_data.prices[asset]
        total_cost_basis += position.quantity * position.average_cost
    portfolio_percent = (total_market_value - total_cost_basis) / total_cost_basis * HUNDRED if total_cost_basis > ZERO else None
    memberships["PORTFOLIO"] = threshold_membership(portfolio_percent)
    return memberships


def establish_alert_baseline(state: dict[str, Any], positions: Mapping[str, Position], market_data: MarketData) -> None:
    memberships = current_threshold_memberships(positions, market_data)
    for key in ("MSTR", "BTC", "PORTFOLIO"):
        state["alert_state"][key] = memberships[key]


def evaluate_pl_alerts(
    state: dict[str, Any],
    positions: Mapping[str, Position],
    market_data: MarketData,
) -> list[str]:
    if not market_data.fresh:
        return []
    memberships = current_threshold_memberships(positions, market_data)
    messages: list[str] = []
    for key in ("MSTR", "BTC", "PORTFOLIO"):
        previous = state["alert_state"].get(key, {"positive": [], "negative": []})
        current = memberships[key]
        for side, label in (("positive", "PROFIT"), ("negative", "LOSS")):
            new_values = [value for value in current[side] if value not in previous.get(side, [])]
            if not new_values:
                continue
            crossed = [Decimal(value) for value in new_values]
            latest = max(crossed) if side == "positive" else min(crossed)
            icon = "🚀" if side == "positive" else "⚠️"
            pretty = " | ".join(("+" if Decimal(value) > ZERO else "") + f"{value}%" for value in new_values)
            messages.append(
                "\n".join(
                    [
                        f"{icon} {key} {label} {fmt_pct(latest, signed=True)}",
                        "",
                        "Threshold baru yang terlewati:",
                        pretty,
                    ]
                )
            )
        state["alert_state"][key] = current
    return messages


def evaluate_mstr_zone_alert(base_dir: Path, state: dict[str, Any], positions: Mapping[str, Position], market_data: MarketData) -> list[str]:
    if not market_data.fresh or "MSTR" not in market_data.prices:
        return []
    payload, _ = load_mstr_engine_state(base_dir)
    if payload is None or not isinstance(payload.get("zones"), dict):
        return []
    zone = classify_mstr_zone(market_data.prices["MSTR"], payload["zones"])
    previous = state["alert_state"].get("mstr_zone")
    state["alert_state"]["mstr_zone"] = zone
    if zone not in {"STRONG BUY", "SELL"} or previous == zone:
        return []
    lines = [f"🎯 MSTR ZONE ALERT: {zone}", "", f"MSTR Last: {fmt_usd(market_data.prices['MSTR'])}"]
    mstr_position = positions["MSTR"]
    if mstr_position.quantity > ZERO:
        market_value = mstr_position.quantity * market_data.prices["MSTR"]
        cost_basis = mstr_position.quantity * mstr_position.average_cost
        percent = (market_value - cost_basis) / cost_basis * HUNDRED if cost_basis > ZERO else ZERO
        lines.extend(
            [
                f"Position: {fmt_quantity('MSTR', mstr_position.quantity)} lembar",
                f"Average Cost: {fmt_usd(mstr_position.average_cost)}",
                f"Unrealized P/L: {fmt_usd_signed(market_value - cost_basis)} ({fmt_pct(percent)})",
            ]
        )
    return ["\n".join(lines)]


def reset_alert_state(state: dict[str, Any]) -> None:
    state["alert_state"] = default_state()["alert_state"]


def to_challenge_market_inputs(
    market: MarketData,
    jisdor: JisdorData,
    *,
    current_time: datetime,
) -> mstr_challenge.MarketInputs:
    freshness = "fresh" if market.fresh and jisdor.fresh else "cached"
    if not market.prices:
        freshness = "unavailable"
    elif market.stale_assets or not jisdor.fresh:
        freshness = "stale"
    warnings = list(market.warnings)
    if jisdor.warning:
        warnings.append(jisdor.warning)
    return mstr_challenge.MarketInputs(
        mstr_price=market.prices.get("MSTR"),
        btc_price=market.prices.get("BTC"),
        usd_idr=jisdor.rate,
        mstr_as_of=market.as_of.get("MSTR"),
        btc_as_of=market.as_of.get("BTC"),
        fx_as_of=jisdor.official_date,
        mstr_source=market.source,
        btc_source=market.source,
        fx_source="Bank Indonesia JISDOR" if jisdor.rate is not None else None,
        fetched_at=iso_seconds(current_time),
        freshness=freshness,
        market_status=mstr_challenge.us_equity_market_status(current_time, market.as_of.get("MSTR")),
        warnings=tuple(warnings),
    )


def cached_jisdor_data(state: Mapping[str, Any]) -> JisdorData:
    cache = state.get("jisdor_cache", {})
    if not isinstance(cache, dict) or not cache.get("rate"):
        return JisdorData(None, None, False, "USD/IDR JISDOR cache is unavailable")
    rate = parse_stored_decimal(cache["rate"], "jisdor_cache.rate", allow_negative=False)
    official_date = cache.get("official_date") if isinstance(cache.get("official_date"), str) else None
    return JisdorData(rate, official_date, False, "USD/IDR uses the persisted JISDOR cache")


def prepare(
    *,
    base_dir: Path = Path("."),
    daily_report: bool = False,
    force_report: bool = False,
    current_time: datetime | None = None,
) -> int:
    ensure_data_files(base_dir)
    mstr_challenge.ensure_challenge_files(base_dir)
    validate_all(base_dir)
    state = load_state(base_dir)
    token, chat_id = telegram_credentials()
    current = ensure_aware_utc(current_time or now_utc())
    if token and chat_id:
        ensure_bot_commands_registered(state, token)
    mutation_happened = False
    if token and chat_id:
        mutation_happened = process_telegram_updates(base_dir, state, token, chat_id, current)
    events = read_ledger(base_dir)
    positions = replay_events(events).positions
    market = fetch_strategy_market_data(state, update_cache=True, current_time=current)
    challenge_config = mstr_challenge.load_config(base_dir)
    should_refresh_jisdor = challenge_config["status"] != "prelaunch" or daily_report
    jisdor = (
        fetch_jisdor(state, update_cache=True, current_time=current)
        if should_refresh_jisdor
        else cached_jisdor_data(state)
    )
    if mutation_happened:
        if market.fresh:
            establish_alert_baseline(state, positions, market)
            state["alert_baseline_pending"] = False
        else:
            state["alert_baseline_pending"] = True
    elif state.get("alert_baseline_pending") and market.fresh:
        establish_alert_baseline(state, positions, market)
        state["alert_baseline_pending"] = False
    else:
        for message in evaluate_pl_alerts(state, positions, market):
            if chat_id:
                enqueue_outbox(state, item_id=outbox_item_id("pl-alert", message), chat_id=chat_id, text=message, category="alert", created_at_utc=current)
    for message in evaluate_mstr_zone_alert(base_dir, state, positions, market):
        if chat_id:
            enqueue_outbox(state, item_id=outbox_item_id("zone-alert", message), chat_id=chat_id, text=message, category="alert", created_at_utc=current)
    if daily_report:
        wib_date = to_wib(current).date().isoformat()
        should_send = force_report or state.get("last_daily_report_date_wib") != wib_date
        if not position_is_empty(positions):
            snapshot_created = create_or_update_daily_snapshot(base_dir, state, market, jisdor, current_time=current)
            if not snapshot_created:
                print("Daily portfolio snapshot skipped: held-asset market price unavailable.", file=sys.stderr)
            if snapshot_created and should_send and chat_id:
                report = render_portfolio_report(base_dir, now=current, market_data=market, jisdor=jisdor)
                enqueue_outbox(state, item_id=outbox_item_id("daily", wib_date), chat_id=chat_id, text=report, category="daily_report", created_at_utc=current)
                state["last_daily_report_date_wib"] = wib_date
    challenge_market = to_challenge_market_inputs(market, jisdor, current_time=current)
    mstr_challenge.export_public(
        base_dir,
        generated_at=current,
        market=challenge_market,
        create_market_snapshot=challenge_config["status"] != "prelaunch",
    )
    save_state(base_dir, state)
    return 0


def flush(base_dir: Path = Path(".")) -> int:
    state = load_state(base_dir)
    token, _ = telegram_credentials()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is required for flush")
    outbox = state.get("outbox", [])
    while outbox:
        item = outbox[0]
        try:
            telegram_post(
                token,
                "sendMessage",
                json_payload={"chat_id": item["chat_id"], "text": item["text"], "disable_web_page_preview": True},
            )
        except TelegramError:
            save_state(base_dir, state)
            return 1
        state["outbox"] = outbox[1:]
        outbox = state["outbox"]
        save_state(base_dir, state)
    return 0


def validate_snapshot_uniqueness(snapshots: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for snapshot in snapshots:
        date_wib = snapshot["date_wib"]
        if date_wib in seen:
            raise DataIntegrityError(f"Duplicate portfolio snapshot date_wib: {date_wib}")
        seen.add(date_wib)


def validate_all(base_dir: Path = Path(".")) -> None:
    ensure_data_files(base_dir)
    events = read_ledger(base_dir)
    replay_events(events)
    state = load_state(base_dir)
    validate_state_shape(state)
    snapshots = read_snapshots(base_dir)
    validate_snapshot_uniqueness(snapshots)
    mstr_challenge.validate_all(base_dir)


def register_commands_cli() -> int:
    token, _ = telegram_credentials()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is required for register-commands")
    register_bot_commands(token)
    return 0


def challenge_init_cli(base_dir: Path, args: argparse.Namespace) -> int:
    amount = mstr_challenge.parse_owner_decimal(args.amount, "starting cash", allow_zero=True)
    legacy_position = replay_events(read_ledger(base_dir)).positions["MSTR"]
    include_legacy = bool(args.include_legacy_position)
    if include_legacy and legacy_position.quantity <= ZERO:
        raise mstr_challenge.ChallengeValidationError("No active legacy MSTR position is available to include")
    event, _ = mstr_challenge.initialize_challenge(
        base_dir,
        currency=args.currency,
        amount=amount,
        timestamp_utc=now_utc(),
        source=mstr_challenge.private_source(source="cli"),
        market=mstr_challenge.load_market_inputs(base_dir),
        include_legacy_position=include_legacy,
        opening_mstr_quantity=legacy_position.quantity if include_legacy else None,
        opening_mstr_average_cost=legacy_position.average_cost if include_legacy else None,
    )
    print(f"challenge initialized: {event['event_id']}")
    return 0


def export_public_cli(base_dir: Path) -> int:
    result = mstr_challenge.export_public(base_dir)
    print(f"public challenge export valid; changed={len(result['changed'])}")
    return 0


def sync_public_to_web(base_dir: Path) -> int:
    sync_url = os.environ.get("MSTR_WEB_SYNC_URL", "").strip()
    sync_token = os.environ.get("MSTR_WEB_SYNC_TOKEN", "").strip()
    if not sync_url or not sync_token:
        raise PortfolioError("MSTR_WEB_SYNC_URL and MSTR_WEB_SYNC_TOKEN are required")
    if not sync_url.startswith("https://"):
        raise PortfolioError("MSTR_WEB_SYNC_URL must use HTTPS")
    mstr_challenge.validate_all(base_dir, require_public=True)
    documents = {
        name: json.loads(mstr_challenge.public_path(base_dir, name).read_text(encoding="utf-8"))
        for name in mstr_challenge.PUBLIC_FILES
    }
    try:
        response = requests.post(
            sync_url,
            headers={
                "Authorization": f"Bearer {sync_token}",
                "Content-Type": "application/json",
                "User-Agent": "mstr-portfolio-publisher/1.0",
            },
            json={"documents": documents},
            timeout=WEB_SYNC_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PortfolioError(f"Website synchronization failed: {exc}") from exc
    if result.get("ok") is not True:
        raise PortfolioError("Website synchronization was rejected")
    print(f"website synchronized: {len(documents)} documents")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nevets personal portfolio worker")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--daily-report", action="store_true")
    prepare_parser.add_argument("--force-report", action="store_true")
    sub.add_parser("flush")
    sub.add_parser("validate")
    sub.add_parser("render")
    sub.add_parser("register-commands")
    challenge_init_parser = sub.add_parser("challenge-init")
    challenge_init_parser.add_argument("currency", choices=["USD", "IDR"])
    challenge_init_parser.add_argument("amount")
    initialization_mode = challenge_init_parser.add_mutually_exclusive_group(required=True)
    initialization_mode.add_argument("--start-empty", action="store_true")
    initialization_mode.add_argument("--include-legacy-position", action="store_true")
    challenge_status_parser = sub.add_parser("challenge-set-status")
    challenge_status_parser.add_argument("status", choices=["active", "paused", "completed"])
    challenge_reset_parser = sub.add_parser("challenge-reset")
    challenge_reset_parser.add_argument("--confirm", action="store_true", required=True)
    sub.add_parser("challenge-status")
    sub.add_parser("export-public")
    sub.add_parser("validate-public")
    sub.add_parser("sync-public")
    return parser.parse_args(argv)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    base_dir = Path(".")
    try:
        if args.command == "prepare":
            return prepare(base_dir=base_dir, daily_report=args.daily_report, force_report=args.force_report)
        if args.command == "flush":
            return flush(base_dir)
        if args.command == "validate":
            validate_all(base_dir)
            print("portfolio data valid")
            return 0
        if args.command == "render":
            print(render_portfolio_report(base_dir))
            return 0
        if args.command == "register-commands":
            return register_commands_cli()
        if args.command == "challenge-init":
            return challenge_init_cli(base_dir, args)
        if args.command == "challenge-set-status":
            updated = mstr_challenge.set_challenge_status(base_dir, args.status)
            print(f"challenge status: {updated['status']}")
            return 0
        if args.command == "challenge-reset":
            event, _ = mstr_challenge.reset_challenge(
                base_dir,
                timestamp_utc=now_utc(),
                source=mstr_challenge.private_source(source="cli"),
            )
            print(f"challenge reset: {event['event_id']}")
            return 0
        if args.command == "challenge-status":
            print(mstr_challenge.render_challenge_status(base_dir))
            return 0
        if args.command == "export-public":
            return export_public_cli(base_dir)
        if args.command == "validate-public":
            mstr_challenge.validate_all(base_dir, require_public=True)
            print("public challenge data valid")
            return 0
        if args.command == "sync-public":
            return sync_public_to_web(base_dir)
        raise PortfolioError(f"Unknown command {args.command}")
    except (PortfolioError, mstr_challenge.ChallengeError) as exc:
        print(f"portfolio error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
