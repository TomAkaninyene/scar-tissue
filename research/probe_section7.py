"""Throwaway probe for FINDINGS.md section 7. Prints RAW results only.

No interpretation, no pass/fail. Run with no args to execute the full probe.
Internal dispatch args (write_phase / read_phase) exist only so part 1 can
run in genuinely separate OS processes.

Scratch DB only -- never touches ./data/reflex.db.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

SCRATCH_DIR = Path(__file__).parent / "probe_scratch"
SCRATCH_DB = SCRATCH_DIR / "probe.db"

sys.path.insert(0, "/root/sibyl-probe/.venv/lib/python3.12/site-packages")


def _client():
    from sibyl_memory_client import MemoryClient
    return MemoryClient.local(str(SCRATCH_DB), tier="free")


def _dump(label, obj):
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# Part 1: cold-start persistence (run in a fresh subprocess per phase)
# ---------------------------------------------------------------------------
def write_phase():
    client = _client()
    marker = str(uuid.uuid4())
    result = client.set_entity(
        "probe", "cold_start_check",
        {"marker": marker, "written_by_pid": os.getpid()},
    )
    _dump("PART 1 / write_phase (pid=%d)" % os.getpid(), {
        "set_entity_result": result,
        "db_path": str(SCRATCH_DB),
        "db_file_exists": SCRATCH_DB.exists(),
        "db_file_size_bytes": SCRATCH_DB.stat().st_size if SCRATCH_DB.exists() else None,
        "wal_file_exists": Path(str(SCRATCH_DB) + "-wal").exists(),
    })


def read_phase():
    client = _client()
    from sibyl_memory_client.exceptions import NotFoundError
    try:
        row = client.get_entity("probe", "cold_start_check")
    except NotFoundError as e:
        row = {"NotFoundError": str(e)}
    _dump("PART 1 / read_phase (pid=%d, fresh process)" % os.getpid(), {
        "get_entity_result": row,
        "db_file_exists": SCRATCH_DB.exists(),
        "db_file_size_bytes": SCRATCH_DB.stat().st_size if SCRATCH_DB.exists() else None,
        "wal_file_exists": Path(str(SCRATCH_DB) + "-wal").exists(),
    })


def part1_cold_start():
    print("\n" + "=" * 70)
    print("PART 1: cold-start persistence (separate OS processes)")
    print("=" * 70)
    print(f"orchestrator pid={os.getpid()}")
    r1 = subprocess.run([sys.executable, __file__, "write_phase"])
    r2 = subprocess.run([sys.executable, __file__, "read_phase"])
    _dump("PART 1 / subprocess exit codes", {
        "write_phase_returncode": r1.returncode,
        "read_phase_returncode": r2.returncode,
    })


# ---------------------------------------------------------------------------
# Part 2: tokenizer behaviour + cross-contract neighbour retrieval
# ---------------------------------------------------------------------------
def _raw_fts_query(client, query, *, prefix=False):
    """Same MATCH the public search_entities() runs, plus the FTS5 rank column
    (hidden in the public API) so ordering/relevance is visible."""
    from sibyl_memory_client.client import _sanitize_fts5_query
    match_q = _sanitize_fts5_query(query, prefix=prefix)
    if not match_q:
        return {"sanitized_query": match_q, "rows": []}
    with client.storage.connection() as conn:
        rows = conn.execute(
            "SELECT e.name, e.category, f.rank AS relevance "
            "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
            "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
            "ORDER BY f.rank LIMIT 20",
            (match_q, client.get_tenant()),
        ).fetchall()
    return {
        "sanitized_query": match_q,
        "rows": [{"name": r["name"], "category": r["category"], "rank": r["relevance"]} for r in rows],
    }


def part2_tokenizer():
    print("\n" + "=" * 70)
    print("PART 2: tokenizer behaviour + cross-contract neighbour retrieval")
    print("=" * 70)
    client = _client()

    seed_entities = [
        ("failure", "routerA/swapExactTokensForTokens/slippage-revert",
         {"note": "contract A, swapExactTokensForTokens signature, reverted on slippage"}),
        ("failure", "routerB/exactInputSingle/slippage-revert",
         {"note": "contract B, exactInputSingle signature, reverted on slippage"}),
        ("failure", "unrelated/approve/allowance-revert",
         {"note": "unrelated failure class entirely, reverted on allowance"}),
        ("failure", "revert-stem-check-a", {"note": "this transaction reverted"}),
        ("failure", "revert-stem-check-b", {"note": "reverting behaviour observed repeatedly"}),
    ]
    written = []
    for category, name, body in seed_entities:
        written.append(client.set_entity(category, name, body))
    _dump("PART 2 / seeded entities", written)

    queries = [
        ("slippage revert", False),   # THE load-bearing query: cross-contract neighbour retrieval
        ("router", False),
        ("slippage", False),
        ("revert", False),
        ("reverting", False),
        ("swapExactTokensForTokens", False),
        ("swap", False),
        ("tokens", False),
        ("exactInputSingle", False),
        ("swapExact", True),          # prefix mode
    ]

    for q, prefix in queries:
        public_result = client.search_entities(q, prefix=prefix)
        raw_fts = _raw_fts_query(client, q, prefix=prefix)
        _dump(f"PART 2 / search_entities(query={q!r}, prefix={prefix})", {
            "public_api_result": public_result,
            "raw_fts_with_rank": raw_fts,
        })


# ---------------------------------------------------------------------------
# Part 3: status trap + revision identity (created_at/updated_at/id)
# ---------------------------------------------------------------------------
def part3_status_trap():
    print("\n" + "=" * 70)
    print("PART 3: status trap + revision identity")
    print("=" * 70)
    client = _client()

    first = client.set_entity("probe", "status_check", {"v": 1}, status="active")
    _dump("PART 3 / after first write (status='active')", first)

    before_second_write = client.get_entity("probe", "status_check")
    _dump("PART 3 / full row BEFORE second write", before_second_write)

    second = client.set_entity("probe", "status_check", {"v": 2})  # status omitted
    _dump("PART 3 / set_entity() return value from second write (status omitted)", second)

    after_second_write = client.get_entity("probe", "status_check")
    _dump("PART 3 / full row AFTER second write", after_second_write)

    _dump("PART 3 / id/created_at/updated_at comparison", {
        "id_before": before_second_write["id"],
        "id_after": after_second_write["id"],
        "id_unchanged": before_second_write["id"] == after_second_write["id"],
        "created_at_before": before_second_write["created_at"],
        "created_at_after": after_second_write["created_at"],
        "created_at_unchanged": before_second_write["created_at"] == after_second_write["created_at"],
        "updated_at_before": before_second_write["updated_at"],
        "updated_at_after": after_second_write["updated_at"],
        "status_before": before_second_write["status"],
        "status_after": after_second_write["status"],
    })


# ---------------------------------------------------------------------------
# Part 4: free-tier cap figure
# ---------------------------------------------------------------------------
def part4_cap():
    print("\n" + "=" * 70)
    print("PART 4: free-tier cap figure")
    print("=" * 70)
    from sibyl_memory_client.lint import TIER_SOFT_CAPS, DEFAULT_SOFT_CAP_BYTES
    _dump("PART 4 / lint.py constants (configured)", {
        "DEFAULT_SOFT_CAP_BYTES": DEFAULT_SOFT_CAP_BYTES,
        "TIER_SOFT_CAPS": TIER_SOFT_CAPS,
    })
    client = _client()
    _dump("PART 4 / client.free_tier_status() (reported by running client)", client.free_tier_status())


def main():
    if SCRATCH_DIR.exists():
        for p in SCRATCH_DIR.glob("probe.db*"):
            p.unlink()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    part1_cold_start()
    part2_tokenizer()
    part3_status_trap()
    part4_cap()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "write_phase":
        write_phase()
    elif len(sys.argv) > 1 and sys.argv[1] == "read_phase":
        read_phase()
    else:
        main()
