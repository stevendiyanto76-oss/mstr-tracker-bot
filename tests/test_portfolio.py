from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import portfolio


UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def market(mstr: str = "112.53", btc: str = "64250", *, fresh: bool = True, stale: tuple[str, ...] = ()) -> portfolio.MarketData:
    return portfolio.MarketData(
        prices={"MSTR": Decimal(mstr), "BTC": Decimal(btc)},
        as_of={"MSTR": "2026-06-22T00:00:00+00:00", "BTC": "2026-06-22T00:00:00+00:00"},
        source="test",
        fresh=fresh,
        stale_assets=set(stale),
        warnings=[] if fresh else ["stale"],
    )


def jisdor(rate: str = "17718", *, fresh: bool = True) -> portfolio.JisdorData:
    return portfolio.JisdorData(Decimal(rate), "2026-06-22", fresh)


class PortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        portfolio.ensure_data_files(self.base)
        self.write_engine_state()
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_engine_state(self) -> None:
        payload = {
            "updated_at_utc": "2026-06-22T00:00:00+00:00",
            "last_action": "HOLD",
            "fingerprint": {"btc_holdings": 100, "basic_shares_m": 1},
            "zones": {
                "fair_price": 126.37,
                "strong_buy_price": 80,
                "accumulate_price": 100,
                "hold_price": 130,
                "reduce_price": 170,
            },
        }
        portfolio.atomic_write_json(self.base / portfolio.MSTR_ENGINE_STATE_FILE, payload)

    def test_us_equity_market_status_uses_new_york_session(self) -> None:
        status = portfolio.mstr_challenge.us_equity_market_status
        self.assertEqual("open", status(dt(2026, 6, 22, 14, 0), "2026-06-22T14:00:00+00:00"))
        self.assertEqual("closed", status(dt(2026, 6, 22, 21, 0), "2026-06-22T20:00:00+00:00"))
        self.assertEqual("closed", status(dt(2026, 6, 22, 14, 0), "2026-06-19T20:00:00+00:00"))

    def test_public_documents_sync_directly_to_website(self) -> None:
        documents = {}
        for name in portfolio.mstr_challenge.PUBLIC_FILES:
            payload = {"schema_version": 1, "generated_at": "2026-06-22T00:00:00+00:00", "name": name}
            portfolio.mstr_challenge.atomic_write_json(portfolio.mstr_challenge.public_path(self.base, name), payload)
            documents[name] = payload
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True}
        with (
            patch.dict(os.environ, {
                "MSTR_WEB_SYNC_URL": "https://example.test/api/mstr/sync",
                "MSTR_WEB_SYNC_TOKEN": "x" * 40,
            }),
            patch.object(portfolio.mstr_challenge, "validate_all"),
            patch("portfolio.requests.post", return_value=response) as post,
        ):
            self.assertEqual(portfolio.sync_public_to_web(self.base), 0)
        self.assertEqual(post.call_args.kwargs["json"], {"documents": documents})

    def events(self) -> list[dict[str, object]]:
        return portfolio.read_ledger(self.base)

    def positions(self) -> dict[str, portfolio.Position]:
        return portfolio.replay_events(self.events()).positions

    def trade(
        self,
        event_type: str,
        asset: str,
        quantity: str,
        price: str,
        *,
        update_id: int = 1,
        when: datetime | None = None,
    ) -> tuple[dict[str, object] | None, str, bool]:
        return portfolio.process_trade(
            self.base,
            self.events(),
            event_type=event_type,
            asset=asset,
            quantity=Decimal(quantity),
            price=Decimal(price),
            telegram_update_id=update_id,
            telegram_message_id=update_id + 100,
            chat_id="123",
            timestamp_utc=when or dt(2026, 6, 22, 2, 15),
        )

    def test_01_first_mstr_buy(self) -> None:
        event, _, mutated = self.trade("BUY", "MSTR", "10", "112.53")
        self.assertTrue(mutated)
        self.assertEqual(event["event_id"], "TX-000001")
        self.assertEqual(self.positions()["MSTR"].quantity, Decimal("10"))
        self.assertEqual(self.positions()["MSTR"].average_cost, Decimal("112.53"))

    def test_02_second_buy_weighted_average_cost(self) -> None:
        self.trade("BUY", "MSTR", "10", "100", update_id=1)
        self.trade("BUY", "MSTR", "5", "130", update_id=2)
        self.assertEqual(self.positions()["MSTR"].quantity, Decimal("15"))
        self.assertEqual(self.positions()["MSTR"].average_cost, Decimal("110"))

    def test_03_btc_precision(self) -> None:
        self.trade("BUY", "BTC", "0.12345678", "64250.12345678")
        self.assertEqual(self.positions()["BTC"].quantity, Decimal("0.12345678"))
        with self.assertRaises(portfolio.PortfolioValidationError):
            portfolio.parse_quantity("BTC", "0.123456789")

    def test_04_fractional_mstr(self) -> None:
        self.trade("BUY", "MSTR", "1.123456", "112")
        self.assertEqual(self.positions()["MSTR"].quantity, Decimal("1.123456"))
        with self.assertRaises(portfolio.PortfolioValidationError):
            portfolio.parse_quantity("MSTR", "1.1234567")

    def test_05_sell_partial(self) -> None:
        self.trade("BUY", "MSTR", "10", "100", update_id=1)
        self.trade("SELL", "MSTR", "4", "125", update_id=2)
        position = self.positions()["MSTR"]
        self.assertEqual(position.quantity, Decimal("6"))
        self.assertEqual(position.average_cost, Decimal("100"))

    def test_06_sell_all(self) -> None:
        self.trade("BUY", "BTC", "0.5", "60000", update_id=1)
        self.trade("SELL", "BTC", "0.5", "65000", update_id=2)
        position = self.positions()["BTC"]
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(position.average_cost, Decimal("0"))

    def test_07_oversell_rejection(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1)
        event, reply, mutated = self.trade("SELL", "MSTR", "2", "100", update_id=2)
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("PENJUALAN DITOLAK", reply)
        self.assertEqual(len(self.events()), 1)

    def test_08_bad_numeric_rejection(self) -> None:
        bad_values = ["0", "-1", "NaN", "Infinity", "1e2", "abc"]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(portfolio.PortfolioValidationError):
                    portfolio.parse_price(value)

    def test_09_comma_price_rejection(self) -> None:
        with self.assertRaises(portfolio.PortfolioValidationError):
            portfolio.parse_price("112,53")

    def test_10_extra_parameter_rejection(self) -> None:
        state = portfolio.load_state(self.base)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            self.events(),
            update_id=1,
            chat_id="123",
            message_id=1,
            message_timestamp_utc=dt(2026, 6, 22),
            text="/buy_mstr 1 100 extra",
        )
        self.assertFalse(mutated)
        self.assertIn("berlebih", reply)
        self.assertEqual(self.events(), [])

    def test_11_unsupported_asset_rejection(self) -> None:
        with self.assertRaises(portfolio.PortfolioValidationError):
            self.trade("BUY", "ETH", "1", "100")

    def test_12_duplicate_telegram_update_idempotency(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=7)
        _, reply, mutated = self.trade("BUY", "MSTR", "1", "100", update_id=7)
        self.assertFalse(mutated)
        self.assertIn("TRANSAKSI SUDAH TERCATAT", reply)
        self.assertIn("TX-000001", reply)
        self.assertEqual(len(self.events()), 1)

    def test_13_unauthorized_chat_ignored(self) -> None:
        state = portfolio.load_state(self.base)
        update = {"update_id": 10, "message": {"chat": {"id": 999}, "from": {"is_bot": False}, "text": "/buy", "date": 1}}
        with patch("portfolio.fetch_updates", return_value=[update]):
            mutated = portfolio.process_telegram_updates(self.base, state, "token", "123", dt(2026, 6, 22))
        self.assertFalse(mutated)
        self.assertEqual(state["last_update_id"], 10)
        self.assertEqual(self.events(), [])

    def test_14_telegram_message_timestamp_converted_to_wib(self) -> None:
        _, reply, _ = self.trade("BUY", "MSTR", "10", "112.53", when=dt(2026, 6, 22, 2, 15))
        self.assertIn("09:15 WIB", reply)

    def test_15_buy_guide(self) -> None:
        self.assertIn("/buy_mstr JUMLAH HARGA", portfolio.buy_guide())
        self.assertIn("/buy_btc 0.015 64250", portfolio.buy_guide())

    def test_16_missing_command_parameters_show_usage(self) -> None:
        state = portfolio.load_state(self.base)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            self.events(),
            update_id=1,
            chat_id="123",
            message_id=1,
            message_timestamp_utc=dt(2026, 6, 22),
            text="/buy_mstr",
        )
        self.assertFalse(mutated)
        self.assertIn("/buy_mstr JUMLAH HARGA", reply)

    def test_17_clear_all_warning_no_mutation(self) -> None:
        state = portfolio.load_state(self.base)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base, state, self.events(), update_id=1, chat_id="123", message_id=1, message_timestamp_utc=dt(2026, 6, 22), text="/clear_all"
        )
        self.assertFalse(mutated)
        self.assertIn("/clear_all CONFIRM", reply)
        self.assertEqual(self.events(), [])

    def test_18_lowercase_confirm_rejected(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base, state, self.events(), update_id=2, chat_id="123", message_id=1, message_timestamp_utc=dt(2026, 6, 22), text="/clear_all confirm"
        )
        self.assertFalse(mutated)
        self.assertIn("Konfirmasi tidak valid", reply)

    def test_19_uppercase_confirmation_creates_reset(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1)
        state = portfolio.load_state(self.base)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base, state, self.events(), update_id=2, chat_id="123", message_id=1, message_timestamp_utc=dt(2026, 6, 22), text="/clear_all CONFIRM"
        )
        self.assertTrue(mutated)
        self.assertIn("RESET-000001", reply)
        self.assertEqual(self.events()[-1]["event_type"], "RESET")

    def test_20_redundant_reset_rejected_empty(self) -> None:
        event, reply, mutated = portfolio.process_reset(
            self.base,
            self.events(),
            telegram_update_id=1,
            telegram_message_id=1,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("sudah kosong", reply)

    def test_21_reset_clears_positions(self) -> None:
        self.trade("BUY", "MSTR", "2", "100", update_id=1)
        portfolio.process_reset(self.base, self.events(), telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22))
        self.assertTrue(portfolio.position_is_empty(self.positions()))

    def test_22_undo_reset_restores_positions(self) -> None:
        self.trade("BUY", "MSTR", "2", "100", update_id=1)
        reset, _, _ = portfolio.process_reset(
            self.base, self.events(), telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22)
        )
        portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id=reset["event_id"],
            telegram_update_id=3,
            telegram_message_id=3,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertEqual(self.positions()["MSTR"].quantity, Decimal("2"))

    def test_23_undo_buy_that_makes_later_sell_invalid_rejected(self) -> None:
        buy, _, _ = self.trade("BUY", "MSTR", "10", "100", update_id=1)
        self.trade("SELL", "MSTR", "5", "110", update_id=2)
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id=buy["event_id"],
            telegram_update_id=3,
            telegram_message_id=3,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("UNDO DITOLAK", reply)
        self.assertIn("Portfolio tidak diubah", reply)

    def test_24_undo_eligible_id_from_last(self) -> None:
        buy, _, _ = self.trade("BUY", "MSTR", "1", "100", update_id=1)
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id=buy["event_id"],
            telegram_update_id=2,
            telegram_message_id=2,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertTrue(mutated)
        self.assertEqual(event["event_type"], "UNDO")
        self.assertIn(buy["event_id"], reply)

    def test_25_undone_events_hidden_from_last(self) -> None:
        buy, _, _ = self.trade("BUY", "MSTR", "1", "100", update_id=1)
        portfolio.process_undo(self.base, self.events(), target_event_id=buy["event_id"], telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22))
        self.assertNotIn(buy["event_id"], portfolio.format_last_events(self.events()))

    def test_26_malformed_ledger_line_hard_error(self) -> None:
        (self.base / portfolio.LEDGER_FILE).write_text("{bad\n", encoding="utf-8")
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.read_ledger(self.base)

    def test_27_missing_data_files_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            portfolio.ensure_data_files(base)
            self.assertTrue((base / portfolio.LEDGER_FILE).exists())
            self.assertTrue((base / portfolio.STATE_FILE).exists())
            self.assertTrue((base / portfolio.SNAPSHOT_FILE).exists())

    def test_28_atomic_state_writes(self) -> None:
        state = portfolio.load_state(self.base)
        state["last_update_id"] = 42
        portfolio.save_state(self.base, state)
        loaded = json.loads((self.base / portfolio.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(loaded["last_update_id"], 42)
        self.assertFalse(list((self.base / "data").glob("*.tmp")))

    def test_29_empty_portfolio_report(self) -> None:
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        self.assertIn("PORTOFOLIO MASIH KOSONG", report)

    def test_30_empty_asset_sections_hidden(self) -> None:
        self.trade("BUY", "BTC", "0.1", "60000")
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        self.assertIn("BTC POSITION", report)
        self.assertNotIn("MSTR POSITION", report)

    def test_31_idr_only_total_market_value_not_positions(self) -> None:
        self.trade("BUY", "BTC", "0.1", "60000")
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        position_section = report.split("₿ BTC POSITION", 1)[1]
        self.assertIn("Market Value: $6,425.00 | Rp113.838.150", report)
        self.assertNotIn("Rp", position_section)

    def test_32_report_under_3900_characters(self) -> None:
        self.trade("BUY", "MSTR", "25", "108.20", update_id=1)
        self.trade("BUY", "BTC", "0.15", "58200", update_id=2)
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        self.assertLessEqual(len(report), portfolio.REPORT_MAX_CHARS)

    def test_33_btc_via_mstr_formula(self) -> None:
        self.trade("BUY", "MSTR", "10", "100")
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        self.assertIn("BTC via MSTR: 0.001 BTC", report)

    def test_34_mstr_summary_approved_fields_only(self) -> None:
        self.trade("BUY", "MSTR", "10", "100")
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22), market_data=market(), jisdor=jisdor())
        self.assertIn("Current Zone:", report)
        self.assertIn("Fair Price:", report)
        self.assertIn("Strong Buy:", report)
        self.assertIn("Sell:", report)
        self.assertIn("Engine Action:", report)
        self.assertNotIn("Accumulate:", report)
        self.assertNotIn("Hold:", report)

    def test_35_market_cache_used_after_network_failure(self) -> None:
        state = portfolio.load_state(self.base)
        state["market_cache"] = {
            "MSTR": {"price_usd": "111", "as_of": "2026-06-21", "fetched_at_utc": "2026-06-21T00:00:00+00:00", "source": "test"},
            "BTC": {"price_usd": "64000", "as_of": "2026-06-21", "fetched_at_utc": "2026-06-21T00:00:00+00:00", "source": "test"},
        }
        with patch("portfolio.import_strategy_fetcher", side_effect=portfolio.MarketDataError("down")):
            data = portfolio.fetch_strategy_market_data(state, update_cache=True, current_time=dt(2026, 6, 22))
        self.assertFalse(data.fresh)
        self.assertEqual(data.prices["MSTR"], Decimal("111"))

    def test_36_stale_market_never_creates_alerts(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        alerts = portfolio.evaluate_pl_alerts(state, self.positions(), market("200", fresh=False, stale=("MSTR", "BTC")))
        self.assertEqual(alerts, [])

    def test_37_jisdor_parser_namespace_indonesian_format(self) -> None:
        xml = """
        <ns:DataSet xmlns:ns="urn:test">
          <ns:Row><ns:Tanggal>21/06/2026</ns:Tanggal><ns:Jisdor>Rp18.039,00</ns:Jisdor></ns:Row>
          <ns:Row><ns:Tanggal>22/06/2026</ns:Tanggal><ns:Jisdor>18.040,00</ns:Jisdor></ns:Row>
        </ns:DataSet>
        """
        rate, official_date = portfolio.parse_jisdor_xml(xml)
        self.assertEqual(rate, Decimal("18040.00"))
        self.assertEqual(official_date, "2026-06-22")

    def test_38_jisdor_cache_fallback(self) -> None:
        state = portfolio.load_state(self.base)
        state["jisdor_cache"] = {"rate": "17718", "official_date": "2026-06-20", "source": "Bank Indonesia"}
        with patch("portfolio.requests.get", side_effect=RuntimeError("down")):
            data = portfolio.fetch_jisdor(state, update_cache=True, current_time=dt(2026, 6, 22))
        self.assertFalse(data.fresh)
        self.assertEqual(data.rate, Decimal("17718"))

    def test_39_snapshot_uniqueness_by_wib_date(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market(), jisdor(), current_time=dt(2026, 6, 22, 0))
        portfolio.create_or_update_daily_snapshot(self.base, state, market("120"), jisdor(), current_time=dt(2026, 6, 22, 1))
        self.assertEqual(len(portfolio.read_snapshots(self.base)), 1)

    def test_40_same_day_snapshot_replacement(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market("110"), jisdor(), current_time=dt(2026, 6, 22, 0))
        portfolio.create_or_update_daily_snapshot(self.base, state, market("120"), jisdor(), current_time=dt(2026, 6, 22, 1))
        snapshot = portfolio.read_snapshots(self.base)[0]
        self.assertEqual(snapshot["raw"]["prices"]["MSTR"], "120")

    def test_41_buy_cash_flow_not_counted_as_return(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", when=dt(2026, 6, 22, 0))
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market("100"), jisdor(), current_time=dt(2026, 6, 22, 1))
        interval = portfolio.read_snapshots(self.base)[0]["derived"]["interval_return"]
        self.assertEqual(Decimal(interval), Decimal("0"))

    def test_42_modified_dietz_known_numerical_example(self) -> None:
        start = dt(2026, 1, 1)
        end = dt(2026, 1, 11)
        flow = portfolio.CashFlow(start + timedelta(days=5), Decimal("100"))
        result = portfolio.modified_dietz_return(Decimal("1000"), Decimal("1200"), [flow], start, end)
        self.assertEqual(result.quantize(Decimal("0.000001")), Decimal("0.095238"))

    def test_43_period_return_na_without_sufficient_history(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market("100"), jisdor(), current_time=dt(2026, 6, 22, 1))
        returns = portfolio.period_returns(self.base, portfolio.current_era_id(self.events()), dt(2026, 6, 22).date())
        self.assertIsNone(returns["1D"])

    def test_44_reset_begins_new_performance_era(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1)
        reset, _, _ = portfolio.process_reset(self.base, self.events(), telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22, 1))
        self.trade("BUY", "BTC", "0.1", "60000", update_id=3, when=dt(2026, 6, 22, 2))
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market(), jisdor(), current_time=dt(2026, 6, 22, 3))
        self.assertEqual(portfolio.read_snapshots(self.base)[0]["era_id"], reset["event_id"])

    def test_45_undo_rebuilds_historical_snapshot_derived_values(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1, when=dt(2026, 6, 22, 0))
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market("100"), jisdor(), current_time=dt(2026, 6, 22, 2))
        buy2, _, _ = self.trade("BUY", "MSTR", "1", "100", update_id=2, when=dt(2026, 6, 22, 1))
        self.assertEqual(portfolio.read_snapshots(self.base)[0]["derived"]["quantities"]["MSTR"], "2")
        portfolio.process_undo(self.base, self.events(), target_event_id=buy2["event_id"], telegram_update_id=3, telegram_message_id=3, chat_id="123", timestamp_utc=dt(2026, 6, 22, 3))
        self.assertEqual(portfolio.read_snapshots(self.base)[0]["derived"]["quantities"]["MSTR"], "1")

    def test_46_one_combined_alert_for_multiple_crossed_thresholds(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        alerts = portfolio.evaluate_pl_alerts(state, self.positions(), market("320"))
        mstr_alerts = [alert for alert in alerts if "MSTR PROFIT" in alert]
        self.assertEqual(len(mstr_alerts), 1)
        self.assertIn("+50%", mstr_alerts[0])
        self.assertIn("+100%", mstr_alerts[0])
        self.assertIn("+200%", mstr_alerts[0])

    def test_47_threshold_rearms_after_exit_and_reentry(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        self.assertTrue(portfolio.evaluate_pl_alerts(state, self.positions(), market("160")))
        self.assertFalse(portfolio.evaluate_pl_alerts(state, self.positions(), market("140")))
        alerts = portfolio.evaluate_pl_alerts(state, self.positions(), market("160"))
        self.assertTrue(alerts)
        self.assertIn("+50%", alerts[0])

    def test_48_transaction_mutation_establishes_no_alert_baseline(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        portfolio.establish_alert_baseline(state, self.positions(), market("200"))
        self.assertEqual(portfolio.evaluate_pl_alerts(state, self.positions(), market("200")), [])

    def test_49_mstr_zone_alert_only_strong_buy_and_sell(self) -> None:
        state = portfolio.load_state(self.base)
        positions = self.positions()
        self.assertEqual(portfolio.evaluate_mstr_zone_alert(self.base, state, positions, market("120")), [])
        self.assertTrue(portfolio.evaluate_mstr_zone_alert(self.base, state, positions, market("50")))
        self.assertEqual(portfolio.evaluate_mstr_zone_alert(self.base, state, positions, market("90")), [])
        self.assertTrue(portfolio.evaluate_mstr_zone_alert(self.base, state, positions, market("180")))

    def test_50_outbox_item_remains_after_send_failure(self) -> None:
        state = portfolio.load_state(self.base)
        portfolio.enqueue_outbox(state, item_id="x", chat_id="123", text="hello", category="reply", created_at_utc=dt(2026, 6, 22))
        portfolio.save_state(self.base, state)
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        with patch("portfolio.telegram_post", side_effect=portfolio.TelegramError("down")):
            self.assertEqual(portfolio.flush(self.base), 1)
        self.assertEqual(len(portfolio.load_state(self.base)["outbox"]), 1)

    def test_51_sent_outbox_item_removed(self) -> None:
        state = portfolio.load_state(self.base)
        portfolio.enqueue_outbox(state, item_id="x", chat_id="123", text="hello", category="reply", created_at_utc=dt(2026, 6, 22))
        portfolio.save_state(self.base, state)
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        with patch("portfolio.telegram_post", return_value={"message_id": 1}):
            self.assertEqual(portfolio.flush(self.base), 0)
        self.assertEqual(portfolio.load_state(self.base)["outbox"], [])

    def test_52_update_offset_advances_for_ignored_updates(self) -> None:
        state = portfolio.load_state(self.base)
        update = {"update_id": 44, "channel_post": {"text": "/buy"}}
        with patch("portfolio.fetch_updates", return_value=[update]):
            portfolio.process_telegram_updates(self.base, state, "token", "123", dt(2026, 6, 22))
        self.assertEqual(state["last_update_id"], 44)

    def test_53_bot_menu_contains_required_commands(self) -> None:
        captured: dict[str, object] = {}

        def fake_post(token: str, method: str, *, json_payload: dict[str, object]) -> object:
            captured["method"] = method
            captured["payload"] = json_payload
            return {}

        with patch("portfolio.telegram_post", side_effect=fake_post):
            portfolio.register_bot_commands("token")
        commands = [item["command"] for item in captured["payload"]["commands"]]
        self.assertEqual(commands, [command for command, _ in portfolio.COMMAND_MENU])

    def test_54_state_containing_token_secret_is_never_written(self) -> None:
        state = portfolio.load_state(self.base)
        state["telegram_bot_token"] = "secret"
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.save_state(self.base, state)

    def test_55_validate_catches_duplicate_snapshot_dates(self) -> None:
        duplicate = {
            "schema_version": 1,
            "date_wib": "2026-06-22",
            "captured_at_utc": "2026-06-21T17:00:00+00:00",
            "captured_at_wib": "2026-06-22T00:00:00+07:00",
            "era_id": "ERA-000000",
            "raw": {"prices": {"MSTR": "100", "BTC": "60000"}, "jisdor": {"rate": "17718"}},
            "derived": {"cost_basis": "0", "market_value": "0", "net_external_flow": "0", "performance_index": "100"},
        }
        text = "\n".join(json.dumps(duplicate) for _ in range(2)) + "\n"
        (self.base / portfolio.SNAPSHOT_FILE).write_text(text, encoding="utf-8")
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.validate_all(self.base)

    def test_56_validate_catches_invalid_decimal_fields(self) -> None:
        state = portfolio.load_state(self.base)
        state["market_cache"] = {"MSTR": {"price_usd": "1e2"}}
        portfolio.atomic_write_json(self.base / portfolio.STATE_FILE, state)
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.validate_all(self.base)

    def test_strategy_snapshot_fetch_is_mocked_not_live(self) -> None:
        state = portfolio.load_state(self.base)
        snapshot = SimpleNamespace(
            mstr_price=123.45,
            btc_price=65432.10,
            source_metadata=SimpleNamespace(mstr_as_of=dt(2026, 6, 22), btc_as_of=dt(2026, 6, 22)),
        )
        with patch("portfolio.import_strategy_fetcher", return_value=Mock(return_value=snapshot)):
            data = portfolio.fetch_strategy_market_data(state, update_cache=True, current_time=dt(2026, 6, 22))
        self.assertTrue(data.fresh)
        self.assertEqual(data.prices["MSTR"], Decimal("123.45"))

    def test_57_oversell_update_advances_offset_and_later_update_processes(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1)
        state = portfolio.load_state(self.base)
        updates = [
            {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 123}, "from": {"is_bot": False}, "date": int(dt(2026, 6, 22).timestamp()), "text": "/sell_mstr 2 100"}},
            {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 123}, "from": {"is_bot": False}, "date": int(dt(2026, 6, 22).timestamp()), "text": "/buy_btc 0.1 60000"}},
        ]
        with patch("portfolio.fetch_updates", return_value=updates):
            mutated = portfolio.process_telegram_updates(self.base, state, "token", "123", dt(2026, 6, 22))
        self.assertTrue(mutated)
        self.assertEqual(state["last_update_id"], 3)
        self.assertEqual(len(self.events()), 2)
        replies = "\n".join(item["text"] for item in state["outbox"])
        self.assertIn("PENJUALAN DITOLAK", replies)
        self.assertIn("BUY BTC TERCATAT", replies)

    def test_58_missing_outbox_self_heals_to_empty(self) -> None:
        raw = portfolio.default_state()
        raw.pop("outbox")
        portfolio.atomic_write_json(self.base / portfolio.STATE_FILE, raw)
        self.assertEqual(portfolio.load_state(self.base)["outbox"], [])

    def test_59_existing_wrong_typed_outbox_raises_and_preserves_file(self) -> None:
        raw = portfolio.default_state()
        raw["outbox"] = {"id": "unsent"}
        portfolio.atomic_write_json(self.base / portfolio.STATE_FILE, raw)
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.load_state(self.base)
        persisted = json.loads((self.base / portfolio.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(persisted["outbox"], {"id": "unsent"})

    def test_60_malformed_alert_state_raises(self) -> None:
        raw = portfolio.default_state()
        raw["alert_state"]["MSTR"]["positive"] = "50"
        portfolio.atomic_write_json(self.base / portfolio.STATE_FILE, raw)
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.load_state(self.base)

    def test_61_invalid_fresh_market_prices_do_not_overwrite_cache(self) -> None:
        invalid_values = ["NaN", "Infinity", "0", "-1"]
        for value in invalid_values:
            with self.subTest(value=value):
                state = portfolio.default_state()
                state["market_cache"] = {
                    "MSTR": {"price_usd": "111", "as_of": "old", "fetched_at_utc": "2026-06-21T00:00:00+00:00", "source": "test"},
                    "BTC": {"price_usd": "64000", "as_of": "old", "fetched_at_utc": "2026-06-21T00:00:00+00:00", "source": "test"},
                }
                snapshot = SimpleNamespace(mstr_price=value, btc_price="64000", source_metadata=SimpleNamespace(mstr_as_of=None, btc_as_of=None))
                with patch("portfolio.import_strategy_fetcher", return_value=Mock(return_value=snapshot)):
                    data = portfolio.fetch_strategy_market_data(state, update_cache=True, current_time=dt(2026, 6, 22))
                self.assertFalse(data.fresh)
                self.assertEqual(state["market_cache"]["MSTR"]["price_usd"], "111")
                self.assertEqual(data.prices["MSTR"], Decimal("111"))

    def test_62_invalid_fresh_market_without_cache_creates_no_alert_or_snapshot(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        snapshot = SimpleNamespace(mstr_price="NaN", btc_price="64000", source_metadata=SimpleNamespace(mstr_as_of=None, btc_as_of=None))
        with patch("portfolio.import_strategy_fetcher", return_value=Mock(return_value=snapshot)):
            data = portfolio.fetch_strategy_market_data(state, update_cache=True, current_time=dt(2026, 6, 22))
        self.assertFalse(data.fresh)
        self.assertEqual(data.prices, {})
        self.assertEqual(portfolio.evaluate_pl_alerts(state, self.positions(), data), [])
        self.assertFalse(portfolio.create_or_update_daily_snapshot(self.base, state, data, jisdor(), current_time=dt(2026, 6, 22)))
        self.assertEqual(portfolio.read_snapshots(self.base), [])

    def test_63_jisdor_dataset_usd_row_supported_and_non_usd_ignored(self) -> None:
        xml = """
        <DataSet xmlns="urn:bi">
          <diffgr:diffgram xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1">
            <NewDataSet>
              <Table>
                <tgl_subkursasing>2026-06-22T00:00:00+07:00</tgl_subkursasing>
                <jual_subkursasing>19.999,00</jual_subkursasing>
                <mts_subkursasing>EUR</mts_subkursasing>
              </Table>
              <Table>
                <tgl_subkursasing>2026-06-22T00:00:00+07:00</tgl_subkursasing>
                <jual_subkursasing>16,345.00</jual_subkursasing>
                <mts_subkursasing>USD</mts_subkursasing>
              </Table>
            </NewDataSet>
          </diffgr:diffgram>
        </DataSet>
        """
        rate, official_date = portfolio.parse_jisdor_xml(xml)
        self.assertEqual(rate, Decimal("16345.00"))
        self.assertEqual(official_date, "2026-06-22")

    def test_64_bi_decimal_formats(self) -> None:
        cases = {
            "18.039,00": Decimal("18039.00"),
            "16,345.00": Decimal("16345.00"),
            "18039.00": Decimal("18039.00"),
            "18039": Decimal("18039"),
            "Rp18.039,00": Decimal("18039.00"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(portfolio.parse_bi_decimal(text), expected)

    def test_65_jisdor_request_headers(self) -> None:
        state = portfolio.load_state(self.base)
        response = Mock()
        response.text = "<DataSet><Row><tanggal>2026-06-22</tanggal><jisdor>18039</jisdor></Row></DataSet>"
        response.raise_for_status.return_value = None
        with patch("portfolio.requests.get", return_value=response) as get:
            data = portfolio.fetch_jisdor(state, update_cache=True, current_time=dt(2026, 6, 22))
        self.assertTrue(data.fresh)
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], portfolio.HTTP_TIMEOUT)
        self.assertIn("application/xml", kwargs["headers"]["Accept"])
        self.assertIn("nevets-portfolio-worker", kwargs["headers"]["User-Agent"])

    def test_66_daily_report_not_marked_sent_when_price_missing(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        stale_missing = portfolio.MarketData({}, {}, "cache", False, set(), ["missing"])
        with patch("portfolio.register_bot_commands"), patch("portfolio.fetch_updates", return_value=[]), patch("portfolio.fetch_strategy_market_data", return_value=stale_missing), patch("portfolio.fetch_jisdor", return_value=jisdor()):
            portfolio.prepare(base_dir=self.base, daily_report=True, current_time=dt(2026, 6, 22))
        state = portfolio.load_state(self.base)
        self.assertIsNone(state["last_daily_report_date_wib"])
        self.assertEqual(state["outbox"], [])
        self.assertEqual(portfolio.read_snapshots(self.base), [])

    def test_67_portofolio_uses_current_ephemeral_performance(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=1, when=dt(2026, 6, 21, 0))
        state = portfolio.load_state(self.base)
        portfolio.create_or_update_daily_snapshot(self.base, state, market("100"), jisdor(), current_time=dt(2026, 6, 21, 1))
        report = portfolio.render_portfolio_report(self.base, now=dt(2026, 6, 22, 1), market_data=market("120"), jisdor=jisdor())
        self.assertIn("1D: +20.00%", report)
        self.assertIn("Since Inception: +20.00%", report)

    def test_68_mutation_with_unavailable_market_sets_pending_then_fresh_baselines(self) -> None:
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 123}, "from": {"is_bot": False}, "date": int(dt(2026, 6, 22).timestamp()), "text": "/buy_mstr 1 100"}}
        unavailable = portfolio.MarketData({}, {}, "cache", False, set(), ["missing"])
        with patch("portfolio.register_bot_commands"), patch("portfolio.fetch_updates", return_value=[update]), patch("portfolio.fetch_strategy_market_data", return_value=unavailable):
            portfolio.prepare(base_dir=self.base, current_time=dt(2026, 6, 22))
        self.assertTrue(portfolio.load_state(self.base)["alert_baseline_pending"])
        with patch("portfolio.fetch_updates", return_value=[]), patch("portfolio.fetch_strategy_market_data", return_value=market("150")):
            portfolio.prepare(base_dir=self.base, current_time=dt(2026, 6, 22, 1))
        state = portfolio.load_state(self.base)
        self.assertFalse(state["alert_baseline_pending"])
        self.assertEqual([item for item in state["outbox"] if item["category"] == "alert"], [])
        self.assertIn("50", state["alert_state"]["MSTR"]["positive"])

    def test_69_duplicate_update_reconstructs_informative_reply(self) -> None:
        self.trade("BUY", "MSTR", "1", "100", update_id=5)
        state = portfolio.load_state(self.base)
        state["last_update_id"] = 4
        update = {"update_id": 5, "message": {"message_id": 5, "chat": {"id": 123}, "from": {"is_bot": False}, "date": int(dt(2026, 6, 22).timestamp()), "text": "/buy_mstr 1 100"}}
        with patch("portfolio.fetch_updates", return_value=[update]):
            mutated = portfolio.process_telegram_updates(self.base, state, "token", "123", dt(2026, 6, 22))
        self.assertFalse(mutated)
        self.assertEqual(state["last_update_id"], 5)
        self.assertEqual(len(self.events()), 1)
        self.assertIn("TRANSAKSI SUDAH TERCATAT", state["outbox"][0]["text"])
        self.assertIn("TX-000001", state["outbox"][0]["text"])
        self.assertIn("BUY MSTR: 1 lembar @ $100.00", state["outbox"][0]["text"])

    def test_70_integrity_rules_for_ledger_metadata(self) -> None:
        event = portfolio.make_event(
            [],
            "BUY",
            asset="MSTR",
            quantity=Decimal("1"),
            price_usd=Decimal("100"),
            target_event_id=None,
            telegram_update_id=1,
            telegram_message_id=1,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        cases = [
            ("telegram_update_id", -1),
            ("telegram_message_id", "1"),
            ("chat_id", ""),
            ("timestamp_wib", "2026-06-22T00:00:00+00:00"),
            ("timestamp_wib", "2026-06-22T08:00:00+07:00"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                bad = dict(event)
                bad[field] = value
                (self.base / portfolio.LEDGER_FILE).write_text(json.dumps(bad) + "\n", encoding="utf-8")
                with self.assertRaises(portfolio.DataIntegrityError):
                    portfolio.validate_all(self.base)
        dup = dict(event)
        dup["event_id"] = "TX-000002"
        (self.base / portfolio.LEDGER_FILE).write_text(json.dumps(event) + "\n" + json.dumps(dup) + "\n", encoding="utf-8")
        with self.assertRaises(portfolio.DataIntegrityError):
            portfolio.validate_all(self.base)

    def test_71_integrity_rules_for_state_fields(self) -> None:
        cases = [
            ("outbox", [{"id": "x", "chat_id": "123", "text": "a", "created_at_utc": "2026-06-22T00:00:00+00:00", "category": "reply"}, {"id": "x", "chat_id": "123", "text": "b", "created_at_utc": "2026-06-22T00:00:00+00:00", "category": "reply"}]),
            ("alert_state", {**portfolio.default_state()["alert_state"], "MSTR": {"positive": ["999"], "negative": []}}),
            ("alert_state", {**portfolio.default_state()["alert_state"], "mstr_zone": "WAIT"}),
            ("last_daily_report_date_wib", "2026-99-99"),
            ("bot_commands_registered", "false"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                raw = portfolio.default_state()
                raw[field] = value
                portfolio.atomic_write_json(self.base / portfolio.STATE_FILE, raw)
                with self.assertRaises(portfolio.DataIntegrityError):
                    portfolio.validate_all(self.base)

    def test_72_integrity_rules_for_snapshot_fields(self) -> None:
        base_snapshot = {
            "schema_version": 1,
            "date_wib": "2026-06-22",
            "captured_at_utc": "2026-06-21T17:00:00+00:00",
            "captured_at_wib": "2026-06-22T00:00:00+07:00",
            "era_id": "ERA-000000",
            "raw": {"prices": {"MSTR": "100", "BTC": "60000"}, "jisdor": {"rate": "17718"}},
            "derived": {"cost_basis": "0", "market_value": "0", "net_external_flow": "0", "performance_index": "100"},
        }
        cases = [
            ("captured_at_wib", "2026-06-21T23:00:00+07:00"),
            ("raw", {"prices": {"MSTR": "0", "BTC": "60000"}, "jisdor": {"rate": "17718"}}),
            ("raw", {"prices": {"MSTR": "100", "BTC": "60000"}, "jisdor": {"rate": "-1"}}),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                snapshot = dict(base_snapshot)
                snapshot[field] = value
                (self.base / portfolio.SNAPSHOT_FILE).write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
                with self.assertRaises(portfolio.DataIntegrityError):
                    portfolio.validate_all(self.base)

    def test_73_loss_alert_headline_uses_deepest_crossed_threshold(self) -> None:
        self.trade("BUY", "MSTR", "1", "100")
        state = portfolio.load_state(self.base)
        alerts = portfolio.evaluate_pl_alerts(state, self.positions(), market("45"))
        mstr_alert = next(alert for alert in alerts if "MSTR LOSS" in alert)
        self.assertIn("MSTR LOSS -50.00%", mstr_alert)
        self.assertIn("-50%", mstr_alert)
        self.assertIn("-25%", mstr_alert)

    def test_74_history_default_shows_20_active_newest(self) -> None:
        for index in range(25):
            self.trade("BUY", "MSTR", "1", "100", update_id=index + 1)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base,
            portfolio.load_state(self.base),
            self.events(),
            update_id=100,
            chat_id="123",
            message_id=100,
            message_timestamp_utc=dt(2026, 6, 22),
            text="/history",
        )
        self.assertFalse(mutated)
        self.assertEqual(reply.count(" | BUY MSTR | "), 20)
        self.assertIn("TX-000025", reply)
        self.assertIn("TX-000006", reply)
        self.assertNotIn("TX-000005", reply)

    def test_75_history_50_shows_maximum_50_active_newest(self) -> None:
        for index in range(60):
            self.trade("BUY", "MSTR", "1", "100", update_id=index + 1)
        reply, mutated = portfolio.handle_authorized_text_command(
            self.base,
            portfolio.load_state(self.base),
            self.events(),
            update_id=100,
            chat_id="123",
            message_id=100,
            message_timestamp_utc=dt(2026, 6, 22),
            text="/history 50",
        )
        self.assertFalse(mutated)
        self.assertEqual(reply.count(" | BUY MSTR | "), 50)
        self.assertIn("TX-000060", reply)
        self.assertIn("TX-000011", reply)
        self.assertNotIn("TX-000010", reply)

    def test_76_history_invalid_limits_show_usage(self) -> None:
        for text in ("/history 0", "/history abc", "/history 101"):
            with self.subTest(text=text):
                reply, mutated = portfolio.handle_authorized_text_command(
                    self.base,
                    portfolio.load_state(self.base),
                    self.events(),
                    update_id=100,
                    chat_id="123",
                    message_id=100,
                    message_timestamp_utc=dt(2026, 6, 22),
                    text=text,
                )
                self.assertFalse(mutated)
                self.assertIn("/history N", reply)
                self.assertIn("1 sampai 100", reply)

    def test_77_undo_old_active_event_not_in_last_succeeds(self) -> None:
        for index in range(6):
            self.trade("BUY", "MSTR", "1", "100", update_id=index + 1)
        self.assertNotIn("TX-000001", portfolio.format_last_events(self.events()))
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id="TX-000001",
            telegram_update_id=100,
            telegram_message_id=100,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertTrue(mutated)
        self.assertEqual(event["event_type"], "UNDO")
        self.assertIn("TX-000001", reply)
        self.assertEqual(self.positions()["MSTR"].quantity, Decimal("5"))

    def test_78_undo_old_event_rejected_if_replay_oversells(self) -> None:
        self.trade("BUY", "MSTR", "10", "100", update_id=1)
        self.trade("SELL", "MSTR", "5", "110", update_id=2)
        for index in range(5):
            self.trade("BUY", "BTC", "0.01", "60000", update_id=index + 3)
        self.assertNotIn("TX-000001", portfolio.format_last_events(self.events()))
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id="TX-000001",
            telegram_update_id=100,
            telegram_message_id=100,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("UNDO DITOLAK", reply)
        self.assertEqual(len(self.events()), 7)

    def test_79_undo_already_undone_id_rejected(self) -> None:
        buy, _, _ = self.trade("BUY", "MSTR", "1", "100", update_id=1)
        portfolio.process_undo(self.base, self.events(), target_event_id=buy["event_id"], telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22))
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id=buy["event_id"],
            telegram_update_id=3,
            telegram_message_id=3,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("sudah tidak aktif", reply)
        self.assertNotIn(buy["event_id"], portfolio.format_history_events(self.events()))

    def test_80_undo_event_undo_rejected(self) -> None:
        buy, _, _ = self.trade("BUY", "MSTR", "1", "100", update_id=1)
        undo, _, _ = portfolio.process_undo(self.base, self.events(), target_event_id=buy["event_id"], telegram_update_id=2, telegram_message_id=2, chat_id="123", timestamp_utc=dt(2026, 6, 22))
        event, reply, mutated = portfolio.process_undo(
            self.base,
            self.events(),
            target_event_id=undo["event_id"],
            telegram_update_id=3,
            telegram_message_id=3,
            chat_id="123",
            timestamp_utc=dt(2026, 6, 22),
        )
        self.assertIsNone(event)
        self.assertFalse(mutated)
        self.assertIn("sudah tidak aktif", reply)

    def test_81_history_in_help_and_menu(self) -> None:
        self.assertIn("/history", portfolio.help_text())
        commands = [command for command, _ in portfolio.COMMAND_MENU]
        self.assertIn("history", commands)

    def test_82_legacy_menu_state_migrates_to_empty_fingerprint(self) -> None:
        path = self.base / portfolio.STATE_FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("bot_commands_fingerprint", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(portfolio.load_state(self.base)["bot_commands_fingerprint"], "")

    def test_83_menu_registration_is_fingerprint_driven(self) -> None:
        state = portfolio.load_state(self.base)
        with patch("portfolio.register_bot_commands") as register:
            self.assertTrue(portfolio.ensure_bot_commands_registered(state, "token"))
            self.assertFalse(portfolio.ensure_bot_commands_registered(state, "token"))
        register.assert_called_once_with("token")
        self.assertEqual(state["bot_commands_fingerprint"], portfolio.bot_commands_fingerprint())

    def test_84_menu_fingerprint_changes_with_contract(self) -> None:
        changed = [*portfolio.COMMAND_MENU, ("future_command", "Future command")]
        self.assertNotEqual(portfolio.bot_commands_fingerprint(), portfolio.bot_commands_fingerprint(changed))


if __name__ == "__main__":
    unittest.main()
