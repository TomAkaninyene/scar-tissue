"""Classifier tests against fixtures taken from the fork probe.

Every revert payload, allowance, balance and amountIn below is a value
that was actually observed on the pinned anvil fork — the four induced
cases and the state read back at each failing block:

    case         revert string             allowance          balance
    A. SLIPPAGE  Too little received       10000000000000000  20000000000000000
    B. ALLOWANCE STF                       0                  20000000000000000
    C. DEADLINE  Transaction too old       10000000000000000  20000000000000000
    D. BALANCE   STF                       10000000000000000   1000000000000000

    amountIn for every case: 10000000000000000

B and D are the same 32-byte payload with mirrored state. Splitting them
is the entire reason the classifier reads the chain, so it has a test of
its own below.

The chain is faked here on purpose: these tests pin the classifier's
DECISIONS, and they must pass on a clean checkout with no anvil running.
The fixtures are what make them real.

    .venv/bin/python -m pytest tests/ -v
"""
import sys
import unittest
from pathlib import Path

from web3 import Web3
from web3.exceptions import ContractLogicError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from failures import (  # noqa: E402
    ALLOWANCE_REVERT,
    BALANCE_REVERT,
    SLIPPAGE_REVERT,
    classify,
    decode_error_string,
    revert_reason,
)

# -- fixtures: exact payloads off the wire ---------------------------------
DATA_TOO_LITTLE = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000013"
    "546f6f206c6974746c6520726563656976656400000000000000000000000000"
)
DATA_STF = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000003"
    "5354460000000000000000000000000000000000000000000000000000000000"
)
DATA_TOO_OLD = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000013"
    "5472616e73616374696f6e20746f6f206f6c6400000000000000000000000000"
)

MSG_TOO_LITTLE = "execution reverted: Too little received"
MSG_STF = "execution reverted: STF"
MSG_TOO_OLD = "execution reverted: Transaction too old"

# -- fixtures: exact state at each failing block ---------------------------
AMOUNT_IN = 10000000000000000
ALLOWANCE_SUFFICIENT = 10000000000000000
ALLOWANCE_NONE = 0
BALANCE_SUFFICIENT = 20000000000000000
BALANCE_SHORT = 1000000000000000

BLOCK_ABC = 50786406
BLOCK_D = 50786407

# Addresses exactly as a receipt returns them: checksummed.
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
WETH = "0x4200000000000000000000000000000000000006"
CALLDATA = "0x04e45aaf" + "00" * 32


def receipt(block=BLOCK_ABC, status=0):
    return {"status": status, "blockNumber": block}


def tx_params(sender=OWNER, to=ROUTER, token=WETH, amount_in=AMOUNT_IN):
    return {
        "from": sender,
        "to": to,
        "data": CALLDATA,
        "token": token,
        "amountIn": amount_in,
    }


# -- the fake chain --------------------------------------------------------
class FakeRead:
    def __init__(self, name, value, recorder):
        self._name, self._value, self._recorder = name, value, recorder

    def call(self, block_identifier=None):
        self._recorder.append((self._name, block_identifier))
        return self._value


class FakeFunctions:
    def __init__(self, allowance, balance, reads, args):
        self._allowance, self._balance = allowance, balance
        self._reads, self._args = reads, args

    def allowance(self, owner, spender):
        self._args.append(("allowance", owner, spender))
        return FakeRead("allowance", self._allowance, self._reads)

    def balanceOf(self, account):
        self._args.append(("balanceOf", account))
        return FakeRead("balanceOf", self._balance, self._reads)


class FakeContract:
    def __init__(self, allowance, balance, reads, args):
        self.functions = FakeFunctions(allowance, balance, reads, args)


class FakeEth:
    def __init__(self, revert=None, allowance=0, balance=0):
        self._revert = revert
        self._allowance, self._balance = allowance, balance
        self.calls = []
        self.reads = []
        self.read_args = []
        self.contracts = []

    def call(self, transaction, block_identifier=None):
        self.calls.append((dict(transaction), block_identifier))
        if self._revert is not None:
            raise self._revert
        return b""

    def contract(self, address=None, abi=None):
        self.contracts.append(address)
        return FakeContract(
            self._allowance, self._balance, self.reads, self.read_args)


class FakeW3:
    def __init__(self, **kwargs):
        self.eth = FakeEth(**kwargs)


def reverting_with(message, data, allowance=0, balance=0):
    return FakeW3(
        revert=ContractLogicError(message, data=data),
        allowance=allowance,
        balance=balance,
    )


# -- the four observed cases ----------------------------------------------
class TestObservedCases(unittest.TestCase):

    def test_case_a_slippage(self):
        w3 = reverting_with(MSG_TOO_LITTLE, DATA_TOO_LITTLE,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), SLIPPAGE_REVERT)

    def test_case_b_allowance(self):
        w3 = reverting_with(MSG_STF, DATA_STF,
                            ALLOWANCE_NONE, BALANCE_SUFFICIENT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), ALLOWANCE_REVERT)

    def test_case_c_deadline_is_out_of_scope(self):
        w3 = reverting_with(MSG_TOO_OLD, DATA_TOO_OLD,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))

    def test_case_d_balance(self):
        w3 = reverting_with(MSG_STF, DATA_STF,
                            ALLOWANCE_SUFFICIENT, BALANCE_SHORT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_D), tx_params()), BALANCE_REVERT)


class TestTheSplit(unittest.TestCase):
    """The whole point: one payload, two classes, decided by state."""

    def test_identical_stf_payload_mirrored_state_classifies_differently(self):
        allowance_case = reverting_with(
            MSG_STF, DATA_STF, ALLOWANCE_NONE, BALANCE_SUFFICIENT)
        balance_case = reverting_with(
            MSG_STF, DATA_STF, ALLOWANCE_SUFFICIENT, BALANCE_SHORT)

        first = classify(allowance_case, receipt(BLOCK_ABC), tx_params())
        second = classify(balance_case, receipt(BLOCK_D), tx_params())

        # Same bytes on the wire in both runs.
        self.assertEqual(
            allowance_case.eth.calls[0][0]["data"],
            balance_case.eth.calls[0][0]["data"],
        )
        self.assertEqual(first, ALLOWANCE_REVERT)
        self.assertEqual(second, BALANCE_REVERT)
        self.assertNotEqual(first, second)

    def test_allowance_wins_when_both_are_short(self):
        w3 = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_NONE, BALANCE_SHORT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_D), tx_params()), ALLOWANCE_REVERT)

    def test_stf_with_sufficient_allowance_and_balance_is_unclassified(self):
        """A cause we have never induced. None, never a guess."""
        w3 = reverting_with(MSG_STF, DATA_STF,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))

    def test_exactly_amount_in_is_sufficient(self):
        """allowance == amountIn covers the swap; the check is <, not <=."""
        w3 = reverting_with(MSG_STF, DATA_STF, AMOUNT_IN, AMOUNT_IN)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))

    def test_one_wei_short_is_short(self):
        w3 = reverting_with(MSG_STF, DATA_STF, AMOUNT_IN, AMOUNT_IN - 1)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), BALANCE_REVERT)


class TestReadsAtTheFailingBlock(unittest.TestCase):

    def test_recall_and_both_reads_use_the_failing_block(self):
        w3 = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_SUFFICIENT,
                            BALANCE_SHORT)
        classify(w3, receipt(BLOCK_D), tx_params())

        self.assertEqual(w3.eth.calls[0][1], BLOCK_D)
        self.assertEqual(
            w3.eth.reads, [("allowance", BLOCK_D), ("balanceOf", BLOCK_D)])

    def test_no_state_read_when_the_reason_is_not_stf(self):
        w3 = reverting_with(MSG_TOO_LITTLE, DATA_TOO_LITTLE,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        classify(w3, receipt(BLOCK_ABC), tx_params())
        self.assertEqual(w3.eth.reads, [])


class TestExceptionHandling(unittest.TestCase):

    def test_str_of_the_exception_is_never_used(self):
        """str() renders the (message, data) tuple. Touching it fails here."""
        class ExplodingStr(ContractLogicError):
            def __str__(self):
                raise AssertionError("classify() called str() on the exception")

        w3 = FakeW3(revert=ExplodingStr(MSG_STF, data=DATA_STF),
                    allowance=ALLOWANCE_NONE, balance=BALANCE_SUFFICIENT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), ALLOWANCE_REVERT)

    def test_data_is_preferred_over_message(self):
        """A node that gives a bare message still classifies, from the bytes."""
        w3 = reverting_with("execution reverted", DATA_TOO_LITTLE,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), SLIPPAGE_REVERT)

    def test_message_is_the_fallback_when_data_is_missing(self):
        w3 = reverting_with(MSG_TOO_LITTLE, None,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertEqual(
            classify(w3, receipt(BLOCK_ABC), tx_params()), SLIPPAGE_REVERT)

    def test_empty_revert_data_is_unclassified(self):
        w3 = reverting_with("execution reverted", "0x",
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))

    def test_unobserved_revert_string_is_unclassified(self):
        w3 = reverting_with("execution reverted: SPL", None,
                            ALLOWANCE_SUFFICIENT, BALANCE_SUFFICIENT)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))

    def test_rpc_errors_are_not_swallowed(self):
        class Boom(Exception):
            pass

        w3 = FakeW3(revert=Boom("node is down"))
        with self.assertRaises(Boom):
            classify(w3, receipt(BLOCK_ABC), tx_params())

    def test_no_revert_on_replay_is_unclassified(self):
        w3 = FakeW3(revert=None)
        self.assertIsNone(classify(w3, receipt(BLOCK_ABC), tx_params()))


class TestReceiptContract(unittest.TestCase):

    def test_successful_receipt_is_rejected(self):
        w3 = reverting_with(MSG_STF, DATA_STF)
        with self.assertRaises(ValueError):
            classify(w3, receipt(BLOCK_ABC, status=1), tx_params())

    def test_gas_used_is_never_consulted(self):
        """The two STF cases burned different gas. It must not matter."""
        w3 = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_NONE,
                            BALANCE_SUFFICIENT)
        minimal = {"status": 0, "blockNumber": BLOCK_ABC}
        self.assertEqual(
            classify(w3, minimal, tx_params()), ALLOWANCE_REVERT)


class TestAddressNormalization(unittest.TestCase):

    def test_checksummed_and_lowercase_inputs_agree(self):
        checksummed = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_NONE,
                                     BALANCE_SUFFICIENT)
        lowercased = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_NONE,
                                    BALANCE_SUFFICIENT)

        from_receipt = classify(checksummed, receipt(BLOCK_ABC), tx_params())
        from_rpc = classify(lowercased, receipt(BLOCK_ABC), tx_params(
            sender=OWNER.lower(), to=ROUTER.lower(), token=WETH.lower()))

        self.assertEqual(from_receipt, from_rpc)
        self.assertEqual(checksummed.eth.calls, lowercased.eth.calls)
        self.assertEqual(checksummed.eth.read_args, lowercased.eth.read_args)

    def test_addresses_reach_web3_checksummed(self):
        """web3 rejects a non-checksummed ABI address argument."""
        w3 = reverting_with(MSG_STF, DATA_STF, ALLOWANCE_NONE,
                            BALANCE_SUFFICIENT)
        classify(w3, receipt(BLOCK_ABC), tx_params(
            sender=OWNER.lower(), to=ROUTER.lower(), token=WETH.lower()))

        self.assertEqual(w3.eth.contracts, [Web3.to_checksum_address(WETH)])
        self.assertEqual(w3.eth.read_args, [
            ("allowance", Web3.to_checksum_address(OWNER),
             Web3.to_checksum_address(ROUTER)),
            ("balanceOf", Web3.to_checksum_address(OWNER)),
        ])
        call = w3.eth.calls[0][0]
        self.assertEqual(call["from"], Web3.to_checksum_address(OWNER))
        self.assertEqual(call["to"], Web3.to_checksum_address(ROUTER))

    def test_malformed_address_is_rejected(self):
        w3 = reverting_with(MSG_STF, DATA_STF)
        with self.assertRaises(ValueError):
            classify(w3, receipt(BLOCK_ABC), tx_params(sender="0xnope"))


class TestDecoder(unittest.TestCase):

    def test_decodes_each_observed_payload(self):
        self.assertEqual(decode_error_string(DATA_TOO_LITTLE),
                         "Too little received")
        self.assertEqual(decode_error_string(DATA_STF), "STF")
        self.assertEqual(decode_error_string(DATA_TOO_OLD),
                         "Transaction too old")

    def test_rejects_non_error_string_payloads(self):
        # Panic(uint256) selector, and a truncated Error(string).
        self.assertIsNone(decode_error_string("0x4e487b71" + "00" * 32))
        self.assertIsNone(decode_error_string("0x08c379a0" + "00" * 8))
        self.assertIsNone(decode_error_string("0x"))
        self.assertIsNone(decode_error_string(None))
        self.assertIsNone(decode_error_string({"message": "nope"}))

    def test_reason_prefers_data_and_falls_back_to_message(self):
        self.assertEqual(
            revert_reason(ContractLogicError(MSG_STF, data=DATA_TOO_LITTLE)),
            "Too little received")
        self.assertEqual(
            revert_reason(ContractLogicError(MSG_STF, data=None)), "STF")
        self.assertIsNone(
            revert_reason(ContractLogicError("execution reverted", data=None)))


if __name__ == "__main__":
    unittest.main()
