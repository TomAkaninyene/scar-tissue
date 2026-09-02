"""The execution loop: retrieve, decide, submit, learn.

One pass of this loop is the whole product. It retrieves what the agent
learned the last time it failed this way, applies it, records what it
believed before acting, submits, and — when the transaction reverts —
classifies the failure and revises the lesson.

The evidence behind every rule here is in CLAUDE.md. The ones that shape
this module:

  - GAS IS EXPLICIT. estimate_gas RAISES on a call that will revert, so an
    estimated transaction never reaches the chain, produces no receipt and
    therefore no post-mortem. A failure the agent cannot observe is a
    failure it cannot learn from.
  - THE POST-MORTEM IS TWO-STEP. The receipt says only THAT it failed;
    classify() re-calls at the failing block to learn why.
  - NONE MEANS UNCLASSIFIED. When classify returns None the journal still
    records the run, but NO lesson is written. A guessed correction is a
    wrong standing correction the next run retrieves and acts on.
  - LESSONS ARE READ EXACTLY, NEIGHBOURS ARE READ BY ONE TOKEN. The
    neighbour query here is the lowercased router address — every failure
    seen on this contract — never prose.

--no-memory removes RETRIEVAL, not the record. No get_lesson, no
find_neighbours, and no write_lesson either, because write_lesson reads
the current row to increment evidence_count and that read is a retrieval.
Post-mortems and open_call claims are still written, so a run without
memory is still fully evidenced — it just cannot benefit from anything it
evidenced before. That is the deletion test, and it is a FLAG.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from web3 import Web3

from failures import (
    ALLOWANCE_REVERT,
    BALANCE_REVERT,
    SLIPPAGE_REVERT,
    classify,
)
from lessons import LessonStore
from memory import build_signature, normalize_address

# --------------------------------------------------------------------------
# Execution constants
# --------------------------------------------------------------------------
# Never estimated. See the module docstring: an estimate raises before the
# transaction is sent, and an unsent transaction cannot be learned from.
GAS_LIMIT = 400_000

# The only function this agent calls. camelCase exactly as in the ABI,
# because it is a sig_v1 segment and camelCase is atomic under unicode61.
FUNCTION_NAME = "exactInputSingle"

# Classes this agent can be corrected for. balanceRevert is retrieved and
# recorded like the others but has NO pre-flight remedy: the agent cannot
# give itself tokens.
IN_SCOPE_CLASSES = (SLIPPAGE_REVERT, ALLOWANCE_REVERT, BALANCE_REVERT)

BPS_DENOMINATOR = 10_000
# How much a standing slippage lesson widens tolerance per prior failure,
# and the ceiling it is never allowed to cross. The ceiling matters: the
# correction must not be able to talk the agent into accepting any fill at
# all after enough failures.
SLIPPAGE_STEP_BPS = 500
MAX_SLIPPAGE_REDUCTION_BPS = 3_000

ROUTER_ABI = [{
    "inputs": [{
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params",
        "type": "tuple",
    }],
    "name": FUNCTION_NAME,
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
    "type": "function",
}]

ERC20_ABI = [
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def _wire(address: str) -> str:
    """Lowercase at the boundary, checksum only for web3.

    Same asymmetry failures.py documents: web3 8.0.0 takes a lowercase
    `to` but raises InvalidAddress on a lowercase ABI argument. Nothing
    checksummed is stored or returned.
    """
    return Web3.to_checksum_address(normalize_address(address))


@dataclass(frozen=True)
class SwapIntent:
    """What the caller wants to do, before memory has had its say."""

    router: str
    token_in: str
    token_out: str
    fee: int
    amount_in: int
    amount_out_minimum: int
    recipient: str

    def params(self, amount_out_minimum: int) -> tuple:
        """ABI params for exactInputSingle, with a possibly revised min-out."""
        return (
            _wire(self.token_in),
            _wire(self.token_out),
            int(self.fee),
            _wire(self.recipient),
            int(self.amount_in),
            int(amount_out_minimum),
            0,  # sqrtPriceLimitX96: unset, the pool decides
        )


class Agent:
    """One swap, one memory pass."""

    def __init__(self, w3, account, store: LessonStore, *,
                 use_memory: bool = True, gas_limit: int = GAS_LIMIT):
        self._w3 = w3
        self._account = account
        self._store = store
        self._use_memory = use_memory
        self._gas_limit = gas_limit

    # -- public ----------------------------------------------------------
    def run(self, intent: SwapIntent) -> dict[str, Any]:
        """Execute one swap and return everything the run learned.

        The returned dict always carries `lesson_applied`, whether or not
        anything was applied and whether or not memory was consulted.
        """
        tx_ref = uuid4().hex
        signatures = {
            failure_class: build_signature(
                intent.router, FUNCTION_NAME, failure_class)
            for failure_class in IN_SCOPE_CLASSES
        }

        standing, neighbours = self._retrieve(intent, signatures)
        amount_out_minimum, lesson_applied = self._decide(intent, standing)

        claim = {
            "txRef": tx_ref,
            "memory": "off" if not self._use_memory else "on",
            "signatures": signatures,
            "standingLessons": {
                failure_class: self._summarise(lesson)
                for failure_class, lesson in standing.items()
                if lesson is not None
            },
            "neighbours": neighbours,
            "params": {
                "router": normalize_address(intent.router),
                "function": FUNCTION_NAME,
                "tokenIn": normalize_address(intent.token_in),
                "tokenOut": normalize_address(intent.token_out),
                "fee": intent.fee,
                "amountIn": intent.amount_in,
                "amountOutMinimumIntended": intent.amount_out_minimum,
                "amountOutMinimumSubmitted": amount_out_minimum,
            },
            "lesson_applied": lesson_applied,
        }
        # Written BEFORE anything is submitted. A claim with no outcome is
        # itself evidence that the process died mid-flight.
        self._store.write_open_call(tx_ref, claim)

        approve_hash = None
        if lesson_applied["approve_first"]:
            approve_hash = self._approve(intent)

        receipt, tx_hash, calldata = self._submit(intent, amount_out_minimum)

        result = {
            "txRef": tx_ref,
            "memory": claim["memory"],
            "signatures": signatures,
            "lesson_applied": lesson_applied,
            "amountOutMinimumIntended": intent.amount_out_minimum,
            "amountOutMinimumSubmitted": amount_out_minimum,
            "approveTxHash": approve_hash,
            "txHash": tx_hash,
            "status": receipt["status"],
            "blockNumber": receipt["blockNumber"],
            "failureClass": None,
            "signature": None,
            "lessonWritten": None,
            "postMortem": None,
        }

        if receipt["status"] == 1:
            claim["outcome"] = {"status": 1, "txHash": tx_hash}
            self._store.write_open_call(tx_ref, claim)
            return result

        self._learn(intent, signatures, receipt, tx_hash, calldata,
                    amount_out_minimum, lesson_applied, standing, result)
        claim["outcome"] = {
            "status": 0,
            "txHash": tx_hash,
            "failureClass": result["failureClass"],
        }
        self._store.write_open_call(tx_ref, claim)
        return result

    # -- retrieval -------------------------------------------------------
    def _retrieve(self, intent: SwapIntent, signatures: dict[str, str]):
        """Exact lookups plus one neighbour query. Skipped entirely if off.

        get_lesson catches NotFoundError and returns None; a signature the
        agent has never failed on is the NORMAL path, not an error.
        """
        if not self._use_memory:
            return {}, []

        standing = {
            failure_class: self._store.get_lesson(sig)
            for failure_class, sig in signatures.items()
        }
        # ONE token, never prose: the lowercased router address is a single
        # unicode61 token and matches every failure seen on this contract.
        neighbours = [
            hit["name"] for hit in self._store.find_neighbours(
                normalize_address(intent.router))
        ]
        return standing, neighbours

    # -- decision --------------------------------------------------------
    def _decide(self, intent: SwapIntent, standing: dict[str, Any]):
        """Apply standing lessons to the intended parameters."""
        applied = {
            "applied": False,
            "approve_first": False,
            "changes": [],
            "reason": ("retrieval skipped (--no-memory)"
                       if not self._use_memory
                       else "no standing lesson for this signature"),
        }
        amount_out_minimum = intent.amount_out_minimum

        slippage = standing.get(SLIPPAGE_REVERT)
        if slippage is not None:
            reduction = self._reduction_bps(self._evidence_count(slippage))
            revised = (intent.amount_out_minimum
                       * (BPS_DENOMINATOR - reduction)) // BPS_DENOMINATOR
            # A correction may widen the tolerance. It may NEVER zero the
            # protection: amountOutMinimum 0 accepts ANY fill, including
            # none at all, and integer division reaches 0 on small
            # intended values long before the bps ceiling bites.
            floored = intent.amount_out_minimum > 0 and revised < 1
            if floored:
                revised = 1
            applied["changes"].append({
                "failureClass": SLIPPAGE_REVERT,
                "correction": slippage["body"].get("correction"),
                "evidenceCount": self._evidence_count(slippage),
                "field": "amountOutMinimum",
                "from": intent.amount_out_minimum,
                "to": revised,
                "reductionBps": reduction,
                "floored": floored,
            })
            amount_out_minimum = revised

        allowance = standing.get(ALLOWANCE_REVERT)
        if allowance is not None:
            applied["approve_first"] = True
            applied["changes"].append({
                "failureClass": ALLOWANCE_REVERT,
                "correction": allowance["body"].get("correction"),
                "evidenceCount": self._evidence_count(allowance),
                "field": "allowance",
                "action": f"approve {intent.amount_in} before submitting",
            })

        balance = standing.get(BALANCE_REVERT)
        if balance is not None:
            # Retrieved and recorded, never applied: the agent cannot give
            # itself tokens. It is reported so a run that is doomed for a
            # known reason says so before it burns the gas.
            applied["changes"].append({
                "failureClass": BALANCE_REVERT,
                "correction": balance["body"].get("correction"),
                "evidenceCount": self._evidence_count(balance),
                "field": None,
                "action": "not applied — no pre-flight remedy exists",
            })

        if applied["changes"]:
            applied["applied"] = any(
                change.get("field") is not None for change in applied["changes"])
            applied["reason"] = (
                "standing lesson(s) retrieved and applied" if applied["applied"]
                else "standing lesson(s) retrieved, none applicable pre-flight")
        return amount_out_minimum, applied

    # -- execution -------------------------------------------------------
    def _approve(self, intent: SwapIntent) -> str:
        """Approve exactly amountIn. Never an unlimited allowance."""
        token = self._w3.eth.contract(
            address=_wire(intent.token_in), abi=ERC20_ABI)
        tx = token.functions.approve(
            _wire(intent.router), int(intent.amount_in),
        ).build_transaction(self._tx_defaults(gas=100_000))
        return self._send(tx)[1]

    def _submit(self, intent: SwapIntent, amount_out_minimum: int):
        router = self._w3.eth.contract(
            address=_wire(intent.router), abi=ROUTER_ABI)
        tx = router.functions.exactInputSingle(
            intent.params(amount_out_minimum),
        ).build_transaction(self._tx_defaults())
        receipt, tx_hash = self._send(tx)
        # The calldata is carried forward rather than re-fetched: classify
        # needs the exact bytes that were submitted, and this module
        # already has them.
        return receipt, tx_hash, tx["data"]

    def _tx_defaults(self, *, gas: int | None = None) -> dict[str, Any]:
        return {
            "from": _wire(self._account.address),
            "gas": gas if gas is not None else self._gas_limit,
            "gasPrice": self._w3.eth.gas_price,
            "nonce": self._w3.eth.get_transaction_count(
                _wire(self._account.address)),
            "chainId": self._w3.eth.chain_id,
            "value": 0,
        }

    def _send(self, tx: dict[str, Any]):
        signed = self._account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = signed.rawTransaction
        tx_hash = self._w3.eth.send_raw_transaction(raw)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
        # to_hex, not .hex(): hexbytes 2.0 returns bare hex with no 0x.
        return receipt, Web3.to_hex(tx_hash)

    # -- learning --------------------------------------------------------
    def _learn(self, intent, signatures, receipt, tx_hash, calldata,
               amount_out_minimum, lesson_applied, standing, result) -> None:
        """Classify the revert, journal it, and revise the lesson."""
        tx_params = {
            "from": self._account.address,
            "to": intent.router,
            "data": calldata,
            "token": intent.token_in,
            "amountIn": intent.amount_in,
        }
        failure_class = classify(self._w3, receipt, tx_params)
        result["failureClass"] = failure_class

        signature = signatures.get(failure_class) if failure_class else None
        result["signature"] = signature

        correction = (
            self._correction_for(failure_class, intent, standing)
            if failure_class
            else "unclassified revert — no lesson filed, no correction claimed"
        )

        evaluated = {
            "signatures": signatures,
            "amountIn": intent.amount_in,
            "amountOutMinimumIntended": intent.amount_out_minimum,
            "amountOutMinimumSubmitted": amount_out_minimum,
            "lesson_applied": lesson_applied,
            "memory": "off" if not self._use_memory else "on",
        }
        # Native columns only, and nothing bulky: no receipts, no market
        # data. The free tier is 5 MB and an empty schema already costs
        # ~274 KB.
        acted = {
            "txHash": tx_hash,
            "status": receipt["status"],
            "blockNumber": receipt["blockNumber"],
            "amountOutMinimum": amount_out_minimum,
        }
        result["postMortem"] = self._store.record_postmortem(
            signature, evaluated=evaluated, acted=acted, correction=correction)

        if failure_class is None:
            return
        if not self._use_memory:
            # write_lesson READS the current row to increment
            # evidence_count. Under --no-memory that read is a retrieval,
            # so the lesson is not written at all — the journal above is
            # the whole record of this run.
            result["lessonWritten"] = None
            return

        evidence = {
            "failureClass": failure_class,
            "txHash": tx_hash,
            "blockNumber": receipt["blockNumber"],
            "amountIn": intent.amount_in,
            "amountOutMinimum": amount_out_minimum,
        }
        written = self._store.write_lesson(signature, correction, evidence)
        result["lessonWritten"] = {
            "signature": signature,
            "correction": correction,
            "evidenceCount": self._evidence_count(written),
        }

    def _correction_for(self, failure_class: str, intent: SwapIntent,
                        standing: dict[str, Any]) -> str:
        """The standing correction to file, stated as the next run's action."""
        # The count the row is ABOUT to carry: what this failure will make
        # the next run do, not what the last one did.
        next_count = self._evidence_count(standing.get(failure_class)) + 1

        if failure_class == SLIPPAGE_REVERT:
            return (
                f"reduce amountOutMinimum by "
                f"{self._reduction_bps(next_count)} bps before retrying "
                f"{FUNCTION_NAME} on this pool"
            )
        if failure_class == ALLOWANCE_REVERT:
            return (
                f"approve at least amountIn of "
                f"{normalize_address(intent.token_in)} to the router before "
                f"calling {FUNCTION_NAME}"
            )
        if failure_class == BALANCE_REVERT:
            return (
                f"hold at least amountIn of "
                f"{normalize_address(intent.token_in)} before calling "
                f"{FUNCTION_NAME}; this cannot be corrected pre-flight"
            )
        raise ValueError(f"no correction defined for {failure_class!r}")

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _evidence_count(lesson: dict[str, Any] | None) -> int:
        if lesson is None:
            return 0
        try:
            return int((lesson.get("body") or {}).get("evidence_count", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _reduction_bps(evidence_count: int) -> int:
        return min(SLIPPAGE_STEP_BPS * max(evidence_count, 1),
                   MAX_SLIPPAGE_REDUCTION_BPS)

    @staticmethod
    def _summarise(lesson: dict[str, Any]) -> dict[str, Any]:
        body = lesson.get("body") or {}
        return {
            "correction": body.get("correction"),
            "evidenceCount": body.get("evidence_count"),
            "firstSeen": lesson.get("created_at"),
            "lastRevised": lesson.get("updated_at"),
        }


# --------------------------------------------------------------------------
# CLI — the deletion test is this flag, not a code edit
# --------------------------------------------------------------------------
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"
DEFAULT_RPC = "http://127.0.0.1:8545"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Execute one swap, learning from prior failures.")
    parser.add_argument("--no-memory", action="store_true",
                        help="skip retrieval entirely: no get_lesson, no "
                             "find_neighbours, no lesson written. The swap "
                             "still runs and the post-mortem is still "
                             "journalled.")
    parser.add_argument("--rpc", default=os.environ.get("RPC_URL", DEFAULT_RPC),
                        help=f"node to execute against (default {DEFAULT_RPC}, "
                             "the local anvil fork)")
    parser.add_argument("--router", default=BASE_SWAP_ROUTER_02)
    parser.add_argument("--token-in", default=BASE_WETH)
    parser.add_argument("--token-out", default=BASE_USDC)
    parser.add_argument("--fee", type=int, default=500)
    parser.add_argument("--amount-in", type=int, default=10**16,
                        help="input amount in base units (default 0.01 WETH)")
    parser.add_argument("--min-out", type=int, required=True,
                        help="intended amountOutMinimum in base units")
    return parser.parse_args(argv)


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
    intent = SwapIntent(
        router=args.router,
        token_in=args.token_in,
        token_out=args.token_out,
        fee=args.fee,
        amount_in=args.amount_in,
        amount_out_minimum=args.min_out,
        recipient=account.address,
    )
    agent = Agent(w3, account, LessonStore(), use_memory=not args.no_memory)
    print(json.dumps(agent.run(intent), indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is pinned in requirements
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    sys.exit(main())
