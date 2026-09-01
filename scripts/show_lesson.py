#!/usr/bin/env python3
"""Print the standing lesson for one signature as formatted JSON.

READ-ONLY. Opens no DB that does not already exist and never writes.

    .venv/bin/python scripts/show_lesson.py <signature>

or, with the venv activated, directly:

    scripts/show_lesson.py <signature>

Exit status: 0 lesson printed, 1 signature not found, 2 no database yet.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lessons import LessonStore, open_client  # noqa: E402
from memory import DB_PATH  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Print one lesson row as formatted JSON. Read-only."
    )
    parser.add_argument(
        "signature",
        help="full sig_v1 key: <lowercase_address>/<functionName>/<failureClassCamel>",
    )
    # Defaults to the one DB path constant. The flag exists so this can be
    # pointed at a scratch DB without touching the agent's store.
    parser.add_argument(
        "--db", default=DB_PATH, type=Path,
        help=f"database to read (default: {DB_PATH})",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # MemoryClient.local() APPLIES THE SCHEMA on first open, which would
    # create the file. Refuse rather than write anything into an empty tree.
    if not args.db.exists():
        print(f"no database at {args.db} — nothing has been learned yet",
              file=sys.stderr)
        return 2

    store = LessonStore(open_client(args.db))
    lesson = store.get_lesson(args.signature)
    if lesson is None:
        print(f"no lesson stored for {args.signature}", file=sys.stderr)
        return 1

    # Explicit key order, not sorted: identity first, then the two row
    # timestamps that carry first_seen / last_revised, then the body.
    print(json.dumps(
        {
            "id": lesson["id"],
            "created_at": lesson["created_at"],
            "updated_at": lesson["updated_at"],
            "body": lesson["body"],
        },
        indent=2,
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
