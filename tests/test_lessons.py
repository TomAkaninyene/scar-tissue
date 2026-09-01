"""Lesson-layer tests against a throwaway temp DB.

Never touches ./data/reflex.db. Run with:

    .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lessons import LESSON_CATEGORY, LessonStore, open_client  # noqa: E402
from memory import build_signature  # noqa: E402

ROUTER_V3 = "0x2626664c2603336E57B271c5C0b26F421741e481"
ROUTER_V2 = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"

SIG_V3_SLIPPAGE = build_signature(ROUTER_V3, "exactInputSingle", "slippageRevert")
SIG_V2_SLIPPAGE = build_signature(ROUTER_V2, "swapExactTokensForTokens", "slippageRevert")
SIG_V3_ALLOWANCE = build_signature(ROUTER_V3, "exactInputSingle", "allowanceRevert")


class LessonStoreTestCase(unittest.TestCase):
    """One temp DB per test. No shared state, no ./data/reflex.db."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "test_reflex.db"
        self.store = LessonStore(open_client(db_path))


class TestWriteAndReadBack(LessonStoreTestCase):

    def test_first_write_then_read_back(self):
        written = self.store.write_lesson(
            SIG_V3_SLIPPAGE,
            correction="raise slippage tolerance to 150 bps",
            evidence="tx 0xabc reverted with slippage at 50 bps",
        )
        self.assertEqual(written["category"], LESSON_CATEGORY)
        self.assertEqual(written["name"], SIG_V3_SLIPPAGE)

        read_back = self.store.get_lesson(SIG_V3_SLIPPAGE)
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back["id"], written["id"])
        self.assertEqual(read_back["name"], SIG_V3_SLIPPAGE)
        self.assertEqual(
            read_back["body"]["correction"], "raise slippage tolerance to 150 bps"
        )
        self.assertEqual(read_back["body"]["evidence_count"], 1)
        self.assertEqual(read_back["body"]["sig"], "v1")

    def test_status_column_is_never_used(self):
        self.store.write_lesson(SIG_V3_SLIPPAGE, correction="c", evidence="e")
        self.assertIsNone(self.store.get_lesson(SIG_V3_SLIPPAGE)["status"])


class TestFirstEncounter(LessonStoreTestCase):

    def test_get_lesson_returns_none_for_unseen_signature(self):
        self.assertIsNone(self.store.get_lesson(SIG_V3_SLIPPAGE))

    def test_get_lesson_does_not_raise_on_empty_store(self):
        try:
            result = self.store.get_lesson(SIG_V2_SLIPPAGE)
        except Exception as exc:  # noqa: BLE001 - the point is that none escapes
            self.fail(f"first encounter must not raise, got {exc!r}")
        self.assertIsNone(result)

    def test_unseen_signature_is_none_while_another_lesson_exists(self):
        self.store.write_lesson(SIG_V3_SLIPPAGE, correction="c", evidence="e")
        self.assertIsNone(self.store.get_lesson(SIG_V2_SLIPPAGE))


class TestRevisionInPlace(LessonStoreTestCase):

    def test_second_write_revises_the_same_row(self):
        first = self.store.write_lesson(
            SIG_V3_SLIPPAGE, correction="raise slippage to 100 bps", evidence="tx 0xaaa"
        )
        before = self.store.get_lesson(SIG_V3_SLIPPAGE)

        # updated_at has millisecond resolution; two writes inside the same
        # millisecond would tie and make the advance unobservable.
        time.sleep(0.005)

        second = self.store.write_lesson(
            SIG_V3_SLIPPAGE, correction="raise slippage to 150 bps", evidence="tx 0xbbb"
        )
        after = self.store.get_lesson(SIG_V3_SLIPPAGE)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(before["created_at"], after["created_at"])
        self.assertGreater(after["updated_at"], before["updated_at"])

        self.assertEqual(before["body"]["evidence_count"], 1)
        self.assertEqual(after["body"]["evidence_count"], 2)
        self.assertEqual(after["body"]["correction"], "raise slippage to 150 bps")
        self.assertEqual(after["body"]["evidence"], "tx 0xbbb")
        self.assertEqual(after["body"]["sig"], "v1")

    def test_revision_does_not_create_a_second_row(self):
        self.store.write_lesson(SIG_V3_SLIPPAGE, correction="c1", evidence="e1")
        self.store.write_lesson(SIG_V3_SLIPPAGE, correction="c2", evidence="e2")
        rows = self.store.client.list_entities(LESSON_CATEGORY, limit=100)
        self.assertEqual(len(rows), 1)

    def test_evidence_count_increments_across_many_revisions(self):
        for _ in range(5):
            self.store.write_lesson(SIG_V3_SLIPPAGE, correction="c", evidence="e")
        self.assertEqual(
            self.store.get_lesson(SIG_V3_SLIPPAGE)["body"]["evidence_count"], 5
        )


class TestNeighbourSearch(LessonStoreTestCase):

    def setUp(self):
        super().setUp()
        # Same failure class, two DIFFERENT contracts.
        self.store.write_lesson(
            SIG_V3_SLIPPAGE, correction="raise slippage tolerance", evidence="tx 0xaaa"
        )
        self.store.write_lesson(
            SIG_V2_SLIPPAGE, correction="raise slippage tolerance", evidence="tx 0xbbb"
        )
        # Different failure class — must not be pulled in.
        self.store.write_lesson(
            SIG_V3_ALLOWANCE, correction="approve before swapping", evidence="tx 0xccc"
        )

    def test_failure_class_finds_both_contracts(self):
        names = [h["name"] for h in self.store.find_neighbours("slippageRevert")]
        self.assertEqual(sorted(names), sorted([SIG_V3_SLIPPAGE, SIG_V2_SLIPPAGE]))

    def test_other_failure_class_is_excluded(self):
        names = [h["name"] for h in self.store.find_neighbours("slippageRevert")]
        self.assertNotIn(SIG_V3_ALLOWANCE, names)

    def test_address_token_finds_both_failures_on_one_contract(self):
        address = SIG_V3_SLIPPAGE.split("/")[0]
        names = [h["name"] for h in self.store.find_neighbours(address)]
        self.assertEqual(sorted(names), sorted([SIG_V3_SLIPPAGE, SIG_V3_ALLOWANCE]))

    def test_exclude_sig_drops_the_originating_lesson(self):
        names = [
            h["name"]
            for h in self.store.find_neighbours(
                "slippageRevert", exclude_sig=SIG_V3_SLIPPAGE
            )
        ]
        self.assertEqual(names, [SIG_V2_SLIPPAGE])

    def test_unknown_token_returns_empty(self):
        self.assertEqual(self.store.find_neighbours("deadlineRevert"), [])

    def test_result_order_is_deterministic(self):
        runs = {
            tuple(h["name"] for h in self.store.find_neighbours("slippageRevert"))
            for _ in range(10)
        }
        self.assertEqual(len(runs), 1)


class TestPostmortemJournal(LessonStoreTestCase):

    def test_record_postmortem_uses_native_columns(self):
        event_id = self.store.record_postmortem(
            SIG_V3_SLIPPAGE,
            evaluated={"assumed_slippage_bps": 50},
            acted={"tx_hash": "0xaaa", "submitted_slippage_bps": 50},
            correction="raise slippage tolerance to 150 bps",
        )
        self.assertTrue(event_id)

        events = self.store.client.read_events(limit=10)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["evaluated"], {"assumed_slippage_bps": 50})
        self.assertEqual(event["acted"]["tx_hash"], "0xaaa")
        self.assertEqual(event["forward"]["signature"], SIG_V3_SLIPPAGE)
        self.assertEqual(
            event["forward"]["correction"], "raise slippage tolerance to 150 bps"
        )
        self.assertIsNone(event["extra"])

    def test_timestamp_shape_matches_the_sdk(self):
        self.store.record_postmortem(
            SIG_V3_SLIPPAGE, evaluated={}, acted={}, correction="c"
        )
        ts = self.store.client.read_events(limit=1)[0]["ts"]
        self.assertRegex(ts, r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


if __name__ == "__main__":
    unittest.main()
