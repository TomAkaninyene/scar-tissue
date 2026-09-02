"""Throwaway probe: do REAL Base contract identifiers survive unicode61?

Prints RAW results only. No interpretation, no pass/fail. Run with no args.

Open question this settles (FINDINGS.md section 9, "NO substring matching"):
    => RULE: every signature segment must be a COMPLETE token you will query in
    full. Test real contract identifiers against this before freezing sig_v1.
Section 7's probe used placeholder names (routerA/routerB). This one uses the
actual on-chain addresses a signature would carry.

Addresses are the real Base mainnet deployments, EIP-55 checksummed, taken from
the official Uniswap deployment docs and cross-checked on Basescan:
    Uniswap v3 SwapRouter02  0x2626664c2603336E57B271c5C0b26F421741e481
    Uniswap v2 Router02      0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24
    (https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments
     https://developers.uniswap.org/docs/protocols/v2/deployments)

Scratch DB only -- never touches ./data/reflex.db.
"""
import json
import sys
from pathlib import Path

SCRATCH_DB = Path(__file__).parent / "sig_scratch.db"

sys.path.insert(0, "/root/sibyl-probe/.venv/lib/python3.12/site-packages")

# Real Base mainnet routers, as a signature would store them: checksummed.
SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"   # Uniswap v3
V2_ROUTER_02 = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"     # Uniswap v2
HUMAN_NAME = "uniswap-v3-swaprouter02"  # control: human-readable, no address

ADDR_PREFIX = SWAP_ROUTER_02[:10]  # "0x2626664c" -- 0x + 8 hex chars


def _client():
    from sibyl_memory_client import MemoryClient
    return MemoryClient.local(str(SCRATCH_DB), tier="free")


def _dump(label, obj):
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, default=str))


def _raw_fts_query(client, query, *, prefix=False):
    """Same MATCH the public search_entities() runs, plus the FTS5 rank column
    (hidden in the public API) so ordering/relevance is visible.

    Secondary sort on e.name per the section 9 tie-breaking rule -- equal BM25
    ranks are expected here and repeat runs must print identically.
    """
    from sibyl_memory_client.client import _sanitize_fts5_query
    match_q = _sanitize_fts5_query(query, prefix=prefix)
    if not match_q:
        return {"sanitized_query": match_q, "rows": []}
    with client.storage.connection() as conn:
        rows = conn.execute(
            "SELECT e.name, e.category, f.rank AS relevance "
            "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
            "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
            "ORDER BY f.rank, e.name LIMIT 20",
            (match_q, client.get_tenant()),
        ).fetchall()
    return {
        "sanitized_query": match_q,
        "rows": [{"name": r["name"], "category": r["category"], "rank": r["relevance"]} for r in rows],
    }


# ---------------------------------------------------------------------------
# Part 1: seed real-identifier signatures
# ---------------------------------------------------------------------------
def part1_seed():
    print("\n" + "=" * 70)
    print("PART 1: seed signatures built from real Base router addresses")
    print("=" * 70)
    client = _client()

    seed_entities = [
        # Two real addresses, signature-shaped: <address>/<function>/<failure-class>
        ("failure", f"{SWAP_ROUTER_02}/exactInputSingle/slippage-revert",
         {"note": "uniswap v3 SwapRouter02 on base, exactInputSingle, reverted on slippage"}),
        ("failure", f"{V2_ROUTER_02}/swapExactTokensForTokens/slippage-revert",
         {"note": "uniswap v2 Router02 on base, swapExactTokensForTokens, reverted on slippage"}),
        # Same address, different failure class: does an address query return both?
        ("failure", f"{SWAP_ROUTER_02}/exactInputSingle/deadline-revert",
         {"note": "uniswap v3 SwapRouter02 on base, exactInputSingle, reverted on deadline"}),
        # Control: human-readable name in the address slot, for comparison.
        ("failure", f"{HUMAN_NAME}/exactInputSingle/slippage-revert",
         {"note": "human-readable router name instead of an address, reverted on slippage"}),
    ]
    written = []
    for category, name, body in seed_entities:
        row = client.set_entity(category, name, body)
        written.append({"category": row["category"], "name": row["name"], "id": row["id"]})
    _dump("PART 1 / seeded entities (names only)", written)


# ---------------------------------------------------------------------------
# Part 2: does a real checksummed address survive unicode61 tokenization?
# ---------------------------------------------------------------------------
def part2_queries():
    print("\n" + "=" * 70)
    print("PART 2: search_entities() against real contract identifiers")
    print("=" * 70)
    client = _client()

    queries = [
        (SWAP_ROUTER_02, False),           # address exactly as stored (checksummed)
        (SWAP_ROUTER_02.lower(), False),   # same address, lowercased
        (SWAP_ROUTER_02.upper(), False),   # same address, uppercased
        (ADDR_PREFIX, False),              # address prefix, no prefix matching
        (ADDR_PREFIX, True),               # same prefix, prefix matching on
        ("slippage revert", False),        # cross-contract neighbour retrieval
        ("exactInputSingle", False),       # camelCase function name
        (HUMAN_NAME, False),               # human-readable router name
    ]

    for q, prefix in queries:
        public_result = client.search_entities(q, prefix=prefix)
        raw_fts = _raw_fts_query(client, q, prefix=prefix)
        _dump(f"PART 2 / search_entities(query={q!r}, prefix={prefix})", {
            "public_api_hit_count": len(public_result),
            "public_api_names": [r["name"] for r in public_result],  # names only, no bodies
            "raw_fts_with_rank": raw_fts,
        })


# ---------------------------------------------------------------------------
# Part 3: exact retrieval by full signature name (get_entity, no tokenizer)
# ---------------------------------------------------------------------------
def part3_exact_get():
    print("\n" + "=" * 70)
    print("PART 3: get_entity() on the exact full signature name")
    print("=" * 70)
    client = _client()
    from sibyl_memory_client.exceptions import NotFoundError

    target = f"{SWAP_ROUTER_02}/exactInputSingle/slippage-revert"
    try:
        row = client.get_entity("failure", target)
        result = {
            "category": row["category"],
            "name": row["name"],
            "id": row["id"],
            "name_matches_requested": row["name"] == target,
        }
    except NotFoundError as e:   # first-encounter is the normal path; never let it raise
        result = {"NotFoundError": str(e)}
    _dump(f"PART 3 / get_entity('failure', {target!r})", result)


# ---------------------------------------------------------------------------
# Part 4a: is the name lookup / UNIQUE constraint case-sensitive?
# ---------------------------------------------------------------------------
def part4a_name_case():
    print("\n" + "=" * 70)
    print("PART 4a: checksummed vs lowercased name on get_entity / set_entity")
    print("=" * 70)
    client = _client()
    from sibyl_memory_client.exceptions import NotFoundError

    stored = f"{V2_ROUTER_02}/removeLiquidity/case-check-revert"
    lowered = stored.lower()

    seeded = client.set_entity("failure", stored, {"note": "reverted while removing liquidity"})
    _dump("PART 4a / seeded entity (checksummed name)", {
        "category": seeded["category"], "name": seeded["name"], "id": seeded["id"],
    })
    _dump("PART 4a / the two names", {"stored": stored, "lowercased": lowered})

    try:
        row = client.get_entity("failure", lowered)
        got = {"outcome": "HIT", "name": row["name"], "id": row["id"]}
    except NotFoundError as e:
        got = {"outcome": "NotFoundError", "error": str(e)}
    _dump("PART 4a / get_entity('failure', <lowercased name>)", got)

    count_before = len(client.list_entities("failure", limit=1000))
    written = client.set_entity("failure", lowered, {"note": "reverted while removing liquidity"})
    count_after = len(client.list_entities("failure", limit=1000))
    _dump("PART 4a / set_entity('failure', <lowercased name>)", {
        "returned_name": written["name"],
        "returned_id": written["id"],
        "seeded_id": seeded["id"],
        "list_entities('failure')_count_before": count_before,
        "list_entities('failure')_count_after": count_after,
    })
    _dump("PART 4a / list_entities('failure') names after write", [
        r["name"] for r in client.list_entities("failure", limit=1000)
    ])


# ---------------------------------------------------------------------------
# Part 4b: camelCase failure class vs two-word prose query
# ---------------------------------------------------------------------------
def part4b_camel_failure_class():
    print("\n" + "=" * 70)
    print("PART 4b: camelCase failure classes (slippageRevert / allowanceRevert)")
    print("=" * 70)
    client = _client()

    seed_entities = [
        ("failure", f"{SWAP_ROUTER_02}/exactInputSingle/slippageRevert",
         {"note": "reverted on slippage tolerance"}),
        ("failure", f"{V2_ROUTER_02}/swapExactTokensForTokens/allowanceRevert",
         {"note": "reverted on slippage tolerance"}),
    ]
    written = []
    for category, name, body in seed_entities:
        row = client.set_entity(category, name, body)
        written.append({"category": row["category"], "name": row["name"], "id": row["id"]})
    _dump("PART 4b / seeded entities (names only)", written)

    for q in ("slippageRevert", "slippage revert"):
        public_result = client.search_entities(q, prefix=False)
        raw_fts = _raw_fts_query(client, q, prefix=False)
        _dump(f"PART 4b / search_entities(query={q!r}, prefix=False)", {
            "public_api_hit_count": len(public_result),
            "public_api_names": [r["name"] for r in public_result],  # names only, no bodies
            "raw_fts_with_rank": raw_fts,
        })


def main():
    for p in SCRATCH_DB.parent.glob(SCRATCH_DB.name + "*"):
        p.unlink()

    part1_seed()
    part2_queries()
    part3_exact_get()
    part4a_name_case()
    part4b_camel_failure_class()


if __name__ == "__main__":
    main()
