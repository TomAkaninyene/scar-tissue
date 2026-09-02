#!/usr/bin/env python3
"""The deletion test: the same intent, with memory and without it.

Runs one intent whose min-out is deliberately too high, repeatedly, with
memory ON. Each revert files a lesson; each retrieval widens the tolerance
a little further; eventually the same intent goes through. Then it runs
THAT SAME INTENT with memory OFF, against THE SAME lesson store, and the
swap reverts again — because nothing reads what is sitting right there.

That contrast is the claim of this project, so it is a flag and a script,
not a code edit:

    .venv/bin/python scripts/deletion_test.py

Everything happens inside evm_snapshot/evm_revert, so the fork is left
exactly as it was found. The lesson store defaults to a throwaway
database so the run starts having learned nothing and is repeatable on
camera; pass --db to use a real one.

Exit status: 0 the contrast held, 1 it did not, 2 the environment is not
ready.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent import (  # noqa: E402
    BASE_SWAP_ROUTER_02,
    BASE_USDC,
    BASE_WETH,
    ERC20_ABI,
    ROUTER_ABI,
    Agent,
    SwapIntent,
    _wire,
)
from lessons import LessonStore, open_client  # noqa: E402
from memory import DB_PATH  # noqa: E402

DEFAULT_RPC = "http://127.0.0.1:8545"
# How far above the pool's real output the intent asks for. Must be
# reachable inside agent.MAX_SLIPPAGE_REDUCTION_BPS or the loop can never
# converge, which would make this a test of the cap, not of memory.
DEFAULT_OVERSHOOT_BPS = 2_000
DEFAULT_MAX_RUNS = 8


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", default=os.environ.get("RPC_URL", DEFAULT_RPC))
    parser.add_argument("--db", type=Path, default=None,
                        help=f"lesson store to use (default: a temp database; "
                             f"the project's is {DB_PATH})")
    parser.add_argument("--amount-in", type=int, default=10**16,
                        help="input amount in base units (default 0.01 WETH)")
    parser.add_argument("--overshoot-bps", type=int,
                        default=DEFAULT_OVERSHOOT_BPS,
                        help="how far above the real quote to ask for")
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    return parser.parse_args(argv)


def rpc(w3, method, params=None):
    response = w3.provider.make_request(method, params or [])
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


def send(w3, account, tx):
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = signed.rawTransaction
    return w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(raw))


def tx_defaults(w3, account, gas):
    return {
        "from": _wire(account.address),
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(_wire(account.address)),
        "chainId": w3.eth.chain_id,
        "value": 0,
    }


def fund(w3, account, token_in, router, amount):
    """Wrap and approve enough for every run, so nothing fails on funding.

    Reverted swaps cost only gas, but the successful one spends amountIn
    and its allowance. Funding 3x keeps the --no-memory run failing for
    the reason under test rather than for want of tokens.
    """
    weth = w3.eth.contract(address=_wire(token_in), abi=[
        {"inputs": [], "name": "deposit", "outputs": [],
         "stateMutability": "payable", "type": "function"},
    ] + ERC20_ABI)
    deposit = weth.functions.deposit().build_transaction(
        {**tx_defaults(w3, account, 100_000), "value": amount})
    send(w3, account, deposit)
    approve = weth.functions.approve(_wire(router), amount).build_transaction(
        tx_defaults(w3, account, 100_000))
    send(w3, account, approve)


def quote(w3, account, intent: SwapIntent) -> int:
    """Ask the pool what this swap actually returns, without submitting.

    A static call runs the swap and hands back amountOut, so the intent
    can be set a known distance above a real number instead of a guess.
    """
    router = w3.eth.contract(address=_wire(intent.router), abi=ROUTER_ABI)
    return router.functions.exactInputSingle(
        intent.params(0)).call({"from": _wire(account.address)})


def column(runs, label):
    return {
        "label": label,
        "txHash": runs["txHash"],
        "status": runs["status"],
        "minOut": runs["amountOutMinimumSubmitted"],
        "applied": runs["lesson_applied"],
        "failureClass": runs["failureClass"],
    }


def short_hash(tx_hash: str) -> str:
    """First and last bytes, for a table that has to stay under 80 columns.

    The full hashes are printed underneath: a judge verifies against those.
    """
    return f"{tx_hash[:10]}\u2026{tx_hash[-8:]}"


def describe_applied_short(applied) -> str:
    for change in applied["changes"]:
        if change.get("field") == "amountOutMinimum":
            return f"yes, -{change['reductionBps']} bps"
    return "no, retrieval skipped" if not applied["changes"] else "not applied"


def describe_applied(applied) -> str:
    if not applied["changes"]:
        return f"no — {applied['reason']}"
    for change in applied["changes"]:
        if change.get("field") == "amountOutMinimum":
            return (f"yes — min-out {change['from']} to {change['to']} "
                    f"(-{change['reductionBps']} bps)")
    return f"retrieved, not applied — {applied['reason']}"


def main(argv=None) -> int:
    args = parse_args(argv)

    key = os.environ.get("PRIVATE_KEY")
    if not key:
        print("PRIVATE_KEY is not set. Put a BURNER key in .env (gitignored); "
              "on the anvil fork, anvil's own dev key is the right one.",
              file=sys.stderr)
        return 2

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        print(f"no node at {args.rpc}", file=sys.stderr)
        return 2
    account = w3.eth.account.from_key(key)

    try:
        snapshot = rpc(w3, "evm_snapshot")
    except RuntimeError:
        print(f"{args.rpc} does not support evm_snapshot. This demo only runs "
              "against the local anvil fork.", file=sys.stderr)
        return 2

    scratch = None
    if args.db is None:
        scratch = tempfile.TemporaryDirectory()
        db_path = Path(scratch.name) / "deletion_test.db"
    else:
        db_path = args.db
    store = LessonStore(open_client(db_path))

    print(f"node        : {w3.client_version} @ block {w3.eth.block_number}")
    print(f"account     : {account.address}")
    print(f"lesson store: {db_path}")
    print(f"snapshot    : {snapshot}")

    try:
        fund(w3, account, BASE_WETH, BASE_SWAP_ROUTER_02, 3 * args.amount_in)

        probe_intent = SwapIntent(
            router=BASE_SWAP_ROUTER_02, token_in=BASE_WETH,
            token_out=BASE_USDC, fee=500, amount_in=args.amount_in,
            amount_out_minimum=0, recipient=account.address)
        real_out = quote(w3, account, probe_intent)
        asked = real_out * (10_000 + args.overshoot_bps) // 10_000
        intent = SwapIntent(
            router=BASE_SWAP_ROUTER_02, token_in=BASE_WETH,
            token_out=BASE_USDC, fee=500, amount_in=args.amount_in,
            amount_out_minimum=asked, recipient=account.address)

        print(f"\npool returns: {real_out} USDC base units for "
              f"{args.amount_in} WETH")
        print(f"intent asks : {asked} "
              f"(+{args.overshoot_bps} bps — unreachable as submitted)")

        print("\n--- memory ON, same intent, until the lesson is enough -----")
        agent = Agent(w3, account, store)
        winner = None
        for attempt in range(1, args.max_runs + 1):
            result = agent.run(intent)
            print(f"  run {attempt}: min-out {result['amountOutMinimumSubmitted']:<12} "
                  f"status {result['status']}  "
                  f"{result['failureClass'] or 'SUCCESS'}  "
                  f"[{describe_applied(result['lesson_applied'])}]")
            if result["status"] == 1:
                winner = result
                break
        if winner is None:
            print(f"\nno success in {args.max_runs} runs — the lesson never "
                  "widened enough. Nothing is demonstrated.", file=sys.stderr)
            return 1

        print("\n--- the SAME intent, --no-memory, SAME store ---------------")
        blind = Agent(w3, account, store, use_memory=False).run(intent)
        print(f"  run  : min-out {blind['amountOutMinimumSubmitted']:<12} "
              f"status {blind['status']}  "
              f"{blind['failureClass'] or 'SUCCESS'}  "
              f"[{describe_applied(blind['lesson_applied'])}]")

        left = column(winner, "memory ON")
        right = column(blind, "--no-memory")
        width = 26
        rows = [
            ("tx hash", short_hash(left["txHash"]), short_hash(right["txHash"])),
            ("status", f"{left['status']}  (success)",
             f"{right['status']}  (reverted)"),
            ("min-out submitted", str(left["minOut"]), str(right["minOut"])),
            ("failure class", str(left["failureClass"] or "—"),
             str(right["failureClass"] or "—")),
            ("lesson applied", describe_applied_short(left["applied"]),
             describe_applied_short(right["applied"])),
        ]
        rule_width = 20 + 2 * width
        print("\n" + "=" * rule_width)
        print(f"  {'':<18}{left['label']:<{width}}{right['label']:<{width}}")
        print("  " + "-" * (rule_width - 4))
        for label, a, b in rows:
            print(f"  {label:<18}{a:<{width}}{b:<{width}}")
        print("=" * rule_width)
        print("\n  full tx hashes")
        print(f"    {left['label']:<12}: {left['txHash']}")
        print(f"    {right['label']:<12}: {right['txHash']}")

        held = winner["status"] == 1 and blind["status"] == 0
        print("\nSame intent. Same store. The only difference is whether the "
              "agent read it." if held else
              "\nThe contrast did NOT hold — see the runs above.")
        return 0 if held else 1
    finally:
        print(f"\nevm_revert  : {rpc(w3, 'evm_revert', [snapshot])} "
              f"(block {w3.eth.block_number})")
        if scratch is not None:
            scratch.cleanup()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is pinned
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    sys.exit(main())
