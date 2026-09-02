#!/usr/bin/env python3
"""THROWAWAY PROBE — not project code, not committed, not imported by src/.

Induce three failure classes against the local anvil fork and print exactly
what web3.py and the node hand back. No classification, no fixtures, no
interpretation. Exceptions are printed in full; nothing is swallowed.

  A. slippage  — impossible amountOutMinimum
  B. allowance — allowance set to 0, balance sufficient
  C. deadline  — multicall(deadline, data) with a deadline in the past
                 (exactInputSingle on SwapRouter02 has NO deadline field;
                  deadline enforcement lives in the multicall wrapper)
  D. balance   — allowance sufficient, balance BELOW amountIn

B and D both revert with the same string. Their allowance and balance are
read back at the failing block and printed side by side at the end, so the
state-based split is evidenced rather than asserted.

The whole run is bracketed by evm_snapshot/evm_revert, and each probe is
preceded by a revert, so every class starts from the same fork state and
the fork is left as it was found.

    .venv/bin/python probe_revert.py
"""
import json
import traceback

from web3 import Web3

RPC = "http://127.0.0.1:8545"

# anvil's default dev account 0 (public test mnemonic "test test ... junk").
ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
FEE = 500

AMOUNT_IN = Web3.to_wei(0.01, "ether")
# Impossible: 1e18 base units of 6-decimal USDC = a trillion USDC out of
# 0.01 WETH in. No pool state can satisfy it.
IMPOSSIBLE_OUT_MIN = 10**18
# Satisfiable by any non-zero fill, so slippage cannot be the cause.
TRIVIAL_OUT_MIN = 1
# Case D wraps this much and then tries to swap AMOUNT_IN, so the balance
# is short by design while the allowance covers the full amount.
SMALL_WRAP = Web3.to_wei(0.001, "ether")
# Case D sends the entry state's leftover WETH here first, so the balance
# at the failing block is exactly SMALL_WRAP and nothing is inferred.
BURN = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

ROUTER_ABI = [
    {
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
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "deadline", "type": "uint256"},
            {"name": "data", "type": "bytes[]"},
        ],
        "name": "multicall",
        "outputs": [{"name": "results", "type": "bytes[]"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

ERC20_ABI = [
    {"inputs": [], "name": "deposit", "outputs": [],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "dst", "type": "address"},
                {"name": "wad", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

w3 = Web3(Web3.HTTPProvider(RPC))
acct = w3.eth.account.from_key(PRIVATE_KEY)
weth = w3.eth.contract(address=WETH, abi=ERC20_ABI)
usdc = w3.eth.contract(address=USDC, abi=ERC20_ABI)
router = w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)


# --------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------
def rule(label, char="="):
    print(f"\n{char * 72}\n{label}\n{char * 72}")


def indent(text):
    return "".join("  | " + line for line in text.splitlines(keepends=True))


def dump_exception(exc):
    """Print everything the exception object carries. Interpret nothing."""
    print(f"  type(e).__name__      : {type(exc).__name__}")
    print(f"  type(e).__module__    : {type(exc).__module__}")
    print(f"  type(e).__mro__       : "
          f"{[c.__name__ for c in type(exc).__mro__[:6]]}")
    print(f"  len(e.args)           : {len(exc.args)}")
    for i, arg in enumerate(exc.args):
        print(f"  e.args[{i}] type       : {type(arg).__name__}")
        print(f"  e.args[{i}] repr       : {arg!r}")
    print(f"  str(e)                : {str(exc)!r}")
    print(f"  repr(e)               : {exc!r}")
    for attr in ("message", "data", "code", "rpc_response", "user_message"):
        if hasattr(exc, attr):
            print(f"  e.{attr:<19}: {getattr(exc, attr)!r}")
    print("  --- traceback ---")
    print(indent(traceback.format_exc()))


# --------------------------------------------------------------------------
# snapshot / revert
# --------------------------------------------------------------------------
def rpc(method, params=None):
    response = w3.provider.make_request(method, params or [])
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


SNAPSHOT = None
RESULTS = []


def take_snapshot(label):
    global SNAPSHOT
    SNAPSHOT = rpc("evm_snapshot")
    print(f"  evm_snapshot   : {SNAPSHOT}  ({label}, block "
          f"{w3.eth.block_number})")


def revert_snapshot(label):
    """Revert to the snapshot and immediately take a fresh one.

    evm_revert consumes the id, so a new snapshot is required to revert
    again. State after this call is identical for every probe.
    """
    ok = rpc("evm_revert", [SNAPSHOT])
    print(f"  evm_revert     : {ok}  ({label}, back to block "
          f"{w3.eth.block_number})")
    take_snapshot("re-armed")


# --------------------------------------------------------------------------
# chain helpers
# --------------------------------------------------------------------------
def send(tx, label):
    """Sign, send, wait. Explicit gas everywhere so nothing is estimated."""
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"  {label:<15}: {tx_hash.hex()} status={receipt['status']} "
          f"gasUsed={receipt['gasUsed']}")
    return receipt


def base_tx(**overrides):
    tx = {
        "from": acct.address,
        "gas": 400_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": w3.eth.chain_id,
        "value": 0,
    }
    tx.update(overrides)
    return tx


def calldata_of(fn):
    return fn.build_transaction(base_tx())["data"]


def wrap_and_set_allowance(allowance):
    send(weth.functions.deposit().build_transaction(
        base_tx(value=AMOUNT_IN, gas=100_000)), "wrap")
    send(weth.functions.approve(ROUTER, allowance).build_transaction(
        base_tx(gas=100_000)), "approve")
    print(f"  WETH balance   : {weth.functions.balanceOf(acct.address).call()}")
    print(f"  USDC balance   : {usdc.functions.balanceOf(acct.address).call()}")
    print(f"  allowance      : "
          f"{weth.functions.allowance(acct.address, ROUTER).call()}")


def probe(fn, label):
    """Four views of the same call: eth_call, estimate_gas, tx, raw RPC."""
    rule(f"{label} — eth_call via web3 .call()", "-")
    try:
        print(f"  NO REVERT. returned: {fn.call({'from': acct.address})!r}")
    except BaseException as e:
        dump_exception(e)

    rule(f"{label} — .estimate_gas()", "-")
    try:
        print(f"  NO REVERT. estimated: {fn.estimate_gas({'from': acct.address})!r}")
    except BaseException as e:
        dump_exception(e)

    rule(f"{label} — SUBMITTED TRANSACTION (explicit gas, so it mines)", "-")
    tx = fn.build_transaction(base_tx())
    calldata = tx["data"]
    print(f"  calldata       : {calldata}")
    receipt = None
    try:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        print(f"  tx_hash        : {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    except BaseException as e:
        dump_exception(e)

    rule(f"{label} — RECEIPT", "-")
    if receipt is None:
        print("  no receipt came back")
    else:
        for key in receipt.keys():
            value = receipt[key]
            if isinstance(value, bytes):
                value = value.hex()
            print(f"  {key:<21}: {value!r}")

    rule(f"{label} — RAW eth_call, unwrapped node response", "-")
    print(json.dumps(w3.provider.make_request("eth_call", [
        {"from": acct.address, "to": ROUTER, "data": calldata}, "latest",
    ]), indent=2, default=str))

    message = None
    if receipt is not None:
        rule(f"{label} — RAW eth_call REPLAYED at failing block - 1", "-")
        replay = w3.provider.make_request("eth_call", [
            {"from": acct.address, "to": ROUTER, "data": calldata},
            hex(receipt["blockNumber"] - 1),
        ])
        print(json.dumps(replay, indent=2, default=str))
        message = replay.get("error", {}).get("message")

        rule(f"{label} — WETH STATE READ AT THE FAILING BLOCK", "-")
        block = receipt["blockNumber"]
        allowance = weth.functions.allowance(acct.address, ROUTER).call(
            block_identifier=block)
        balance = weth.functions.balanceOf(acct.address).call(
            block_identifier=block)
        print(f"  block          : {block}")
        print(f"  allowance      : {allowance}")
        print(f"  balance        : {balance}")
        print(f"  amountIn       : {AMOUNT_IN}")
        print(f"  allowance >= amountIn : {allowance >= AMOUNT_IN}")
        print(f"  balance   >= amountIn : {balance >= AMOUNT_IN}")
        RESULTS.append({
            "case": label,
            "message": message,
            "block": block,
            "allowance": allowance,
            "balance": balance,
        })
    return receipt


# --------------------------------------------------------------------------
rule("0. CONNECTION")
print(f"  connected      : {w3.is_connected()}")
print(f"  client_version : {w3.client_version}")
print(f"  chain_id       : {w3.eth.chain_id}")
print(f"  block_number   : {w3.eth.block_number}")
print(f"  account        : {acct.address}")
print(f"  balance (ETH)  : {w3.from_wei(w3.eth.get_balance(acct.address), 'ether')}")
take_snapshot("entry state")
ENTRY_BLOCK = w3.eth.block_number

try:
    # ---------------------------------------------------------------- A
    rule("A. SLIPPAGE — impossible amountOutMinimum, allowance sufficient")
    wrap_and_set_allowance(AMOUNT_IN)
    params = (WETH, USDC, FEE, acct.address, AMOUNT_IN, IMPOSSIBLE_OUT_MIN, 0)
    print(f"  params         : {params}")
    probe(router.functions.exactInputSingle(params), "A. SLIPPAGE")

    # ---------------------------------------------------------------- B
    revert_snapshot("before B")
    rule("B. ALLOWANCE — allowance 0, amountOutMinimum trivially satisfiable")
    wrap_and_set_allowance(0)
    params = (WETH, USDC, FEE, acct.address, AMOUNT_IN, TRIVIAL_OUT_MIN, 0)
    print(f"  params         : {params}")
    probe(router.functions.exactInputSingle(params), "B. ALLOWANCE")

    # ---------------------------------------------------------------- C
    revert_snapshot("before C")
    rule("C. DEADLINE — multicall(deadline, data), deadline one hour in the past")
    wrap_and_set_allowance(AMOUNT_IN)
    params = (WETH, USDC, FEE, acct.address, AMOUNT_IN, TRIVIAL_OUT_MIN, 0)
    inner = calldata_of(router.functions.exactInputSingle(params))
    now = w3.eth.get_block("latest")["timestamp"]
    past_deadline = now - 3600
    print(f"  params         : {params}")
    print(f"  block ts       : {now}")
    print(f"  deadline       : {past_deadline}  (now - 3600)")
    print(f"  inner calldata : {inner}")
    probe(
        router.functions.multicall(past_deadline, [Web3.to_bytes(hexstr=inner)]),
        "C. DEADLINE",
    )

    # ---------------------------------------------------------------- D
    revert_snapshot("before D")
    rule("D. BALANCE — allowance sufficient, WETH balance below amountIn")
    # The entry state already holds WETH from an earlier run, so send it
    # away first: the balance at the failing block is then exactly
    # SMALL_WRAP, not SMALL_WRAP plus whatever happened to be there.
    leftover = weth.functions.balanceOf(acct.address).call()
    print(f"  leftover WETH  : {leftover}")
    if leftover:
        send(weth.functions.transfer(BURN, leftover).build_transaction(
            base_tx(gas=100_000)), "clear")
    send(weth.functions.deposit().build_transaction(
        base_tx(value=SMALL_WRAP, gas=100_000)), "wrap small")
    send(weth.functions.approve(ROUTER, AMOUNT_IN).build_transaction(
        base_tx(gas=100_000)), "approve")
    print(f"  WETH balance   : {weth.functions.balanceOf(acct.address).call()}")
    print(f"  allowance      : "
          f"{weth.functions.allowance(acct.address, ROUTER).call()}")
    print(f"  amountIn       : {AMOUNT_IN}")
    params = (WETH, USDC, FEE, acct.address, AMOUNT_IN, TRIVIAL_OUT_MIN, 0)
    print(f"  params         : {params}")
    probe(router.functions.exactInputSingle(params), "D. BALANCE")

    # ---------------------------------------------------------------- B vs D
    rule("B vs D — SAME REVERT STRING, DIFFERENT STATE")
    width = max([len(str(r["message"])) for r in RESULTS] + [len("message")])
    header = (f"  {'case':<12} {'message':<{width}} {'block':<10} "
              f"{'allowance':<18} {'balance':<18}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in RESULTS:
        print(f"  {row['case']:<12} {str(row['message']):<{width}} "
              f"{row['block']:<10} {row['allowance']:<18} {row['balance']:<18}")
    print(f"\n  amountIn for every row above: {AMOUNT_IN}")
    print("  B and D are the same string with opposite state. Nothing in the")
    print("  revert distinguishes them; only allowance and balance do.")
finally:
    rule("Z. RESTORE")
    ok = rpc("evm_revert", [SNAPSHOT])
    print(f"  evm_revert     : {ok}")
    print(f"  block_number   : {w3.eth.block_number} (entry was {ENTRY_BLOCK})")
    print(f"  WETH balance   : {weth.functions.balanceOf(acct.address).call()}")
    print(f"  allowance      : "
          f"{weth.functions.allowance(acct.address, ROUTER).call()}")
