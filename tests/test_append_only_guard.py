from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_append_only_prefix import (
    AppendOnlyViolation,
    assert_append_only_prefix,
)


class AppendOnlyLedgerGuardTests(unittest.TestCase):
    def write(self, directory: Path, name: str, lines: list[str]) -> Path:
        path = directory / name
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    def test_accepts_identical_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = self.write(root, "remote.jsonl", ["A", "B"])
            candidate = self.write(root, "candidate.jsonl", ["A", "B"])
            self.assertEqual(assert_append_only_prefix(remote, candidate), (2, 2))

    def test_accepts_appended_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = self.write(root, "remote.jsonl", ["A", "B"])
            candidate = self.write(root, "candidate.jsonl", ["A", "B", "C"])
            self.assertEqual(assert_append_only_prefix(remote, candidate), (2, 3))

    def test_rejects_shorter_stale_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = self.write(root, "remote.jsonl", ["A", "B", "C"])
            candidate = self.write(root, "candidate.jsonl", ["A", "B"])
            with self.assertRaises(AppendOnlyViolation):
                assert_append_only_prefix(remote, candidate)

    def test_rejects_modified_existing_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = self.write(root, "remote.jsonl", ["A", "B"])
            candidate = self.write(root, "candidate.jsonl", ["A", "X", "C"])
            with self.assertRaises(AppendOnlyViolation):
                assert_append_only_prefix(remote, candidate)


if __name__ == "__main__":
    unittest.main()
