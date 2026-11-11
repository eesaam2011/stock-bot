import ast
import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from elite_float_updater import DailyFloatUpdater, SYMBOL_BLACKLIST, BAD_NAME_KEYWORDS


class FakeHTTP:
    def __init__(self):
        self.cache = {"OLD": {"float": 12.0, "updated": "2026-11-09"},
                      "ZERO": {"float": 0.0, "updated": "2026-11-09"}}
        self.original = copy.deepcopy(self.cache)
        self.values = {"NEW": 2.5, "OLD": 3.0}
        self.patches = []
        self.fetched = []
        self.read_status = self.write_status = 200
        self.truncated = False
        self.mismatch = False
        self.missing_file = False

    def get(self, url, **kwargs):
        if "profile2" in url:
            symbol = kwargs["params"]["symbol"]
            self.fetched.append(symbol)
            return SimpleNamespace(status_code=200, json=lambda: {"floatingShare": self.values.get(symbol)})
        if "gist.githubusercontent.com" in url:
            return SimpleNamespace(status_code=200, text=json.dumps(self.cache))
        cache = self.original if self.mismatch and self.patches else self.cache
        file = {"content": json.dumps(cache), "truncated": self.truncated,
                "raw_url": "https://gist.githubusercontent.com/user/id/raw/float_cache.json"}
        return SimpleNamespace(status_code=self.read_status,
                               json=lambda: {"files": {} if self.missing_file else {"float_cache.json": file}})

    def patch(self, url, **kwargs):
        self.patches.append((url, kwargs["json"]))
        if self.write_status == 200:
            self.cache = json.loads(kwargs["json"]["files"]["float_cache.json"]["content"])
        return SimpleNamespace(status_code=self.write_status)


class FloatUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.clock = datetime(2026, 11, 11, 8, 0, tzinfo=ZoneInfo("Asia/Riyadh"))
        self.assets = [SimpleNamespace(symbol="OLD", name="Old Inc"),
                       SimpleNamespace(symbol="NEW", name="New Inc")]
        self.publish = Mock()
        self.wait = Mock()
        self.logs = []
        self.updater = DailyFloatUpdater(
            gist_id="original-id", token="test-token", finnhub_key="test-key",
            filename="float_cache.json", list_assets=lambda: self.assets,
            wait_slot=self.wait, blacklist=SYMBOL_BLACKLIST, bad_names=BAD_NAME_KEYWORDS,
            log=self.logs.append, publish_cache=self.publish, now=lambda: self.clock, http=self.http)

    def test_daily_adds_missing_preserves_old_and_zero_same_destination(self):
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.fetched, ["NEW"])
        self.assertEqual(self.http.cache["OLD"], self.http.original["OLD"])
        self.assertEqual(self.http.cache["ZERO"], self.http.original["ZERO"])
        self.assertEqual(self.http.cache["NEW"], {"float": 2500000.0, "updated": "2026-11-11"})
        url, payload = self.http.patches[-1]
        self.assertEqual(url, "https://api.github.com/gists/original-id")
        self.assertEqual(set(payload["files"]), {"float_cache.json"})
        self.wait.assert_called_once()
        self.publish.assert_called_once()

    def test_monday_refreshes_existing(self):
        self.clock = self.clock.replace(day=16)
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.fetched, ["OLD", "NEW"])
        self.assertEqual(self.http.cache["OLD"]["float"], 3000000.0)

    def test_monday_restart_skips_already_persisted_progress(self):
        self.clock = self.clock.replace(day=16)
        self.http.cache["OLD"]["updated"] = "2026-11-16"
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.fetched, ["NEW"])

    def test_before_schedule_does_nothing(self):
        self.clock = self.clock.replace(hour=7, minute=49)
        self.assertFalse(self.updater.run_once())
        self.assertFalse(self.http.patches)

    def test_once_per_day_and_next_day_runs(self):
        self.assertTrue(self.updater.run_once())
        self.assertFalse(self.updater.run_once())
        self.clock = self.clock.replace(day=12)
        self.assertTrue(self.updater.run_once())

    def test_read_failure_does_not_write_or_mark_complete(self):
        self.http.read_status = 403
        self.assertFalse(self.updater.run_once())
        self.assertFalse(self.http.patches)
        self.assertIsNone(self.updater.completed_date)

    def test_missing_file_refuses_creation(self):
        self.http.missing_file = True
        self.assertFalse(self.updater.run_once())
        self.assertFalse(self.http.patches)

    def test_write_failure_does_not_publish_or_mark_complete(self):
        self.http.write_status = 403
        self.assertFalse(self.updater.run_once())
        self.publish.assert_not_called()
        self.assertIsNone(self.updater.completed_date)

    def test_readback_mismatch_does_not_report_verified(self):
        self.http.mismatch = True
        self.assertFalse(self.updater.run_once())
        self.publish.assert_not_called()
        self.assertFalse(any("VERIFIED" in line for line in self.logs))

    def test_truncated_gist_reads_full_raw_file(self):
        self.http.truncated = True
        self.assertTrue(self.updater.run_once())
        self.assertIn("OLD", self.http.cache)

    def test_api_missing_value_preserves_old_on_monday(self):
        self.clock = self.clock.replace(day=16)
        self.http.values = {}
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.cache, self.http.original)

    def test_original_exclusions_without_price_filter(self):
        self.assets += [SimpleNamespace(symbol="JPM", name="JPM"),
                        SimpleNamespace(symbol="FUND", name="Index Fund")]
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.fetched, ["NEW"])

    def test_concurrent_run_skips(self):
        self.updater.lock.acquire()
        self.assertFalse(self.updater.run_once())
        self.updater.lock.release()

    def test_empty_cache_refuses_overwrite(self):
        self.http.cache = {}
        self.assertFalse(self.updater.run_once())
        self.assertFalse(self.http.patches)

    def test_invalid_float_is_not_published(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                self.http.values["NEW"] = value
                self.assertIsNone(self.updater.fetch_float("NEW"))

    def test_checkpoints_and_final_write_are_verified(self):
        self.assets = [SimpleNamespace(symbol=f"S{i}", name="Company") for i in range(26)]
        self.http.values = {a.symbol: 1.0 for a in self.assets}
        self.assertTrue(self.updater.run_once())
        self.assertEqual(len(self.http.patches), 2)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(len(self.http.cache), 28)

    def test_failed_write_can_retry_without_losing_old_records(self):
        self.http.write_status = 500
        self.assertFalse(self.updater.run_once())
        self.http.write_status = 200
        self.assertTrue(self.updater.run_once())
        self.assertEqual(self.http.cache["OLD"], self.http.original["OLD"])

    def test_exclusions_match_original_producer(self):
        tree = ast.parse(Path("early_explosion_bot.py").read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("SYMBOL_BLACKLIST", "BAD_NAME_KEYWORDS"):
                        values[target.id] = ast.literal_eval(node.value)
        self.assertEqual(values["SYMBOL_BLACKLIST"], SYMBOL_BLACKLIST)
        self.assertEqual(values["BAD_NAME_KEYWORDS"], BAD_NAME_KEYWORDS)


if __name__ == "__main__":
    unittest.main()
