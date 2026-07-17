from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


class AppendOnlyViolation(ValueError):
    """Raised when a candidate ledger would remove or rewrite remote events."""


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def assert_append_only_prefix(remote_path: Path, candidate_path: Path) -> tuple[int, int]:
    remote_lines = read_lines(remote_path)
    candidate_lines = read_lines(candidate_path)

    if len(candidate_lines) < len(remote_lines):
        raise AppendOnlyViolation(
            "Refusing stale challenge-ledger write: "
            f"remote has {len(remote_lines)} lines but candidate has {len(candidate_lines)}."
        )

    if candidate_lines[: len(remote_lines)] != remote_lines:
        raise AppendOnlyViolation(
            "Refusing non-append-only challenge-ledger write: "
            "an existing remote event was removed, reordered, or modified."
        )

    return len(remote_lines), len(candidate_lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that a candidate JSONL ledger only appends to the remote ledger."
    )
    parser.add_argument("remote", type=Path)
    parser.add_argument("candidate", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        remote_count, candidate_count = assert_append_only_prefix(
            args.remote, args.candidate
        )
    except AppendOnlyViolation as exc:
        raise SystemExit(str(exc)) from exc

    print(
        "append-only ledger check passed: "
        f"remote={remote_count} candidate={candidate_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
