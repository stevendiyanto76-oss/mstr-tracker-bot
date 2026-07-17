from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import challenge
import portfolio


UTC = timezone.utc


def at(day: int = 1, hour: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


def market(
    *,
    mstr: str = "100",
    btc: str = "50000",
    fx: str = "16000",
    captured_at: datetime | None = None,
) -> challenge.MarketInputs:
    current = captured_at or at()
    stamp = challenge.iso_seconds(current)
    return challenge.MarketInputs(
        mstr_price=Decimal(mstr),
        btc_price=Decimal(btc),
        usd_idr=Decimal(fx),
        mstr_as_of=current.date().isoformat(),
        btc_as_of=current.date().isoformat(),
        fx_as_of=current.date().isoformat(),
        mstr_source="test_mstr",
        btc_source="test_btc",
        fx_source="test_jisdor",
        fetched_at=stamp,
        freshness="fresh",
        market_status="open",
    )


def source(update_id: int | None = None) -> dict[str, object]:
    return challenge.private_source(
        telegram_update_id=update_id,
        telegram_message_id=update_id + 100 if update_id is not None else None,
        chat_id="private-owner",
        source="test",
    )


class ChallengeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        challenge.ensure_challenge_files(self.base)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def initialize(
        self,
        amount: str = "1000",
        *,
        currency: str = "USD",
        when: datetime | None = None,
        update_id: int = 1,
        include_legacy_position: bool = False,
        opening_quantity: str | None = None,
        opening_cost: str | None = None,
    ) -> challenge.ChallengeState:
        _, state = challenge.initialize_challenge(
            self.base,
            currency=currency,
            amount=Decimal(amount),
            timestamp_utc=when or at(),
            source=source(update_id),
            market=market(captured_at=when or at()),
            include_legacy_position=include_legacy_position,
            opening_mstr_quantity=Decimal(opening_quantity) if opening_quantity else None,
            opening_mstr_average_cost=Decimal(opening_cost) if opening_cost else None,
        )
        return state

    def test_default_configuration_is_prelaunch_without_capital(self) -> None:
        config = challenge.load_config(self.base)
        self.assertEqual(config["status"], "prelaunch")
        self.assertIsNone(config["start_at_utc"])
        self.assertIsNone(config["starting_event_id"])
        self.assertEqual(challenge.read_events(self.base), [])

    def test_empty_start_is_explicit_and_valid(self) -> None:
        state = self.initialize("0")
        self.assertTrue(state.initialized)
        self.assertEqual(state.cash["USD"], Decimal("0"))
        self.assertEqual(challenge.load_config(self.base)["status"], "active")

    def test_legacy_position_cost_is_counted_as_starting_capital(self) -> None:
        state = self.initialize(
            "100",
            include_legacy_position=True,
            opening_quantity="2",
            opening_cost="50",
        )
        self.assertEqual(state.net_contributions_usd, Decimal("200"))
        self.assertEqual(state.position.quantity, Decimal("2"))
        self.assertEqual(state.position.average_cost, Decimal("50"))

    def test_legacy_position_requires_complete_positive_values(self) -> None:
        with self.assertRaises(challenge.ChallengeValidationError):
            self.initialize("100", include_legacy_position=True, opening_quantity="2")

    def test_second_initialization_is_rejected(self) -> None:
        self.initialize()
        with self.assertRaises(challenge.ChallengeValidationError):
            self.initialize(update_id=2)

    def test_usd_deposit_and_withdrawal_update_cash_and_contributions(self) -> None:
        self.initialize()
        challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("250"),
            timestamp_utc=at(2),
            source=source(2),
        )
        _, state, _ = challenge.record_cash_event(
            self.base,
            event_type="WITHDRAWAL",
            currency="USD",
            amount=Decimal("100"),
            timestamp_utc=at(3),
            source=source(3),
        )
        self.assertEqual(state.cash["USD"], Decimal("1150"))
        self.assertEqual(state.net_contributions_usd, Decimal("1150"))

    def test_idr_deposit_uses_timestamped_fx_for_contribution_value(self) -> None:
        self.initialize("0")
        _, state, _ = challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="IDR",
            amount=Decimal("1600000"),
            timestamp_utc=at(2),
            source=source(2),
            market=market(fx="16000", captured_at=at(2)),
        )
        self.assertEqual(state.cash["IDR"], Decimal("1600000"))
        self.assertEqual(state.net_contributions_usd, Decimal("100"))

    def test_withdrawal_cannot_exceed_cash(self) -> None:
        self.initialize("100")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_cash_event(
                self.base,
                event_type="WITHDRAWAL",
                currency="USD",
                amount=Decimal("101"),
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_fx_conversion_uses_actual_amounts(self) -> None:
        self.initialize("160000", currency="IDR")
        _, state, _ = challenge.record_fx_conversion(
            self.base,
            from_currency="IDR",
            from_amount=Decimal("160000"),
            to_currency="USD",
            to_amount=Decimal("10"),
            timestamp_utc=at(2),
            source=source(2),
        )
        self.assertEqual(state.cash["IDR"], Decimal("0"))
        self.assertEqual(state.cash["USD"], Decimal("10"))

    def test_fx_conversion_cannot_exceed_source_cash(self) -> None:
        self.initialize("100", currency="IDR")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_fx_conversion(
                self.base,
                from_currency="IDR",
                from_amount=Decimal("101"),
                to_currency="USD",
                to_amount=Decimal("1"),
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_buy_uses_cash_and_weighted_average_cost(self) -> None:
        self.initialize("1000")
        challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("2"),
            price_usd=Decimal("100"),
            timestamp_utc=at(2),
            source=source(2),
        )
        _, state, _ = challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("2"),
            price_usd=Decimal("120"),
            timestamp_utc=at(3),
            source=source(3),
        )
        self.assertEqual(state.cash["USD"], Decimal("560"))
        self.assertEqual(state.position.quantity, Decimal("4"))
        self.assertEqual(state.position.average_cost, Decimal("110"))

    def test_buy_cannot_exceed_available_cash(self) -> None:
        self.initialize("100")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_trade(
                self.base,
                event_type="BUY",
                quantity=Decimal("2"),
                price_usd=Decimal("100"),
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_sell_releases_cash_and_realizes_profit(self) -> None:
        self.initialize("1000")
        challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("5"),
            price_usd=Decimal("100"),
            timestamp_utc=at(2),
            source=source(2),
        )
        _, state, _ = challenge.record_trade(
            self.base,
            event_type="SELL",
            quantity=Decimal("2"),
            price_usd=Decimal("125"),
            timestamp_utc=at(3),
            source=source(3),
        )
        self.assertEqual(state.cash["USD"], Decimal("750"))
        self.assertEqual(state.position.quantity, Decimal("3"))
        self.assertEqual(state.realized_pl_usd, Decimal("50"))

    def test_sell_cannot_exceed_position(self) -> None:
        self.initialize("1000")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_trade(
                self.base,
                event_type="SELL",
                quantity=Decimal("1"),
                price_usd=Decimal("100"),
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_fee_and_tax_reduce_cash_and_realized_result(self) -> None:
        self.initialize("100")
        challenge.record_cash_event(
            self.base,
            event_type="FEE",
            currency="USD",
            amount=Decimal("2"),
            timestamp_utc=at(2),
            source=source(2),
        )
        _, state, _ = challenge.record_cash_event(
            self.base,
            event_type="TAX",
            currency="USD",
            amount=Decimal("3"),
            timestamp_utc=at(3),
            source=source(3),
        )
        self.assertEqual(state.cash["USD"], Decimal("95"))
        self.assertEqual(state.fees["USD"], Decimal("2"))
        self.assertEqual(state.taxes["USD"], Decimal("3"))
        self.assertEqual(state.realized_pl_usd, Decimal("-5"))

    def test_duplicate_telegram_update_is_idempotent(self) -> None:
        self.initialize("100")
        first, _, first_mutated = challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("10"),
            timestamp_utc=at(2),
            source=source(2),
        )
        second, state, second_mutated = challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("999"),
            timestamp_utc=at(3),
            source=source(2),
        )
        self.assertTrue(first_mutated)
        self.assertFalse(second_mutated)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(state.cash["USD"], Decimal("110"))

    def test_safe_undo_replays_the_ledger(self) -> None:
        self.initialize("100")
        event, _, _ = challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("25"),
            timestamp_utc=at(2),
            source=source(2),
        )
        _, state, mutated = challenge.record_undo(
            self.base,
            target_event_id=event["event_id"],
            timestamp_utc=at(3),
            source=source(3),
        )
        self.assertTrue(mutated)
        self.assertEqual(state.cash["USD"], Decimal("100"))
        self.assertIn(event["event_id"], state.undone_event_ids)

    def test_undo_is_rejected_when_replay_would_break_cash_invariant(self) -> None:
        self.initialize("100")
        deposit, _, _ = challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("100"),
            timestamp_utc=at(2),
            source=source(2),
        )
        challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("2"),
            price_usd=Decimal("100"),
            timestamp_utc=at(3),
            source=source(3),
        )
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_undo(
                self.base,
                target_event_id=deposit["event_id"],
                timestamp_utc=at(4),
                source=source(4),
            )
        self.assertEqual(len(challenge.read_events(self.base)), 3)

    def test_initialization_cannot_be_undone(self) -> None:
        self.initialize("100")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_undo(
                self.base,
                target_event_id="CH-000001",
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_reset_returns_to_prelaunch_and_clears_live_state(self) -> None:
        self.initialize("100")
        _, state = challenge.reset_challenge(self.base, timestamp_utc=at(2), source=source(2))
        self.assertFalse(state.initialized)
        self.assertEqual(state.cash["USD"], Decimal("0"))
        self.assertEqual(challenge.load_config(self.base)["status"], "prelaunch")
        challenge.validate_all(self.base)

    def test_paused_challenge_rejects_mutation(self) -> None:
        self.initialize("100")
        challenge.set_challenge_status(self.base, "paused")
        with self.assertRaises(challenge.ChallengeValidationError):
            challenge.record_cash_event(
                self.base,
                event_type="DEPOSIT",
                currency="USD",
                amount=Decimal("10"),
                timestamp_utc=at(2),
                source=source(2),
            )

    def test_telegram_dispatcher_routes_active_mstr_commands_to_challenge_ledger(self) -> None:
        portfolio.ensure_data_files(self.base)
        state = portfolio.load_state(self.base)
        message, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            portfolio.read_ledger(self.base),
            update_id=101,
            chat_id="owner",
            message_id=201,
            message_timestamp_utc=at(),
            text="/challenge_init USD 500",
        )
        self.assertTrue(mutated)
        self.assertIn("Challenge initialized", message)
        message, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            portfolio.read_ledger(self.base),
            update_id=102,
            chat_id="owner",
            message_id=202,
            message_timestamp_utc=at(2),
            text="/buy_mstr 2 100",
        )
        self.assertTrue(mutated)
        self.assertIn("BUY MSTR recorded", message)
        self.assertEqual(len(challenge.read_events(self.base)), 2)
        self.assertEqual(portfolio.read_ledger(self.base), [])

    def test_telegram_dispatcher_preserves_legacy_surface_while_prelaunch(self) -> None:
        portfolio.ensure_data_files(self.base)
        state = portfolio.load_state(self.base)
        message, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            portfolio.read_ledger(self.base),
            update_id=101,
            chat_id="owner",
            message_id=201,
            message_timestamp_utc=at(),
            text="/buy_mstr 1 100",
        )
        self.assertTrue(mutated)
        self.assertIn("MSTR", message)
        self.assertEqual(challenge.read_events(self.base), [])
        self.assertEqual(len(portfolio.read_ledger(self.base)), 1)

    def test_telegram_dispatcher_reports_challenge_validation_without_mutation(self) -> None:
        portfolio.ensure_data_files(self.base)
        self.initialize("100")
        state = portfolio.load_state(self.base)
        message, mutated = portfolio.handle_authorized_text_command(
            self.base,
            state,
            portfolio.read_ledger(self.base),
            update_id=102,
            chat_id="owner",
            message_id=202,
            message_timestamp_utc=at(2),
            text="/buy_mstr 2 100",
        )
        self.assertFalse(mutated)
        self.assertIn("exceeds USD cash", message)
        self.assertEqual(len(challenge.read_events(self.base)), 1)

    def test_event_reader_rejects_non_chronological_tampering(self) -> None:
        self.initialize("100")
        challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("10"),
            timestamp_utc=at(2),
            source=source(2),
        )
        events = challenge.read_events(self.base)
        events[1]["timestamp_utc"] = challenge.iso_seconds(at() - timedelta(days=1))
        events[1]["timestamp_wib"] = challenge.wib_iso_seconds(at() - timedelta(days=1))
        challenge.atomic_write_text(
            challenge.event_path(self.base),
            "".join(challenge.canonical_json(item) + "\n" for item in events),
        )
        with self.assertRaises(challenge.ChallengeIntegrityError):
            challenge.read_events(self.base)

    def test_modified_dietz_without_flow_matches_simple_return(self) -> None:
        result, reason = challenge.modified_dietz_return(
            Decimal("100"), Decimal("110"), at(), at(2), []
        )
        self.assertIsNone(reason)
        self.assertEqual(result, Decimal("10"))

    def test_modified_dietz_weights_midperiod_deposit(self) -> None:
        self.initialize("100", when=at())
        challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("50"),
            timestamp_utc=at(2),
            source=source(2),
        )
        result, reason = challenge.modified_dietz_return(
            Decimal("100"), Decimal("160"), at(), at(3), challenge.read_events(self.base)
        )
        self.assertIsNone(reason)
        self.assertEqual(result.quantize(Decimal("0.0001")), Decimal("8.0000"))

    def test_modified_dietz_weights_midperiod_withdrawal(self) -> None:
        self.initialize("100", when=at())
        challenge.record_cash_event(
            self.base,
            event_type="WITHDRAWAL",
            currency="USD",
            amount=Decimal("20"),
            timestamp_utc=at(2),
            source=source(2),
        )
        result, reason = challenge.modified_dietz_return(
            Decimal("100"), Decimal("88"), at(), at(3), challenge.read_events(self.base)
        )
        self.assertIsNone(reason)
        self.assertEqual(result.quantize(Decimal("0.0001")), Decimal("8.8889"))

    def test_snapshot_replaces_same_timestamp_and_rejects_reverse_time(self) -> None:
        self.initialize("100")
        challenge.create_snapshot(self.base, market=market(mstr="100"), captured_at=at(2))
        challenge.create_snapshot(self.base, market=market(mstr="110"), captured_at=at(2))
        self.assertEqual(len(challenge.read_snapshots(self.base)), 1)
        with self.assertRaises(challenge.ChallengeIntegrityError):
            challenge.create_snapshot(self.base, market=market(mstr="90"), captured_at=at())

    def test_benchmark_applies_each_external_flow_at_its_reference_price(self) -> None:
        self.initialize("1000", when=at())
        challenge.record_cash_event(
            self.base,
            event_type="DEPOSIT",
            currency="USD",
            amount=Decimal("500"),
            timestamp_utc=at(2),
            source=source(2),
            market=market(btc="100000", captured_at=at(2)),
        )
        snapshots = [
            {
                "schema_version": 1,
                "challenge_id": challenge.DEFAULT_CHALLENGE_ID,
                "captured_at_utc": challenge.iso_seconds(at(3)),
                "captured_at_wib": challenge.wib_iso_seconds(at(3)),
                "prices": {"MSTR": "100", "BTC": "100000", "USD_IDR": "16000"},
                "sources": {},
                "portfolio": {},
            }
        ]
        value, units, reason = challenge.benchmark_value_at(
            challenge.read_events(self.base), snapshots, asset="BTC", at=at(3)
        )
        self.assertIsNone(reason)
        self.assertEqual(units, Decimal("0.025"))
        self.assertEqual(value, Decimal("2500"))

    def test_performance_export_uses_modified_dietz_and_benchmarks(self) -> None:
        self.initialize("1000", when=at())
        challenge.create_snapshot(self.base, market=market(mstr="100", btc="50000"), captured_at=at())
        challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("10"),
            price_usd=Decimal("100"),
            timestamp_utc=at(1, 1),
            source=source(2),
        )
        challenge.create_snapshot(self.base, market=market(mstr="110", btc="50000"), captured_at=at(2))
        payload = challenge.build_performance_export(
            challenge.load_config(self.base),
            challenge.read_events(self.base),
            challenge.read_snapshots(self.base),
            generated_at=challenge.iso_seconds(at(2)),
        )
        self.assertTrue(payload["data_sufficient"])
        self.assertEqual(Decimal(payload["metrics"]["challenge"]["return_pct"]), Decimal("10"))
        self.assertEqual(Decimal(payload["metrics"]["direct_btc"]["return_pct"]), Decimal("0"))
        self.assertEqual(Decimal(payload["metrics"]["mstr_buy_hold"]["return_pct"]), Decimal("10"))
        self.assertEqual(Decimal(payload["ranges"]["ALL"]["metrics"]["challenge"]["return_pct"]), Decimal("10"))
        self.assertEqual(payload["ranges"]["1D"]["observation_count"], 2)

    def test_performance_ignores_snapshots_from_a_reset_era(self) -> None:
        self.initialize("100", when=at())
        challenge.create_snapshot(self.base, market=market(), captured_at=at())
        challenge.reset_challenge(self.base, timestamp_utc=at(2), source=source(2))
        self.initialize("200", when=at(3), update_id=3)
        challenge.create_snapshot(self.base, market=market(), captured_at=at(3))
        challenge.create_snapshot(self.base, market=market(), captured_at=at(4))
        payload = challenge.build_performance_export(
            challenge.load_config(self.base),
            challenge.read_events(self.base),
            challenge.read_snapshots(self.base),
            generated_at=challenge.iso_seconds(at(4)),
        )
        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(payload["series"][0]["timestamp"], challenge.iso_seconds(at(3)))

    def test_public_export_excludes_private_telegram_metadata(self) -> None:
        self.initialize("100")
        challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("0.5"),
            price_usd=Decimal("100"),
            timestamp_utc=at(2),
            source=source(2),
        )
        exported = challenge.export_public(
            self.base,
            generated_at=at(3),
            source_commit_sha="abc123",
            market=market(captured_at=at(3)),
        )
        serialized = json.dumps(exported["payloads"], sort_keys=True).lower()
        self.assertNotIn("private_source", serialized)
        self.assertNotIn("chat_id", serialized)
        self.assertNotIn("private-owner", serialized)
        self.assertIn("balance_before", serialized)
        self.assertIn("balance_after", serialized)

    def test_public_export_records_legacy_opening_position(self) -> None:
        self.initialize(
            "100",
            include_legacy_position=True,
            opening_quantity="2",
            opening_cost="50",
        )
        payloads = challenge.export_public(
            self.base,
            generated_at=at(2),
            market=market(captured_at=at(2)),
        )["payloads"]
        self.assertEqual(payloads["transactions"]["events"], [])
        self.assertEqual(payloads["overview"]["portfolio"]["mstr_quantity"], "2")
        self.assertEqual(payloads["overview"]["portfolio"]["mstr_average_cost"], "50")
        self.assertEqual(payloads["overview"]["portfolio"]["net_contributions_usd"], "200")

    def test_public_transactions_include_only_active_buy_and_sell_events(self) -> None:
        self.initialize("1000")
        undone_buy, _, _ = challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("1"),
            price_usd=Decimal("100"),
            timestamp_utc=at(2),
            source=source(2),
        )
        challenge.record_undo(
            self.base,
            target_event_id=undone_buy["event_id"],
            timestamp_utc=at(3),
            source=source(3),
        )
        active_buy, _, _ = challenge.record_trade(
            self.base,
            event_type="BUY",
            quantity=Decimal("2"),
            price_usd=Decimal("100"),
            timestamp_utc=at(4),
            source=source(4),
        )
        payload = challenge.export_public(
            self.base,
            generated_at=at(5),
            market=market(captured_at=at(5)),
        )["payloads"]["transactions"]
        self.assertEqual(payload["count"], 1)
        self.assertEqual([event["event_id"] for event in payload["events"]], [active_buy["event_id"]])
        self.assertEqual({event["type"] for event in payload["events"]}, {"BUY"})

    def test_public_hash_and_export_writes_are_deterministic(self) -> None:
        self.initialize("100")
        events = challenge.read_events(self.base)
        self.assertEqual(challenge.public_ledger_hash(events), challenge.public_ledger_hash(events))
        first = challenge.export_public(
            self.base,
            generated_at=at(2),
            market=market(captured_at=at(2)),
            create_market_snapshot=False,
        )
        second = challenge.export_public(
            self.base,
            generated_at=at(3),
            market=market(captured_at=at(2)),
            create_market_snapshot=False,
        )
        self.assertTrue(first["changed"])
        self.assertEqual(second["changed"], [])

    def test_public_validator_rejects_floats_and_private_keys(self) -> None:
        with self.assertRaises(challenge.ChallengeIntegrityError):
            challenge.validate_public_payload({"schema_version": 1, "value": 1.5})
        with self.assertRaises(challenge.ChallengeIntegrityError):
            challenge.validate_public_payload({"schema_version": 1, "chat_id": "x"})

    def test_thesis_export_blocks_unknown_gates_and_stringifies_legacy_floats(self) -> None:
        engine = {
            "updated_at_utc": challenge.iso_seconds(at()),
            "last_action": "HOLD",
            "fingerprint": {
                "usd_div_coverage_months": 16.5,
                "btc_holdings": 100,
                "basic_shares_m": 1,
                "diluted_shares_m": 1.1,
                "debt_schedule": {"2028": 1.25},
            },
            "zones": {"risk_score": 0.2, "liquidity_score": 0.8, "accretion_score": 0.5},
        }
        (self.base / "mstr_decision_engine_v2_state.json").write_text(json.dumps(engine), encoding="utf-8")
        payload = challenge.build_thesis_export(
            self.base,
            market(mstr="80"),
            generated_at=challenge.iso_seconds(at()),
            source_commit_sha="abc",
        )
        self.assertFalse(payload["gates"]["starter"]["eligible"])
        self.assertFalse(payload["gates"]["strategic"]["eligible"])
        self.assertEqual(payload["liquidity"]["debt_schedule"]["2028"], "1.25")
        challenge.validate_public_payload(payload)

    def test_thesis_export_compares_material_disclosure_observations(self) -> None:
        previous_engine = {
            "updated_at_utc": challenge.iso_seconds(at()),
            "fingerprint": {
                "btc_holdings": 100,
                "basic_shares_m": 1,
                "diluted_shares_m": 1,
                "usd_reserve_b": 1,
                "usd_div_coverage_months": 12,
                "debt_b": 2,
                "preferred_b": 3,
            },
            "zones": {"fair_ev_nav": 1.1},
        }
        current_engine = {
            "updated_at_utc": challenge.iso_seconds(at(2)),
            "last_action": "HOLD",
            "fingerprint": {
                "btc_holdings": 100,
                "basic_shares_m": 1.1,
                "diluted_shares_m": 1.1,
                "usd_reserve_b": 1.5,
                "usd_div_coverage_months": 18,
                "debt_b": 2,
                "preferred_b": 3,
            },
            "zones": {
                "risk_score": 0.2,
                "liquidity_score": 0.8,
                "accretion_score": 0.5,
                "fair_ev_nav": 1.2,
            },
        }
        challenge.record_engine_disclosure(self.base, previous_engine)
        (self.base / "mstr_decision_engine_v2_state.json").write_text(
            json.dumps(current_engine), encoding="utf-8"
        )
        payload = challenge.build_thesis_export(
            self.base,
            market(mstr="80"),
            generated_at=challenge.iso_seconds(at(2)),
            source_commit_sha="abc",
        )
        self.assertEqual(payload["accretion"]["disclosure_observations"], 2)
        self.assertEqual(payload["accretion"]["residual_adso_trend"], "deteriorating")
        self.assertLess(Decimal(payload["accretion"]["btc_per_adso_change_pct"]), Decimal("0"))
        self.assertFalse(payload["gates"]["starter"]["checks"][2]["passed"])

    def test_prelaunch_health_is_setup_required_without_fake_metrics(self) -> None:
        payloads = challenge.export_public(
            self.base,
            generated_at=at(),
            market=market(),
            create_market_snapshot=False,
        )["payloads"]
        self.assertEqual(payloads["health"]["status"], "setup_required")
        self.assertTrue(payloads["overview"]["portfolio"]["setup_required"])
        self.assertIsNone(payloads["overview"]["portfolio"]["total_portfolio_usd"])

    def test_validate_all_checks_generated_public_files(self) -> None:
        challenge.export_public(
            self.base,
            generated_at=at(),
            market=market(),
            create_market_snapshot=False,
        )
        challenge.validate_all(self.base, require_public=True)

    def test_financial_module_contains_no_float_literals(self) -> None:
        tree = ast.parse(Path(challenge.__file__).read_text(encoding="utf-8"))
        float_literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        self.assertEqual(float_literals, [])


if __name__ == "__main__":
    unittest.main()
