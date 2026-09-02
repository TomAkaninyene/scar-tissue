"""Decision-layer tests: what a standing lesson does to the parameters.

The chain is not involved. These pin what the agent DECIDES before it
submits — which is where memory either changes behaviour or does not, and
therefore what the deletion test is actually testing.

    .venv/bin/python -m pytest tests/ -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent import (  # noqa: E402
    BASE_SWAP_ROUTER_02,
    BASE_USDC,
    BASE_WETH,
    MAX_SLIPPAGE_REDUCTION_BPS,
    SLIPPAGE_STEP_BPS,
    Agent,
    SwapIntent,
)
from failures import (  # noqa: E402
    ALLOWANCE_REVERT,
    BALANCE_REVERT,
    SLIPPAGE_REVERT,
)
from memory import build_signature  # noqa: E402

AMOUNT_IN = 10**16
MIN_OUT = 40_000_000  # 40 USDC, six decimals


def intent(min_out=MIN_OUT):
    return SwapIntent(
        router=BASE_SWAP_ROUTER_02, token_in=BASE_WETH, token_out=BASE_USDC,
        fee=500, amount_in=AMOUNT_IN, amount_out_minimum=min_out,
        recipient="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    )


def lesson(correction="widen it", evidence_count=1):
    return {
        "id": "row-id",
        "created_at": "2026-09-02T00:00:00.000Z",
        "updated_at": "2026-09-02T00:00:01.000Z",
        "body": {"sig": "v1", "correction": correction,
                 "evidence_count": evidence_count, "evidence": {}},
    }


class FakeStore:
    """Records every retrieval so a test can assert none happened."""

    def __init__(self, lessons=None):
        self._lessons = lessons or {}
        self.get_calls = []
        self.neighbour_calls = []
        self.open_calls = []

    def get_lesson(self, sig):
        self.get_calls.append(sig)
        return self._lessons.get(sig)

    def find_neighbours(self, token, **kwargs):
        self.neighbour_calls.append(token)
        return [{"name": "some/other/lesson"}]

    def write_open_call(self, tx_ref, claim):
        self.open_calls.append((tx_ref, claim))
        return claim


def signature(failure_class):
    return build_signature(
        BASE_SWAP_ROUTER_02, "exactInputSingle", failure_class)


def decide(store, *, use_memory=True, min_out=MIN_OUT):
    agent = Agent(w3=None, account=None, store=store, use_memory=use_memory)
    signatures = {
        failure_class: signature(failure_class)
        for failure_class in (SLIPPAGE_REVERT, ALLOWANCE_REVERT, BALANCE_REVERT)
    }
    standing, _neighbours = agent._retrieve(intent(min_out), signatures)
    return agent._decide(intent(min_out), standing)


class TestNoStandingLesson(unittest.TestCase):

    def test_parameters_are_untouched(self):
        amount_out_minimum, applied = decide(FakeStore())
        self.assertEqual(amount_out_minimum, MIN_OUT)
        self.assertFalse(applied["applied"])
        self.assertEqual(applied["changes"], [])
        self.assertIn("no standing lesson", applied["reason"])


class TestSlippageLesson(unittest.TestCase):

    def test_min_out_is_reduced_by_one_step_per_prior_failure(self):
        store = FakeStore({signature(SLIPPAGE_REVERT): lesson(evidence_count=2)})
        amount_out_minimum, applied = decide(store)

        expected = MIN_OUT * (10_000 - 2 * SLIPPAGE_STEP_BPS) // 10_000
        self.assertEqual(amount_out_minimum, expected)
        self.assertTrue(applied["applied"])
        change = applied["changes"][0]
        self.assertEqual(change["field"], "amountOutMinimum")
        self.assertEqual(change["from"], MIN_OUT)
        self.assertEqual(change["to"], expected)
        self.assertEqual(change["reductionBps"], 2 * SLIPPAGE_STEP_BPS)

    def test_reduction_is_capped(self):
        store = FakeStore(
            {signature(SLIPPAGE_REVERT): lesson(evidence_count=99)})
        _amount_out_minimum, applied = decide(store)
        self.assertEqual(applied["changes"][0]["reductionBps"],
                         MAX_SLIPPAGE_REDUCTION_BPS)

    def test_a_correction_never_zeroes_the_protection(self):
        """min-out 0 accepts any fill. No lesson may reach it."""
        store = FakeStore(
            {signature(SLIPPAGE_REVERT): lesson(evidence_count=3)})
        amount_out_minimum, applied = decide(store, min_out=1)

        self.assertEqual(amount_out_minimum, 1)
        self.assertTrue(applied["changes"][0]["floored"])

    def test_zero_intent_stays_zero_and_is_not_floored(self):
        store = FakeStore({signature(SLIPPAGE_REVERT): lesson()})
        amount_out_minimum, applied = decide(store, min_out=0)
        self.assertEqual(amount_out_minimum, 0)
        self.assertFalse(applied["changes"][0]["floored"])


class TestAllowanceLesson(unittest.TestCase):

    def test_approve_is_scheduled_and_min_out_untouched(self):
        store = FakeStore({signature(ALLOWANCE_REVERT): lesson()})
        amount_out_minimum, applied = decide(store)

        self.assertEqual(amount_out_minimum, MIN_OUT)
        self.assertTrue(applied["approve_first"])
        self.assertEqual(applied["changes"][0]["field"], "allowance")


class TestBalanceLesson(unittest.TestCase):

    def test_reported_but_never_applied(self):
        store = FakeStore({signature(BALANCE_REVERT): lesson()})
        amount_out_minimum, applied = decide(store)

        self.assertEqual(amount_out_minimum, MIN_OUT)
        self.assertFalse(applied["applied"])
        self.assertFalse(applied["approve_first"])
        self.assertEqual(len(applied["changes"]), 1)
        self.assertIn("no pre-flight remedy", applied["changes"][0]["action"])


class TestBothLessonsStanding(unittest.TestCase):

    def test_slippage_and_allowance_apply_together(self):
        store = FakeStore({
            signature(SLIPPAGE_REVERT): lesson(evidence_count=1),
            signature(ALLOWANCE_REVERT): lesson(),
        })
        amount_out_minimum, applied = decide(store)

        self.assertLess(amount_out_minimum, MIN_OUT)
        self.assertTrue(applied["approve_first"])
        self.assertEqual(len(applied["changes"]), 2)


class TestNoMemory(unittest.TestCase):

    def test_nothing_is_retrieved_and_nothing_is_applied(self):
        store = FakeStore({
            signature(SLIPPAGE_REVERT): lesson(evidence_count=5),
            signature(ALLOWANCE_REVERT): lesson(),
        })
        amount_out_minimum, applied = decide(store, use_memory=False)

        self.assertEqual(store.get_calls, [])
        self.assertEqual(store.neighbour_calls, [])
        self.assertEqual(amount_out_minimum, MIN_OUT)
        self.assertFalse(applied["applied"])
        self.assertFalse(applied["approve_first"])
        self.assertIn("--no-memory", applied["reason"])

    def test_memory_on_reads_every_in_scope_signature_and_one_neighbour_token(self):
        store = FakeStore()
        decide(store)

        self.assertEqual(sorted(store.get_calls), sorted([
            signature(SLIPPAGE_REVERT),
            signature(ALLOWANCE_REVERT),
            signature(BALANCE_REVERT),
        ]))
        # ONE token, and the lowercased router address, never prose.
        self.assertEqual(store.neighbour_calls,
                         [BASE_SWAP_ROUTER_02.lower()])


class TestCorrectionText(unittest.TestCase):

    def test_correction_states_what_the_next_run_will_do(self):
        store = FakeStore()
        agent = Agent(w3=None, account=None, store=store)
        standing = {SLIPPAGE_REVERT: lesson(evidence_count=1)}

        correction = agent._correction_for(SLIPPAGE_REVERT, intent(), standing)
        # evidence_count 1 now, so the row about to be written carries 2.
        self.assertIn(f"{2 * SLIPPAGE_STEP_BPS} bps", correction)

    def test_unknown_class_has_no_correction(self):
        agent = Agent(w3=None, account=None, store=FakeStore())
        with self.assertRaises(ValueError):
            agent._correction_for("deadlineRevert", intent(), {})


if __name__ == "__main__":
    unittest.main()
