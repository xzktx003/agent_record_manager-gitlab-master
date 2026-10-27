#!/usr/bin/env python3
"""Operate the work-memory intelligence layer without the Web UI."""

from __future__ import annotations

import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.database import init_db  # noqa: E402
from app.enrichment import enrichment_manager, process_session  # noqa: E402
from app.smart_search import quick_search_payload  # noqa: E402
from app.work_memory import generate_review, get_enrichment_status  # noqa: E402


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Work Memory intelligence tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show queue and recent Agent runs")
    enrich = sub.add_parser("analyze-session", help="analyze one session now")
    enrich.add_argument("session_id")

    backfill = sub.add_parser("backfill", help="enqueue historical sessions")
    backfill.add_argument("--days", type=int)
    backfill.add_argument("--source")
    backfill.add_argument("--force", action="store_true")

    search = sub.add_parser("search", help="task-level quick search")
    search.add_argument("query")
    search.add_argument("--source")
    search.add_argument("--limit", type=int, default=20)

    review = sub.add_parser("review", help="generate deterministic review")
    review.add_argument("--period", choices=("day", "week", "month"), default="week")
    review.add_argument("--anchor")

    args = parser.parse_args()
    init_db()
    if args.command == "status":
        print_json(get_enrichment_status())
    elif args.command == "analyze-session":
        print_json(process_session(args.session_id, require_running_job=False))
    elif args.command == "backfill":
        print_json(enrichment_manager.enqueue_history(args.days, args.source, args.force))
    elif args.command == "search":
        print_json(
            quick_search_payload(
                args.query, {"source": args.source}, limit=max(1, args.limit)
            )
        )
    elif args.command == "review":
        print_json(generate_review(args.period, args.anchor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
