#!/usr/bin/env python3
"""Compact Agent Memory's FTS index without taking the read API offline."""
import argparse
import os
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.database import compact_messages_fts, init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Online rebuild of the SQLite trigram index in compact mode."
    )
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args()

    init_db()
    started = time.monotonic()

    def report(done: int, total: int) -> None:
        elapsed = max(time.monotonic() - started, 0.001)
        percent = (done / total * 100) if total else 100
        rate = done / elapsed
        print(
            f"\rIndexed {done:,}/{total:,} ({percent:5.1f}%) "
            f"at {rate:,.0f} messages/s",
            end="",
            flush=True,
        )

    result = compact_messages_fts(
        batch_size=args.batch_size,
        progress_callback=report,
    )
    print()
    print(
        f"Compact index active; reusable free space: "
        f"{result['reusable_free_bytes'] / 1073741824:.2f} GiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
