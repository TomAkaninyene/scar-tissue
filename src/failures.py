"""Revert classification: a reverted receipt in, a failure class out.

Every rule here was established against the pinned anvil fork — the probe
evidence is in the "Revert handling" section of CLAUDE.md. The short
version:

  - a reverted receipt carries NO reason. status 0, logs [], nothing else.
    The reason exists only via a RE-CALL at the failing block, so this
    module is inherently two-step: the receipt says THAT it failed, the
    re-call says WHY.
  - the reason arrives on ContractLogicError as .message and .data. str()
    of that exception stringifies a 2-tuple and glues the hex onto the
    message, so it is never used here.
  - all three observed reverts are Error(string) under selector
    0x08c379a0. The selector therefore carries NO information and the
    string is the only thing the chain offers.
  - and the string is not enough. "STF" is emitted for BOTH an
    insufficient allowance and an insufficient balance — verified, same
    32-byte payload, mirrored state — so those two are split by reading
    allowance and balance at the failing block.

Scope is two classes: slippageRevert, and the STF pair allowanceRevert /
balanceRevert. Everything else returns None.

None means NOT CLASSIFIED, and a caller must never file a lesson for it.
A guessed class is a wrong lesson, and a wrong lesson is worse than none:
the next run retrieves it and acts on it.
"""
from __future__ import annotations

from typing import Any

from web3 import Web3
from web3.exceptions import ContractLogicError

from memory import normalize_address

# --------------------------------------------------------------------------
# Failure classes. camelCase, no hyphen — they are sig_v1 segments and must
# survive unicode61 as single tokens (CLAUDE.md, sig_v1).
# --------------------------------------------------------------------------
SLIPPAGE_REVERT = "slippageRevert"
ALLOWANCE_REVERT = "allowanceRevert"
BALANCE_REVERT = "balanceRevert"

# --------------------------------------------------------------------------
# Observed revert strings. Nothing here is speculative: each one was
# induced on the fork and read back off the wire.
# --------------------------------------------------------------------------
REASON_SLIPPAGE = "Too little received"
REASON_TRANSFER_FAILED = "STF"
REASON_DEADLINE = "Transaction too old"

# Reverts that are real, reproduced, and deliberately NOT classified.
# "Transaction too old" can only come from the multicall(uint256,bytes[])
# wrapper — exactInputSingle has no deadline field in SwapRouter02 — and
# the wrapper rejects before the swap ever executes. Out of scope.
OUT_OF_SCOPE_REASONS = frozenset({REASON_DEADLINE})

# Solidity Error(string). Every revert observed on this router uses it.
ERROR_STRING_SELECTOR = "08c379a0"
_ABI_WORD = 64  # hex characters in one 32-byte word

_REVERTED_PREFIX = "execution reverted"

ERC20_ABI = [
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def _wire(address: str) -> str:
    """Normalize an address, then checksum it for the wire.

    normalize_address is THE boundary: everything entering this module
    becomes lowercase first, because lowercase is what memory keys on and
    what RPC returns natively.

    The checksum is applied only on the way back out to web3, and only
    because web3 requires it: an ABI-encoded address argument that is not
    checksummed raises InvalidAddress ("web3.py only accepts checksum
    addresses"). Verified against web3 8.0.0 — eth_call's `to` field is
    accepted lowercase, a function ARGUMENT is not. Nothing checksummed
    ever leaves this module.
    """
    return Web3.to_checksum_address(normalize_address(address))


def decode_error_string(data: Any) -> str | None:
    """Decode a Solidity Error(string) payload into its message.

    Returns None for anything that is not a well-formed Error(string) —
    a custom error, a panic, empty revert data, or a dict-shaped `data`
    from some other node. Decoding is preferred over reading .message
    because these are the chain's own bytes: the node's message text is
    formatting, the payload is evidence.
    """
    if not isinstance(data, str):
        # CLAUDE.md: .data is a str on this stack. A dict means a node
        # that does not behave like the one the rules were verified on.
        return None
    payload = data[2:] if data.startswith("0x") else data
    if not payload.startswith(ERROR_STRING_SELECTOR):
        return None
    body = payload[len(ERROR_STRING_SELECTOR):]
    if len(body) < 2 * _ABI_WORD:
        return None
    try:
        offset = int(body[:_ABI_WORD], 16)
        length = int(body[_ABI_WORD:2 * _ABI_WORD], 16)
    except ValueError:
        return None
    if offset != 32:
        return None
    start = 2 * _ABI_WORD
    end = start + 2 * length
    if len(body) < end:
        return None
    try:
        return bytes.fromhex(body[start:end]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def revert_reason(exc: ContractLogicError) -> str | None:
    """Pull the revert string off a ContractLogicError.

    .data first (the chain's bytes), .message second (the node's text).
    NEVER str(exc): args is a 2-tuple (message, data) and str() renders
    the whole tuple, so the hex ends up glued to the message.
    """
    reason = decode_error_string(getattr(exc, "data", None))
    if reason is not None:
        return reason

    message = getattr(exc, "message", None)
    if not isinstance(message, str):
        return None
    if message.startswith(_REVERTED_PREFIX):
        # The colon is not guaranteed: a node with no reason string to
        # report says just "execution reverted", and that is an absence of
        # a reason, not a reason named "execution reverted".
        message = message[len(_REVERTED_PREFIX):].lstrip(":")
    reason = message.strip()
    return reason or None


def _reason_at_block(w3, tx_params: dict[str, Any], block: int) -> str | None:
    """Re-call the failed transaction at the failing block to learn why.

    This is the second step the receipt forces. A reverted transaction
    writes no state, so replaying at the block that contains it sees the
    state it saw.

    Only ContractLogicError is caught. An RPC failure, a bad block, a
    dead node — those propagate, because "could not reach the chain" must
    never be silently recorded as "no failure class".
    """
    call = {
        "from": _wire(tx_params["from"]),
        "to": _wire(tx_params["to"]),
        "data": tx_params["data"],
    }
    value = tx_params.get("value")
    if value:
        call["value"] = value

    try:
        w3.eth.call(call, block_identifier=block)
    except ContractLogicError as exc:
        return revert_reason(exc)
    # It did not revert on replay. Nothing to classify, and nothing to
    # guess: this is a real state we have no evidence about.
    return None


def _split_transfer_failure(w3, tx_params: dict[str, Any], block: int) -> str | None:
    """Split "STF" into allowanceRevert / balanceRevert by reading state.

    THE reason this module reads the chain at all. TransferHelper's
    safeTransferFrom reports only that the transferFrom failed, never why,
    so an insufficient allowance and an insufficient balance are
    indistinguishable from the revert alone — verified: byte-identical
    payloads, mirrored state.

    The owner is the transaction's sender and the spender is the contract
    it called, because the router pulls the input token with transferFrom.
    """
    token = _wire(tx_params["token"])
    owner = _wire(tx_params["from"])
    spender = _wire(tx_params["to"])
    amount_in = int(tx_params["amountIn"])

    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
    allowance = erc20.functions.allowance(owner, spender).call(
        block_identifier=block)
    balance = erc20.functions.balanceOf(owner).call(block_identifier=block)

    if allowance < amount_in:
        return ALLOWANCE_REVERT
    if balance < amount_in:
        return BALANCE_REVERT
    # Allowance and balance both cover the swap, so neither observed cause
    # explains it. A third cause exists that we have not induced and have
    # no evidence for. Return None rather than file a lesson under a
    # signature the agent would later act on.
    return None


def classify(w3, receipt, tx_params: dict[str, Any]) -> str | None:
    """Classify a reverted transaction. Returns a failure class or None.

    ``receipt``    a status-0 receipt. Only status and blockNumber are read;
                   gasUsed is NEVER keyed on — it varies with the revert
                   site and is not evidence of a cause.
    ``tx_params``  the submitted call, as {"from", "to", "data"}, plus
                   {"token", "amountIn"} which the STF split needs. "value"
                   is optional.

    None means NOT CLASSIFIED. Do not file a lesson for it.
    """
    if receipt["status"] != 0:
        raise ValueError(
            "classify() takes a REVERTED receipt; got status "
            f"{receipt['status']}. A successful transaction has no failure "
            "class and asking for one is a caller bug."
        )

    block = receipt["blockNumber"]
    reason = _reason_at_block(w3, tx_params, block)
    if reason is None:
        return None
    if reason == REASON_SLIPPAGE:
        return SLIPPAGE_REVERT
    if reason == REASON_TRANSFER_FAILED:
        return _split_transfer_failure(w3, tx_params, block)
    if reason in OUT_OF_SCOPE_REASONS:
        return None
    # An unobserved revert string. Out of scope by definition — a class we
    # have never induced is a class we cannot correct.
    return None
