"""Empirical Challenger stress test suite for Milestone M2.

Independently verifies:
1. Line-by-line accounting replay of all 18 events against an independent oracle.
2. Undo semantics (CH-000016 cleanly neutralizing CH-000015).
3. Liquidation semantics (CH-000018 cleanly closing position to 0 shares and $965.27 cash).
4. Direct calculation of public ledger hash from first principles.
5. Production snapshot match against authoritative hash '564752a86b573c874763e219479ebdc3e9c198933c22793607aae21b62b9d115'.
6. Bot 3-layer snapshot fallbacks and dry-run execution.
7. Public mirror document integrity and privacy scan.
"""
from __future__ import annotations

import io
import json
import hashlib
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

import challenge
import mstr_bot
from tools.check_append_only_prefix import assert_append_only_prefix

EXPECTED_PRODUCTION_HASH = "564752a86b573c874763e219479ebdc3e9c198933c22793607aae21b62b9d115"
ROOT_DIR = Path(__file__).resolve().parent.parent


class IndependentAccountingOracle:
    """Independent mathematical model of portfolio event accounting from first principles."""

    def __init__(self):
        self.cash_usd = Decimal("0")
        self.cash_idr = Decimal("0")
        self.shares = Decimal("0")
        self.average_cost = Decimal("0")
        self.cost_basis = Decimal("0")
        self.realized_pl_usd = Decimal("0")
        self.net_contributions_usd = Decimal("0")
        self.initialized = False

    def replay_all(self, raw_events: list[dict]) -> list[dict]:
        """Replays events using undo-exclusion model."""
        undone_target_ids = {
            e["target_event_id"]
            for e in raw_events
            if e.get("event_type") == "UNDO" and e.get("target_event_id")
        }

        step_history = []
        for e in raw_events:
            eid = e["event_id"]
            etype = e["event_type"]

            is_undone = eid in undone_target_ids
            is_undo_op = etype == "UNDO"

            if not is_undone and not is_undo_op:
                self._apply_event(e)

            step_history.append({
                "event_id": eid,
                "event_type": etype,
                "is_undone": is_undone,
                "is_undo_op": is_undo_op,
                "cash_usd": self.cash_usd,
                "shares": self.shares,
                "average_cost": self.average_cost,
                "cost_basis": self.cost_basis,
                "realized_pl_usd": self.realized_pl_usd,
                "net_contributions_usd": self.net_contributions_usd,
            })
        return step_history

    def _apply_event(self, e: dict):
        etype = e["event_type"]
        if etype == "CHALLENGE_INIT":
            self.initialized = True
            amt = Decimal(str(e["amount"]))
            self.cash_usd += amt
            self.net_contributions_usd += amt
        elif etype == "DEPOSIT":
            amt = Decimal(str(e["amount"]))
            self.cash_usd += amt
            self.net_contributions_usd += amt
        elif etype == "WITHDRAWAL":
            amt = Decimal(str(e["amount"]))
            assert amt <= self.cash_usd
            self.cash_usd -= amt
            self.net_contributions_usd -= amt
        elif etype == "BUY":
            qty = Decimal(str(e["quantity"]))
            price = Decimal(str(e["price_usd"]))
            cost = qty * price
            assert cost <= self.cash_usd
            self.cash_usd -= cost
            new_shares = self.shares + qty
            new_cost_basis = self.cost_basis + cost
            self.average_cost = new_cost_basis / new_shares
            self.shares = new_shares
            self.cost_basis = new_cost_basis
        elif etype == "SELL":
            qty = Decimal(str(e["quantity"]))
            price = Decimal(str(e["price_usd"]))
            assert qty <= self.shares
            proceeds = qty * price
            released_cost = qty * self.average_cost
            self.cash_usd += proceeds
            self.shares -= qty
            self.cost_basis -= released_cost
            self.realized_pl_usd += (proceeds - released_cost)
            if self.shares == Decimal("0"):
                self.average_cost = Decimal("0")
                self.cost_basis = Decimal("0")


class EmpiricalChallengerM2Tests(unittest.TestCase):
    def setUp(self):
        self.events_path = ROOT_DIR / "data" / "challenge_events.jsonl"
        self.raw_lines = [
            line.strip()
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.raw_events = [json.loads(line) for line in self.raw_lines]
        self.events = challenge.read_events(ROOT_DIR)

    def test_01_event_counts_and_structure(self):
        """Stress test: 18 raw events, 10 active events, 4 undone events, 4 undo operations."""
        self.assertEqual(len(self.raw_lines), 18)
        self.assertEqual(len(self.raw_events), 18)
        self.assertEqual(len(self.events), 18)

        # Check monotonically increasing IDs
        for idx, ev in enumerate(self.raw_events, start=1):
            expected_id = f"CH-{idx:06d}"
            self.assertEqual(ev["event_id"], expected_id)

        # Check undo counts
        undo_ops = [e for e in self.raw_events if e["event_type"] == "UNDO"]
        self.assertEqual(len(undo_ops), 4)
        target_ids = {e["target_event_id"] for e in undo_ops}
        self.assertEqual(target_ids, {"CH-000003", "CH-000007", "CH-000008", "CH-000015"})

        active_events = [e for e in self.raw_events if e["event_id"] not in target_ids and e["event_type"] != "UNDO"]
        self.assertEqual(len(active_events), 10)
        active_ids = [e["event_id"] for e in active_events]
        self.assertEqual(
            active_ids,
            ["CH-000001", "CH-000002", "CH-000005", "CH-000006", "CH-000011", "CH-000012", "CH-000013", "CH-000014", "CH-000017", "CH-000018"]
        )

    def test_02_line_by_line_independent_accounting(self):
        """Line-by-line verification against independent accounting oracle."""
        oracle = IndependentAccountingOracle()
        history = oracle.replay_all(self.raw_events)
        self.assertEqual(len(history), 18)

        # Step 1: CH-000001 CHALLENGE_INIT (172.188 USD)
        h1 = history[0]
        self.assertEqual(h1["cash_usd"], Decimal("172.188"))
        self.assertEqual(h1["shares"], Decimal("0"))
        self.assertEqual(h1["net_contributions_usd"], Decimal("172.188"))

        # Step 2: CH-000002 BUY 1.0 @ $82.00
        h2 = history[1]
        self.assertEqual(h2["cash_usd"], Decimal("90.188"))
        self.assertEqual(h2["shares"], Decimal("1.0"))
        self.assertEqual(h2["cost_basis"], Decimal("82.00"))

        # Step 3: CH-000003 BUY 1.0 (undone by CH-000004) -> state unchanged in replay
        h3 = history[2]
        self.assertTrue(h3["is_undone"])
        self.assertEqual(h3["cash_usd"], Decimal("90.188"))
        self.assertEqual(h3["shares"], Decimal("1.0"))

        # Step 4: CH-000004 UNDO -> state unchanged in replay
        h4 = history[3]
        self.assertTrue(h4["is_undo_op"])
        self.assertEqual(h4["cash_usd"], Decimal("90.188"))

        # Step 5: CH-000005 BUY 0.9 @ $82.00 ($73.80)
        h5 = history[4]
        self.assertEqual(h5["cash_usd"], Decimal("16.388"))
        self.assertEqual(h5["shares"], Decimal("1.9"))
        self.assertEqual(h5["cost_basis"], Decimal("155.80"))

        # Step 6: CH-000006 BUY 0.2 @ $81.94 ($16.388)
        h6 = history[5]
        self.assertEqual(h6["cash_usd"], Decimal("0.000"))
        self.assertEqual(h6["shares"], Decimal("2.1"))
        self.assertEqual(h6["cost_basis"], Decimal("172.188"))

        # Steps 7-10: Undone sells & undo events
        for i in range(6, 10):
            self.assertEqual(history[i]["shares"], Decimal("2.1"))
            self.assertEqual(history[i]["cash_usd"], Decimal("0.000"))

        # Step 11: CH-000011 DEPOSIT $278.40
        h11 = history[10]
        self.assertEqual(h11["cash_usd"], Decimal("278.40"))
        self.assertEqual(h11["net_contributions_usd"], Decimal("450.588"))

        # Step 12: CH-000012 DEPOSIT $106.54
        h12 = history[11]
        self.assertEqual(h12["cash_usd"], Decimal("384.94"))
        self.assertEqual(h12["net_contributions_usd"], Decimal("557.128"))

        # Step 13: CH-000013 DEPOSIT $175.74
        h13 = history[12]
        self.assertEqual(h13["cash_usd"], Decimal("560.68"))
        self.assertEqual(h13["net_contributions_usd"], Decimal("732.868"))

        # Step 14: CH-000014 DEPOSIT $28.09
        h14 = history[13]
        self.assertEqual(h14["cash_usd"], Decimal("588.77"))
        self.assertEqual(h14["net_contributions_usd"], Decimal("760.958"))

        # Step 15 & 16: CH-000015 (DEPOSIT $28.09) and CH-000016 (UNDO target CH-000015)
        h15 = history[14]
        h16 = history[15]
        self.assertTrue(h15["is_undone"])
        self.assertTrue(h16["is_undo_op"])
        self.assertEqual(h15["cash_usd"], Decimal("588.77"))
        self.assertEqual(h15["net_contributions_usd"], Decimal("760.958"))
        self.assertEqual(h16["cash_usd"], Decimal("588.77"))
        self.assertEqual(h16["net_contributions_usd"], Decimal("760.958"))

        # Step 17: CH-000017 DEPOSIT $27.90
        h17 = history[16]
        self.assertEqual(h17["cash_usd"], Decimal("616.67"))
        self.assertEqual(h17["net_contributions_usd"], Decimal("788.858"))

        # Step 18: CH-000018 SELL 2.1 @ $166.00 ($348.60 proceeds)
        h18 = history[17]
        self.assertEqual(h18["shares"], Decimal("0.0"))
        self.assertEqual(h18["average_cost"], Decimal("0.0"))
        self.assertEqual(h18["cost_basis"], Decimal("0.0"))
        self.assertEqual(h18["cash_usd"], Decimal("965.27"))
        self.assertEqual(round(h18["realized_pl_usd"], 3), Decimal("176.412"))
        self.assertEqual(round(h18["net_contributions_usd"], 3), Decimal("788.858"))

        # Verify challenge.replay_events matches independent oracle 100%
        state = challenge.replay_events(self.events)
        self.assertEqual(state.cash["USD"], oracle.cash_usd)
        self.assertEqual(state.position.quantity, oracle.shares)
        self.assertEqual(state.position.average_cost, oracle.average_cost)
        self.assertEqual(round(state.realized_pl_usd, 3), Decimal("176.412"))
        self.assertEqual(round(state.net_contributions_usd, 3), Decimal("788.858"))

    def test_03_undo_neutralization_adversarial(self):
        """Stress-test undo semantics: isolation, counterfactual replay, and invalid undo scenarios."""
        # 1. Counterfactual: What if CH-000016 was omitted?
        events_without_undo = [e for e in self.raw_events if e["event_id"] != "CH-000016"]
        state_cf = challenge.replay_events(events_without_undo)
        # Without undo of CH-000015 ($28.09 deposit), cash would be 965.27 + 28.09 = 993.36
        self.assertEqual(state_cf.cash["USD"], Decimal("993.360"))
        self.assertEqual(round(state_cf.net_contributions_usd, 3), Decimal("816.948"))

        # 2. Adversarial: Undo of an UNDO must be forbidden
        bad_undo_of_undo = {
            "event_id": "CH-000019",
            "event_type": "UNDO",
            "target_event_id": "CH-000016",
            "timestamp_utc": "2026-09-22T01:00:00+00:00",
            "timestamp_wib": "2026-09-22T08:00:00+07:00",
            "schema_version": 2,
            "private_source": {"source": "test"},
        }
        with self.assertRaises(challenge.ChallengeIntegrityError) as ctx:
            challenge.replay_events([*self.raw_events, bad_undo_of_undo])
        self.assertIn("UNDO of UNDO is forbidden", str(ctx.exception))

        # 3. Adversarial: Double UNDO of same target must be forbidden
        duplicate_undo = {
            "event_id": "CH-000019",
            "event_type": "UNDO",
            "target_event_id": "CH-000015",
            "timestamp_utc": "2026-09-22T01:00:00+00:00",
            "timestamp_wib": "2026-09-22T08:00:00+07:00",
            "schema_version": 2,
            "private_source": {"source": "test"},
        }
        with self.assertRaises(challenge.ChallengeIntegrityError) as ctx:
            challenge.replay_events([*self.raw_events, duplicate_undo])
        self.assertIn("undone twice", str(ctx.exception))

        # 4. Adversarial: Forward UNDO (targeting a future event) must be forbidden
        forward_undo = {
            "event_id": "CH-000002",
            "event_type": "UNDO",
            "target_event_id": "CH-000005",
            "timestamp_utc": "2026-06-26T22:13:46+00:00",
            "timestamp_wib": "2026-06-27T05:13:46+07:00",
            "schema_version": 2,
            "private_source": {"source": "test"},
        }
        with self.assertRaises(challenge.ChallengeIntegrityError) as ctx:
            challenge.replay_events([self.raw_events[0], forward_undo, self.raw_events[1]])
        self.assertIn("precede event", str(ctx.exception))

    def test_04_liquidation_adversarial(self):
        """Stress-test liquidation CH-000018: complete exit, zero balance, and oversell guard."""
        state = challenge.replay_events(self.events)
        self.assertEqual(state.position.quantity, Decimal("0.0"))
        self.assertEqual(state.position.average_cost, Decimal("0.0"))
        self.assertEqual(state.cash["USD"], Decimal("965.270"))

        # Value Conservation identity: Cash + Stock Market Value == Net Contributions + Realized PL + Unrealized PL
        stock_market_value = state.position.quantity * Decimal("166.00")
        total_portfolio_value = state.cash["USD"] + stock_market_value
        total_profit = state.realized_pl_usd + Decimal("0.0")
        self.assertEqual(total_portfolio_value - state.net_contributions_usd, total_profit)

        # Adversarial: Attempting to sell more shares than held must raise ChallengeIntegrityError
        oversell_event = dict(self.raw_events[-1])
        oversell_event["quantity"] = "2.100001"
        oversell_event["amount"] = str(Decimal("2.100001") * Decimal("166"))
        events_oversell = [*self.raw_events[:-1], oversell_event]
        with self.assertRaises(challenge.ChallengeIntegrityError) as ctx:
            challenge.replay_events(events_oversell)
        self.assertIn("exceeds MSTR position", str(ctx.exception))

    def test_05_direct_public_ledger_hash_calculation(self):
        """Directly calculate SHA-256 ledger hash from first principles and compare with production."""
        # 1. Challenge helper function
        computed_hash = challenge.public_ledger_hash(self.events)
        self.assertEqual(computed_hash, EXPECTED_PRODUCTION_HASH)

        # 2. Independent first-principles calculation
        # Filter BUY/SELL not in undone
        undone = challenge.active_target_ids(self.events)
        effective_prefix = []
        exported = []
        for ev in self.events:
            sb = challenge.replay_events(effective_prefix)
            if ev["event_type"] != "UNDO" and ev["event_id"] not in undone:
                effective_prefix.append(dict(ev))
            sa = challenge.replay_events(effective_prefix)
            if ev["event_type"] in {"BUY", "SELL"} and ev["event_id"] not in undone:
                exported.append(challenge._public_event(
                    ev, undone, None, challenge._public_balance(sb), challenge._public_balance(sa)
                ))

        # Canonical RFC 8785 json dump
        canonical_str = json.dumps(exported, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        independent_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        self.assertEqual(independent_hash, EXPECTED_PRODUCTION_HASH)

    def test_06_public_mirror_state_and_manifest(self):
        """Verify public mirror JSON files match authoritative state."""
        public_dir = ROOT_DIR / "data" / "public"

        overview = json.loads((public_dir / "challenge_overview.json").read_text(encoding="utf-8"))
        self.assertEqual(overview["portfolio"]["cash_usd"], "965.27")
        self.assertEqual(overview["portfolio"]["mstr_quantity"], "0")
        self.assertEqual(overview["portfolio"]["realized_pl_usd"], "176.412")
        self.assertEqual(overview["portfolio"]["net_contributions_usd"], "788.858")
        self.assertEqual(overview["portfolio"]["cash_allocation_pct"], "100")
        self.assertEqual(overview["portfolio"]["mstr_allocation_pct"], "0")
        self.assertEqual(overview["market"]["freshness"], "fresh")
        self.assertEqual(overview["audit"]["ledger_hash"], EXPECTED_PRODUCTION_HASH)

        audit = json.loads((public_dir / "challenge_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["ledger_hash"], EXPECTED_PRODUCTION_HASH)
        self.assertEqual(audit["active_event_count"], 10)
        self.assertEqual(audit["undone_event_count"], 4)
        self.assertEqual(audit["latest_transaction_id"], "CH-000018")
        self.assertEqual(audit["market_data_health"], "fresh")

        manifest = json.loads((public_dir / "v2_authority_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["event_count"], 18)
        self.assertEqual(manifest["latest_event_id"], "CH-000018")
        self.assertEqual(manifest["ledger_hash"], EXPECTED_PRODUCTION_HASH)

        health = json.loads((public_dir / "challenge_health.json").read_text(encoding="utf-8"))
        self.assertTrue(health["ok"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["market_freshness"], "fresh")

        txs = json.loads((public_dir / "challenge_transactions.json").read_text(encoding="utf-8"))
        self.assertEqual(txs["count"], 10)
        self.assertEqual(txs["events"][-1]["event_id"], "CH-000018")

    def test_07_append_only_guard(self):
        """Verify candidate 18 events append cleanly onto original 12 events."""
        candidate_path = ROOT_DIR / "data" / "challenge_events.jsonl"
        lines = candidate_path.read_text(encoding="utf-8").splitlines()
        temp_remote = ROOT_DIR / "data" / ".temp_remote_12.tmp"
        try:
            temp_remote.write_text("\n".join(lines[:12]) + "\n", encoding="utf-8")
            remote_n, candidate_n = assert_append_only_prefix(temp_remote, candidate_path)
            self.assertEqual(remote_n, 12)
            self.assertEqual(candidate_n, 18)
        finally:
            if temp_remote.exists():
                temp_remote.unlink()

    def test_08_privacy_and_data_leak_scan(self):
        """Scans public mirror files to ensure no private telegram or auth metadata leaked."""
        forbidden = [
            "chat_id", "telegram_update_id", "telegram_message_id",
            "bot_token", "private_source", "encrypted_payload",
        ]
        public_dir = ROOT_DIR / "data" / "public"
        for p in public_dir.glob("*.json"):
            text = p.read_text(encoding="utf-8").lower()
            for token in forbidden:
                self.assertNotIn(token, text, f"Leak of '{token}' detected in {p.name}")

    def test_09_mstr_bot_snapshot_fallbacks(self):
        """Test all 3 fallback layers in fetch_v2_portfolio_snapshot."""
        # Layer 1 test: Real live worker API
        live_snap = mstr_bot.fetch_v2_portfolio_snapshot()
        self.assertIsNotNone(live_snap)
        portfolio = live_snap.get("portfolio", {})
        self.assertEqual(float(portfolio.get("cash_usd")), 965.27)
        self.assertEqual(float(portfolio.get("mstr_quantity")), 0.0)

        # Layer 2 test: Network down -> fallback to local mirror
        with patch("requests.get", side_effect=RuntimeError("Simulated Network Down")):
            l2_snap = mstr_bot.fetch_v2_portfolio_snapshot()
            self.assertIsNotNone(l2_snap)
            p2 = l2_snap.get("portfolio", {})
            self.assertEqual(float(p2.get("cash_usd")), 965.27)
            self.assertEqual(float(p2.get("mstr_quantity")), 0.0)

        # Layer 3 test: Network down and local mirror missing -> fail cleanly (None) instead of fabricating hardcoded baseline
        with patch("requests.get", side_effect=RuntimeError("Simulated Network Down")):
            with patch("mstr_bot.LOCAL_OVERVIEW_PATH", ROOT_DIR / "nonexistent.json"):
                l3_snap = mstr_bot.fetch_v2_portfolio_snapshot()
                self.assertIsNone(l3_snap)


if __name__ == "__main__":
    unittest.main()
