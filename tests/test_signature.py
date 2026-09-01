"""sig_v1 tests. stdlib unittest — requirements.txt is pinned and has no
test runner, so these must run with no extra install:

    .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memory import SIG_SEPARATOR, build_signature, normalize_address  # noqa: E402

# Real Uniswap v3 SwapRouter02 on Base, in its EIP-55 checksummed form.
CHECKSUMMED = "0x2626664c2603336E57B271c5C0b26F421741e481"
LOWERCASED = "0x2626664c2603336e57b271c5c0b26f421741e481"


class TestAddressLowercasing(unittest.TestCase):
    """A checksummed address must always land in the key as lowercase."""

    def test_checksummed_address_is_lowercased(self):
        self.assertEqual(normalize_address(CHECKSUMMED), LOWERCASED)

    def test_already_lowercase_address_is_unchanged(self):
        self.assertEqual(normalize_address(LOWERCASED), LOWERCASED)

    def test_uppercase_address_is_lowercased(self):
        upper = "0x" + CHECKSUMMED[2:].upper()
        self.assertEqual(normalize_address(upper), LOWERCASED)

    def test_signature_carries_the_lowercased_address(self):
        sig = build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert")
        self.assertTrue(sig.startswith(LOWERCASED + SIG_SEPARATOR))
        self.assertNotIn("E", sig.split(SIG_SEPARATOR)[0])

    def test_signature_matches_the_frozen_example_in_claude_md(self):
        self.assertEqual(
            build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert"),
            "0x2626664c2603336e57b271c5c0b26f421741e481"
            "/exactInputSingle/slippageRevert",
        )

    def test_case_variants_of_one_address_collapse_to_one_key(self):
        """The split-brain case: three spellings, one memory key."""
        keys = {
            build_signature(addr, "exactInputSingle", "slippageRevert")
            for addr in (CHECKSUMMED, LOWERCASED, "0x" + CHECKSUMMED[2:].upper())
        }
        self.assertEqual(len(keys), 1)


class TestCamelCaseRoundTrip(unittest.TestCase):
    """camelCase is atomic under unicode61 — it must survive byte-identical."""

    def test_failure_class_survives_round_trip(self):
        sig = build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert")
        self.assertEqual(sig.split(SIG_SEPARATOR)[2], "slippageRevert")

    def test_function_name_survives_round_trip(self):
        sig = build_signature(
            CHECKSUMMED, "swapExactTokensForTokens", "allowanceRevert"
        )
        self.assertEqual(sig.split(SIG_SEPARATOR)[1], "swapExactTokensForTokens")

    def test_all_three_segments_round_trip(self):
        for function_name, failure_class in (
            ("exactInputSingle", "slippageRevert"),
            ("swapExactTokensForTokens", "allowanceRevert"),
            ("removeLiquidity", "deadlineRevert"),
        ):
            with self.subTest(function_name=function_name):
                address, fn, fc = build_signature(
                    CHECKSUMMED, function_name, failure_class
                ).split(SIG_SEPARATOR)
                self.assertEqual(address, LOWERCASED)
                self.assertEqual(fn, function_name)
                self.assertEqual(fc, failure_class)

    def test_signature_has_exactly_three_segments(self):
        sig = build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert")
        self.assertEqual(len(sig.split(SIG_SEPARATOR)), 3)

    def test_hyphenated_failure_class_is_rejected(self):
        """Hyphens shatter into common words that collide with body prose."""
        with self.assertRaises(ValueError):
            build_signature(CHECKSUMMED, "exactInputSingle", "slippage-revert")


class TestDeterminism(unittest.TestCase):
    """Same inputs, same string. A key that drifts is a lost lesson."""

    def test_repeated_calls_are_identical(self):
        sigs = {
            build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert")
            for _ in range(100)
        }
        self.assertEqual(len(sigs), 1)

    def test_distinct_inputs_produce_distinct_signatures(self):
        sigs = {
            build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert"),
            build_signature(CHECKSUMMED, "exactInputSingle", "allowanceRevert"),
            build_signature(CHECKSUMMED, "removeLiquidity", "slippageRevert"),
            build_signature(LOWERCASED.replace("2626664c", "4752ba5d"),
                            "exactInputSingle", "slippageRevert"),
        }
        self.assertEqual(len(sigs), 4)

    def test_surrounding_whitespace_does_not_change_the_key(self):
        self.assertEqual(
            build_signature(f"  {CHECKSUMMED}  ", " exactInputSingle ",
                            " slippageRevert "),
            build_signature(CHECKSUMMED, "exactInputSingle", "slippageRevert"),
        )


if __name__ == "__main__":
    unittest.main()
